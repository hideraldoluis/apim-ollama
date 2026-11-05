"""Rotas de usuários do painel (admin); as regras ficam em app/servicos/usuarios.py.

Regras:
- não existe autocadastro; só admin cria usuários, sempre com senha temporária (24 h, troca obrigatória);
- usuários nunca são excluídos, só desativados (a auditoria continua apontando para a pessoa);
- sempre resta ao menos um admin ativo;
- ninguém altera o próprio papel, se desativa ou redefine a própria senha por aqui;
- mudança de papel, redefinição de senha e desativação encerram as sessões do usuário.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from psycopg.types.json import Jsonb

from .. import auditoria, autenticacao as auth, db, validacao
from ..config import config
from ..servicos import NaoEncontrado, RegraViolada, usuarios as servico
from ..web import redirecionar, render

router = APIRouter(prefix="/usuarios")


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
        alvo, temporaria = servico.criar(u, auth.ip_cliente(request), login, nome, papel)
    except validacao.Invalido as e:
        return render(request, "usuario_novo.html", {"erros": [str(e)], "form": form}, status=400)
    return render(request, "senha_temporaria.html", {
        "alvo": alvo, "temporaria": temporaria,
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


def _executar(id_: int, acao):
    """Roda a ação do serviço traduzindo usuário inexistente em 404."""
    try:
        return acao()
    except NaoEncontrado:
        raise HTTPException(404) from None


@router.post("/{id_}/papel")
def alterar_papel(request: Request, id_: int, u: dict = Depends(auth.exigir_admin), papel: str = Form("")):
    try:
        mudou = _executar(id_, lambda: servico.alterar_papel(u, auth.ip_cliente(request), id_, papel))
    except (RegraViolada, validacao.Invalido) as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    if not mudou:
        return redirecionar(f"/usuarios/{id_}", "Nada foi alterado.", "aviso")
    return redirecionar(f"/usuarios/{id_}", "Papel alterado. As sessões do usuário foram encerradas.")


@router.post("/{id_}/redefinir-senha")
def redefinir_senha(request: Request, id_: int, u: dict = Depends(auth.exigir_admin)):
    try:
        alvo, temporaria = _executar(id_, lambda: servico.redefinir_senha(u, auth.ip_cliente(request), id_))
    except RegraViolada as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    return render(request, "senha_temporaria.html", {"alvo": alvo, "temporaria": temporaria,
                                                     "horas": config.senha_temp_validade_h,
                                                     "titulo": "Senha redefinida"})


@router.post("/{id_}/desbloquear")
def desbloquear(request: Request, id_: int, u: dict = Depends(auth.exigir_admin)):
    _executar(id_, lambda: servico.desbloquear(u, auth.ip_cliente(request), id_))
    return redirecionar(f"/usuarios/{id_}", "Usuário desbloqueado.")


@router.post("/{id_}/desativar")
def desativar(request: Request, id_: int, u: dict = Depends(auth.exigir_admin), motivo: str = Form("")):
    try:
        mudou = _executar(id_, lambda: servico.desativar(u, auth.ip_cliente(request), id_, motivo))
    except (RegraViolada, validacao.Invalido) as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    if not mudou:
        return redirecionar(f"/usuarios/{id_}", "O usuário já está desativado.", "aviso")
    return redirecionar(f"/usuarios/{id_}", "Usuário desativado.")


@router.post("/{id_}/reativar")
def reativar(request: Request, id_: int, u: dict = Depends(auth.exigir_admin)):
    """Reativação sempre gera nova senha temporária (a antiga pode estar comprometida ou esquecida)."""
    try:
        alvo, temporaria = _executar(id_, lambda: servico.reativar(u, auth.ip_cliente(request), id_))
    except RegraViolada as e:
        return redirecionar(f"/usuarios/{id_}", str(e), "erro")
    if alvo is None:
        return redirecionar(f"/usuarios/{id_}", "O usuário já está ativo.", "aviso")
    return render(request, "senha_temporaria.html", {"alvo": alvo, "temporaria": temporaria,
                                                     "horas": config.senha_temp_validade_h,
                                                     "titulo": "Usuário reativado"})
