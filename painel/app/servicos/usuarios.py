"""Governança de usuários do painel.

Regras:
- não existe autocadastro; só admin cria usuários, sempre com senha temporária (24 h, troca obrigatória);
- usuários nunca são excluídos, só desativados (a auditoria continua apontando para a pessoa);
- sempre resta ao menos um admin ativo;
- ninguém altera o próprio papel, se desativa ou redefine a própria senha;
- mudança de papel, redefinição de senha e desativação encerram as sessões do usuário.

As senhas temporárias são devolvidas ao chamador e nunca gravadas em claro.
"""
from psycopg.errors import UniqueViolation

from .. import auditoria, autenticacao as auth, db, senhas, validacao
from ..config import config
from . import NaoEncontrado, RegraViolada, com_origem


def _usuario(conn, id_: int) -> dict:
    u = conn.execute("select * from painel.usuarios where id = %s for update", (id_,)).fetchone()
    if u is None:
        raise NaoEncontrado("Usuário não encontrado.")
    return u


def _garantir_outro_admin(conn, alvo_id: int) -> None:
    """Trava os admins ativos e confirma que resta ao menos um além do alvo."""
    admins = conn.execute(
        "select id from painel.usuarios where papel = 'admin' and ativo order by id for update").fetchall()
    if not any(a["id"] != alvo_id for a in admins):
        raise RegraViolada("Operação recusada: é preciso manter ao menos um administrador ativo.")


def _nao_si_mesmo(ator: dict, alvo_id: int) -> None:
    if ator["id"] == alvo_id:
        raise RegraViolada("Você não pode executar esta operação sobre o próprio usuário.")


def _nova_temporaria(conn, alvo_id: int) -> str:
    temporaria = senhas.gerar_temporaria()
    conn.execute(
        "update painel.usuarios set senha_hash = %s, deve_trocar_senha = true, "
        "senha_temp_expira_em = now() + make_interval(hours => %s), falhas_login = 0, bloqueado_ate = null "
        "where id = %s",
        (senhas.gerar_hash(temporaria), config.senha_temp_validade_h, alvo_id))
    return temporaria


def id_por_login(login: str) -> int:
    u = db.consultar_um("select id from painel.usuarios where login = %s", (login.strip().lower(),))
    if u is None:
        raise NaoEncontrado(f"Usuário {login} não encontrado.")
    return u["id"]


def criar(ator: dict, ip: str | None, login: str, nome: str, papel: str,
          origem: str | None = None) -> tuple[dict, str]:
    """Devolve ({"id","login","nome"}, senha temporária)."""
    login = validacao.login(login)
    nome = validacao.nome(nome)
    papel = validacao.papel(papel)
    temporaria = senhas.gerar_temporaria()
    try:
        with db.transacao() as conn:
            novo = conn.execute(
                "insert into painel.usuarios (login, nome, papel, senha_hash, deve_trocar_senha, "
                "senha_temp_expira_em, criado_por) values (%s, %s, %s, %s, true, "
                "now() + make_interval(hours => %s), %s) returning id",
                (login, nome, papel, senhas.gerar_hash(temporaria), config.senha_temp_validade_h, ator["login"]),
            ).fetchone()
            auditoria.registrar("usuario_criado", ator["login"], ip, login,
                                com_origem({"nome": nome, "papel": papel}, origem), conn)
    except UniqueViolation:
        raise validacao.Invalido("Já existe um usuário com esse login (inclusive desativado).") from None
    return {"id": novo["id"], "login": login, "nome": nome}, temporaria


def alterar_papel(ator: dict, ip: str | None, id_: int, papel: str, origem: str | None = None) -> bool:
    """False se o usuário já tinha esse papel (nada foi alterado)."""
    papel = validacao.papel(papel)
    with db.transacao() as conn:
        _nao_si_mesmo(ator, id_)
        alvo = _usuario(conn, id_)
        if alvo["papel"] == papel:
            return False
        if alvo["papel"] == "admin":
            _garantir_outro_admin(conn, id_)
        conn.execute("update painel.usuarios set papel = %s where id = %s", (papel, id_))
        auth.encerrar_sessoes_do_usuario(id_, conn)
        auditoria.registrar("usuario_papel", ator["login"], ip, alvo["login"],
                            com_origem({"antes": alvo["papel"], "depois": papel}, origem), conn)
    return True


def redefinir_senha(ator: dict, ip: str | None, id_: int, origem: str | None = None) -> tuple[dict, str]:
    with db.transacao() as conn:
        _nao_si_mesmo(ator, id_)
        alvo = _usuario(conn, id_)
        if not alvo["ativo"]:
            raise RegraViolada("Usuário desativado: use Reativar, que já gera uma senha temporária.")
        temporaria = _nova_temporaria(conn, id_)
        auth.encerrar_sessoes_do_usuario(id_, conn)
        auditoria.registrar("usuario_senha_redefinida", ator["login"], ip, alvo["login"],
                            com_origem({}, origem), conn)
    return alvo, temporaria


def desbloquear(ator: dict, ip: str | None, id_: int, origem: str | None = None) -> dict:
    with db.transacao() as conn:
        alvo = _usuario(conn, id_)
        conn.execute("update painel.usuarios set bloqueado_ate = null, falhas_login = 0 where id = %s", (id_,))
        auditoria.registrar("usuario_desbloqueado", ator["login"], ip, alvo["login"],
                            com_origem({}, origem), conn)
    return alvo


def desativar(ator: dict, ip: str | None, id_: int, motivo: str, origem: str | None = None) -> bool:
    """False se o usuário já estava desativado."""
    motivo = validacao.motivo(motivo)
    with db.transacao() as conn:
        _nao_si_mesmo(ator, id_)
        alvo = _usuario(conn, id_)
        if not alvo["ativo"]:
            return False
        if alvo["papel"] == "admin":
            _garantir_outro_admin(conn, id_)
        conn.execute(
            "update painel.usuarios set ativo = false, desativado_em = now(), desativado_por = %s, "
            "motivo_desativacao = %s where id = %s", (ator["login"], motivo, id_))
        auth.encerrar_sessoes_do_usuario(id_, conn)
        auditoria.registrar("usuario_desativado", ator["login"], ip, alvo["login"],
                            com_origem({"motivo": motivo}, origem), conn)
    return True


def reativar(ator: dict, ip: str | None, id_: int, origem: str | None = None) -> tuple[dict | None, str | None]:
    """Reativação sempre gera nova senha temporária (a antiga pode estar comprometida ou esquecida).
    Devolve (None, None) se o usuário já estava ativo."""
    with db.transacao() as conn:
        _nao_si_mesmo(ator, id_)
        alvo = _usuario(conn, id_)
        if alvo["ativo"]:
            return None, None
        conn.execute(
            "update painel.usuarios set ativo = true, reativado_em = now(), desativado_em = null, "
            "desativado_por = null, motivo_desativacao = null where id = %s", (id_,))
        temporaria = _nova_temporaria(conn, id_)
        auditoria.registrar("usuario_reativado", ator["login"], ip, alvo["login"], com_origem({}, origem), conn)
    return alvo, temporaria
