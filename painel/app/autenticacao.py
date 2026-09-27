"""Sessões, CSRF e controle de acesso por papel."""
import hashlib
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, Request

from . import auditoria, db, senhas
from .config import config

COOKIE = "__Host-painel" if config.cookie_seguro else "painel"
COOKIE_PRE = "__Host-painel-pre" if config.cookie_seguro else "painel-pre"
MSG_LOGIN = "Usuário ou senha inválidos, ou conta temporariamente bloqueada."


class Redirecionar(Exception):
    def __init__(self, url: str):
        self.url = url


class Proibido(Exception):
    def __init__(self, motivo: str = "Acesso negado."):
        self.motivo = motivo


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ip_cliente(request: Request) -> str:
    return request.client.host if request.client else ""


def definir_cookie(resposta, nome: str, valor: str, max_age: int | None = None) -> None:
    resposta.set_cookie(nome, valor, max_age=max_age, path="/", httponly=True,
                        secure=config.cookie_seguro, samesite="strict")


# ---------------------------------------------------------------------------
# Limite de falhas por IP (em memória; complementa o bloqueio por conta)
# ---------------------------------------------------------------------------
_falhas_ip: dict[str, deque] = defaultdict(deque)
_JANELA_IP_S = 15 * 60
_MAX_FALHAS_IP = 20


def ip_bloqueado(ip: str) -> bool:
    fila = _falhas_ip[ip]
    limite = time.monotonic() - _JANELA_IP_S
    while fila and fila[0] < limite:
        fila.popleft()
    return len(fila) >= _MAX_FALHAS_IP


def _falha_ip(ip: str) -> None:
    _falhas_ip[ip].append(time.monotonic())


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def autenticar(login: str, senha: str, ip: str) -> tuple[dict | None, str | None]:
    """Devolve (usuário, None) no sucesso ou (None, mensagem) na falha."""
    login = login.strip().lower()
    alvo = login if len(login) <= 64 and login.replace(".", "").replace("_", "").replace("-", "").isalnum() else "(inválido)"
    if ip_bloqueado(ip):
        auditoria.registrar("login_falha", None, ip, alvo, {"motivo": "limite por IP"})
        return None, "Muitas tentativas a partir deste endereço. Aguarde alguns minutos."

    with db.transacao() as conn:
        u = conn.execute("select * from painel.usuarios where login = %s for update", (login,)).fetchone()
        if u is None:
            senhas.conferir(None, senha)  # tempo constante
            _falha_ip(ip)
            auditoria.registrar("login_falha", None, ip, alvo, {"motivo": "usuário inexistente"}, conn)
            return None, MSG_LOGIN
        if not u["ativo"]:
            senhas.conferir(None, senha)
            _falha_ip(ip)
            auditoria.registrar("login_falha", None, ip, login, {"motivo": "usuário desativado"}, conn)
            return None, MSG_LOGIN
        bloqueado = conn.execute("select %s::timestamptz > now() as b", (u["bloqueado_ate"],)).fetchone()["b"]
        if bloqueado:
            _falha_ip(ip)
            auditoria.registrar("login_falha", None, ip, login, {"motivo": "conta bloqueada"}, conn)
            return None, MSG_LOGIN
        if not senhas.conferir(u["senha_hash"], senha):
            _falha_ip(ip)
            falhas = u["falhas_login"] + 1
            if falhas >= config.login_max_falhas:
                conn.execute(
                    "update painel.usuarios set falhas_login = 0, "
                    "bloqueado_ate = now() + make_interval(mins => %s) where id = %s",
                    (config.login_bloqueio_min, u["id"]))
                auditoria.registrar("login_bloqueio", None, ip, login,
                                    {"minutos": config.login_bloqueio_min}, conn)
            else:
                conn.execute("update painel.usuarios set falhas_login = %s where id = %s", (falhas, u["id"]))
                auditoria.registrar("login_falha", None, ip, login, {"motivo": "senha incorreta"}, conn)
            return None, MSG_LOGIN
        if u["deve_trocar_senha"] and u["senha_temp_expira_em"] is not None:
            expirada = conn.execute("select %s::timestamptz < now() as e", (u["senha_temp_expira_em"],)).fetchone()["e"]
            if expirada:
                auditoria.registrar("login_falha", None, ip, login, {"motivo": "senha temporária expirada"}, conn)
                return None, "Senha temporária expirada. Peça a um administrador uma nova senha."

        conn.execute("update painel.usuarios set falhas_login = 0, bloqueado_ate = null, "
                     "ultimo_login = now() where id = %s", (u["id"],))
        auditoria.registrar("login", login, ip, login, conn=conn)
    return u, None


def criar_sessao(usuario_id: int, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    db.executar("insert into painel.sessoes (token_hash, usuario_id, csrf, ip) values (%s, %s, %s, %s)",
                (_hash(token), usuario_id, secrets.token_urlsafe(32), ip))
    return token


def encerrar_sessao(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.executar("delete from painel.sessoes where token_hash = %s", (_hash(token),))


def encerrar_sessoes_do_usuario(usuario_id: int, conn=None, exceto_token: str | None = None) -> None:
    sql = "delete from painel.sessoes where usuario_id = %s and token_hash <> %s"
    params = (usuario_id, _hash(exceto_token) if exceto_token else "")
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.executar(sql, params)


def _sessao_atual(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    th = _hash(token)
    linha = db.consultar_um(
        """
        select u.id, u.login, u.nome, u.papel, u.ativo, u.deve_trocar_senha, s.csrf,
               s.ultimo_uso < now() - make_interval(mins => %(inat)s)
                 or s.criada_em < now() - make_interval(hours => %(max)s) as expirada,
               s.ultimo_uso < now() - interval '1 minute' as tocar
          from painel.sessoes s join painel.usuarios u on u.id = s.usuario_id
         where s.token_hash = %(th)s
        """,
        {"th": th, "inat": config.sessao_inatividade_min, "max": config.sessao_maxima_h},
    )
    if linha is None:
        return None
    if linha["expirada"] or not linha["ativo"]:
        db.executar("delete from painel.sessoes where token_hash = %s", (th,))
        return None
    if linha["tocar"]:
        db.executar("update painel.sessoes set ultimo_uso = now() where token_hash = %s", (th,))
    return linha


# ---------------------------------------------------------------------------
# Dependências do FastAPI
# ---------------------------------------------------------------------------
async def _conferir_csrf(request: Request, esperado: str) -> None:
    if request.method != "POST":
        return
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), esperado):
        raise Proibido("Formulário expirado ou inválido. Recarregue a página e tente novamente.")


def _sessao(request: Request) -> dict:
    # Síncrona de propósito: o FastAPI a executa no threadpool (acesso ao banco é bloqueante).
    u = _sessao_atual(request)
    if u is None:
        raise Redirecionar("/login")
    if u["deve_trocar_senha"] and request.url.path not in ("/trocar-senha", "/logout"):
        raise Redirecionar("/trocar-senha")
    return u


async def usuario_logado(request: Request, u: dict = Depends(_sessao)) -> dict:
    await _conferir_csrf(request, u["csrf"])
    request.state.usuario = u
    return u


async def exigir_admin(u: dict = Depends(usuario_logado)) -> dict:
    if u["papel"] != "admin":
        raise Proibido("Esta área é restrita a administradores.")
    return u
