"""Governança de usuários do painel (admin).

Regras:
- não existe autocadastro; só admin cria usuários, sempre com senha temporária (24 h, troca obrigatória);
- usuários nunca são excluídos, só desativados (a auditoria continua apontando para a pessoa);
- sempre resta ao menos um admin ativo;
- ninguém altera o próprio papel, se desativa ou redefine a própria senha por aqui;
- mudança de papel, redefinição de senha e desativação encerram as sessões do usuário.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .. import auditoria, autenticacao as auth, db, senhas, validacao
from ..config import config
from ..web import redirecionar, render

router = APIRouter(prefix="/usuarios")


class RegraViolada(Exception):
    pass


def _usuario_ou_404(conn, id_: int) -> dict:
    u = conn.execute("select * from painel.usuarios where id = %s for update", (id_,)).fetchone()
    if u is None:
        raise HTTPException(404)
    return u


def _garantir_outro_admin(conn, alvo_id: int) -> None:
    """Trava os admins ativos e confirma que resta ao menos um além do alvo."""
    admins = conn.execute(
        "select id from painel.usuarios where papel = 'admin' and ativo order by id for update").fetchall()
    if not any(a["id"] != alvo_id for a in admins):
        raise RegraViolada("Operação recusada: é preciso manter ao menos um administrador ativo.")


def _nao_si_mesmo(atual: dict, alvo_id: int) -> None:
    if atual["id"] == alvo_id:
        raise RegraViolada("Você não pode executar esta operação sobre o próprio usuário.")


def _nova_temporaria(conn, alvo_id: int) -> str:
    temporaria = senhas.gerar_temporaria()
    conn.execute(
        "update painel.usuarios set senha_hash = %s, deve_trocar_senha = true, "
        "senha_temp_expira_em = now() + make_interval(hours => %s), falhas_login = 0, bloqueado_ate = null "
        "where id = %s",
        (senhas.gerar_hash(temporaria), config.senha_temp_validade_h, alvo_id))
    return temporaria


@router.get("")
def lista(request: Request, u: dict = Depends(auth.exigir_admin)):
    usuarios = db.consultar(
        "select *, bloqueado_ate is not null and bloqueado_ate > now() as bloqueado, "
        "deve_trocar_senha and senha_temp_expira_em < now() as temp_expirada "
        "from painel.usuarios order by ativo desc, login")
    return render(request, "usuarios.html", {"usuarios": usuarios})


@router.get("/novo")
def novo_form(request: Request, u: dict = Depends(auth.exigir_admin)):
    return render(request, "usuario_novo.html", {"erros": [], "form": {"login": "", "nome": "", "papel": "leitor"}})


@router.post("/novo")
def criar(request: Request, u: dict = Depends(auth.exigir_admin),
          login: str = Form(""), nome: str = Form(""), papel: str = Form("leitor")):
    form = {"login": login, "nome": nome, "papel": papel}
    try:
        login = validacao.login(login)
        nome = validacao.nome(nome)
        papel = validacao.papel(papel)
    except validacao.Invalido as e:
        return render(request, "usuario_novo.html", {"erros": [str(e)], "form": form}, status=400)
    temporaria = senhas.gerar_temporaria()
    try:
        with db.transacao() as conn:
            novo = conn.execute(
                "insert into painel.usuarios (login, nome, papel, senha_hash, deve_trocar_senha, "
                "senha_temp_expira_em, criado_por) values (%s, %s, %s, %s, true, "
                "now() + make_interval(hours => %s), %s) returning id",
                (login, nome, papel, senhas.gerar_hash(temporaria), config.senha_temp_validade_h, u["login"]),
            ).fetchone()
            auditoria.registrar("usuario_criado", u["login"], auth.ip_cliente(request), login,
                                {"nome": nome, "papel": papel}, conn)
    except UniqueViolation:
        return render(request, "usuario_novo.html",
                      {"erros": ["Já existe um usuário com esse login (inclusive desativado)."], "form": form},
                      status=400)
    return render(request, "senha_temporaria.html", {
        "alvo": {"id": novo["id"], "login": login, "nome": nome}, "temporaria": temporaria,
        "horas": config.senha_temp_validade_h, "titulo": "Usuário criado"})


@router.get("/revisao")
def revisao_form(request: Request, u: dict = Depends(auth.exigir_admin)):
    usuarios = db.consultar(
        "select login, nome, papel, ativo, criado_em, criado_por, ultimo_login, desativado_em, motivo_desativacao, "
        "extract(day from now() - greatest(ultimo_login, criado_em, reativado_em))::int as dias_sem_login "
        "from painel.usuarios order by ativo desc, papel, login")
    revisoes = db.consultar(
        "select quando, revisor, observacao from painel.revisoes_acesso order by quando desc limit 10")
    return render(request, "revisao.html", {"usuarios": usuarios, "revisoes": revisoes,
                                            "inatividade": config.inatividade_desativa_dias})


@router.post("/revisao")
def revisao(request: Request, u: dict = Depends(auth.exigir_admin), observacao: str = Form("")):
    try:
        observacao = validacao.motivo(observacao)
    except validacao.Invalido:
        return redirecionar("/usuarios/revisao", "Descreva o resultado da revisão (de 5 a 500 caracteres).", "erro")
    with db.transacao() as conn:
        retrato = conn.execute(
            "select login, papel, ativo, ultimo_login from painel.usuarios order by login").fetchall()
        retrato = [{**r, "ultimo_login": r["ultimo_login"].isoformat() if r["ultimo_login"] else None}
                   for r in retrato]
        conn.execute("insert into painel.revisoes_acesso (revisor, observacao, usuarios) values (%s, %s, %s)",
                     (u["login"], observacao, Jsonb(retrato)))
        auditoria.registrar("revisao_acesso", u["login"], auth.ip_cliente(request), None,
                            {"observacao": observacao, "usuarios": len(retrato)}, conn)
    return redirecionar("/usuarios/revisao", "Revisão registrada.")


@router.get("/{id_}")
def detalhe(request: Request, id_: int, u: dict = Depends(auth.exigir_admin)):
    alvo = db.consultar_um(
        "select *, bloqueado_ate is not null and bloqueado_ate > now() as bloqueado from painel.usuarios "
        "where id = %s", (id_,))
    if alvo is None:
        raise HTTPException(404)
    trilha = db.consultar(
        "select quando, usuario, ip, acao, alvo, detalhes from painel.auditoria "
        "where alvo = %s or usuario = %s order by quando desc limit 50", (alvo["login"], alvo["login"]))
    return render(request, "usuario.html", {"alvo": alvo, "trilha": trilha})


@router.post("/{id_}/papel")
def alterar_papel(request: Request, id_: int, u: dict = Depends(auth.exigir_admin), papel: str = Form("")):
    try:
        papel = validacao.papel(papel)
        with db.transacao() as conn:
            _nao_si_mesmo(u, id_)
            alvo = _usuario_ou_404(conn, id_)
            if alvo["papel"] == papel:
                return redirecionar(f"/usuarios/{id_}", "Nada foi alterado.", "aviso")
            if alvo["papel"] == "admin":
                _garantir_outro_admin(conn, id_)
            conn.execute("update painel.usuarios set papel = %s where id = %s", (papel, id_))
            auth.encerrar_sessoes_do_usuario(id_, conn)
            auditoria.registrar("usuario_papel", u["login"], auth.ip_cliente(request), alvo["login"],
                                {"antes": alvo["papel"], "depois": papel}, conn)
    except (RegraViolada, validacao.Invalido) as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    return redirecionar(f"/usuarios/{id_}", "Papel alterado. As sessões do usuário foram encerradas.")


@router.post("/{id_}/redefinir-senha")
def redefinir_senha(request: Request, id_: int, u: dict = Depends(auth.exigir_admin)):
    try:
        with db.transacao() as conn:
            _nao_si_mesmo(u, id_)
            alvo = _usuario_ou_404(conn, id_)
            if not alvo["ativo"]:
                raise RegraViolada("Usuário desativado: use Reativar, que já gera uma senha temporária.")
            temporaria = _nova_temporaria(conn, id_)
            auth.encerrar_sessoes_do_usuario(id_, conn)
            auditoria.registrar("usuario_senha_redefinida", u["login"], auth.ip_cliente(request), alvo["login"],
                                conn=conn)
    except RegraViolada as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    return render(request, "senha_temporaria.html", {"alvo": alvo, "temporaria": temporaria,
                                                     "horas": config.senha_temp_validade_h,
                                                     "titulo": "Senha redefinida"})


@router.post("/{id_}/desbloquear")
def desbloquear(request: Request, id_: int, u: dict = Depends(auth.exigir_admin)):
    with db.transacao() as conn:
        alvo = _usuario_ou_404(conn, id_)
        conn.execute("update painel.usuarios set bloqueado_ate = null, falhas_login = 0 where id = %s", (id_,))
        auditoria.registrar("usuario_desbloqueado", u["login"], auth.ip_cliente(request), alvo["login"], conn=conn)
    return redirecionar(f"/usuarios/{id_}", "Usuário desbloqueado.")


@router.post("/{id_}/desativar")
def desativar(request: Request, id_: int, u: dict = Depends(auth.exigir_admin), motivo: str = Form("")):
    try:
        motivo = validacao.motivo(motivo)
        with db.transacao() as conn:
            _nao_si_mesmo(u, id_)
            alvo = _usuario_ou_404(conn, id_)
            if not alvo["ativo"]:
                return redirecionar(f"/usuarios/{id_}", "O usuário já está desativado.", "aviso")
            if alvo["papel"] == "admin":
                _garantir_outro_admin(conn, id_)
            conn.execute(
                "update painel.usuarios set ativo = false, desativado_em = now(), desativado_por = %s, "
                "motivo_desativacao = %s where id = %s", (u["login"], motivo, id_))
            auth.encerrar_sessoes_do_usuario(id_, conn)
            auditoria.registrar("usuario_desativado", u["login"], auth.ip_cliente(request), alvo["login"],
                                {"motivo": motivo}, conn)
    except (RegraViolada, validacao.Invalido) as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    return redirecionar(f"/usuarios/{id_}", "Usuário desativado.")


@router.post("/{id_}/reativar")
def reativar(request: Request, id_: int, u: dict = Depends(auth.exigir_admin)):
    """Reativação sempre gera nova senha temporária (a antiga pode estar comprometida ou esquecida)."""
    try:
        with db.transacao() as conn:
            _nao_si_mesmo(u, id_)
            alvo = _usuario_ou_404(conn, id_)
            if alvo["ativo"]:
                return redirecionar(f"/usuarios/{id_}", "O usuário já está ativo.", "aviso")
            conn.execute(
                "update painel.usuarios set ativo = true, reativado_em = now(), desativado_em = null, "
                "desativado_por = null, motivo_desativacao = null where id = %s", (id_,))
            temporaria = _nova_temporaria(conn, id_)
            auditoria.registrar("usuario_reativado", u["login"], auth.ip_cliente(request), alvo["login"], conn=conn)
    except RegraViolada as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    return render(request, "senha_temporaria.html", {"alvo": alvo, "temporaria": temporaria,
                                                     "horas": config.senha_temp_validade_h,
                                                     "titulo": "Usuário reativado"})
