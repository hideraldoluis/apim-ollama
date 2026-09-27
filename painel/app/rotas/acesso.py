"""Login, logout e troca de senha."""
import secrets

from fastapi import APIRouter, Depends, Form, Request

from .. import auditoria, autenticacao as auth, db, senhas
from ..web import redirecionar, render

router = APIRouter()


def _form_login(request: Request, erro: str | None = None, status: int = 200):
    # CSRF antes do login: token aleatório em cookie e no formulário (double submit).
    token = secrets.token_urlsafe(32)
    resposta = render(request, "login.html", {"csrf_pre": token, "erro": erro}, status=status)
    auth.definir_cookie(resposta, auth.COOKIE_PRE, token, max_age=600)
    return resposta


@router.get("/login")
def login_form(request: Request):
    return _form_login(request)


@router.post("/login")
def login(request: Request, login: str = Form(""), senha: str = Form(""), csrf: str = Form("")):
    esperado = request.cookies.get(auth.COOKIE_PRE, "")
    if not esperado or not secrets.compare_digest(csrf, esperado):
        return _form_login(request, "Formulário expirado. Tente novamente.", status=400)
    ip = auth.ip_cliente(request)
    usuario, erro = auth.autenticar(login, senha, ip)
    if usuario is None:
        return _form_login(request, erro, status=401)
    token = auth.criar_sessao(usuario["id"], ip)
    resposta = redirecionar("/trocar-senha" if usuario["deve_trocar_senha"] else "/")
    auth.definir_cookie(resposta, auth.COOKIE, token)
    resposta.delete_cookie(auth.COOKIE_PRE, path="/")
    return resposta


@router.post("/logout")
def logout(request: Request, u: dict = Depends(auth.usuario_logado)):
    auth.encerrar_sessao(request)
    auditoria.registrar("logout", u["login"], auth.ip_cliente(request), u["login"])
    resposta = redirecionar("/login", "Sessão encerrada.")
    resposta.delete_cookie(auth.COOKIE, path="/")
    return resposta


@router.get("/trocar-senha")
def trocar_senha_form(request: Request, u: dict = Depends(auth.usuario_logado)):
    return render(request, "trocar_senha.html", {"erros": []})


@router.post("/trocar-senha")
def trocar_senha(request: Request, u: dict = Depends(auth.usuario_logado),
                 atual: str = Form(""), nova: str = Form(""), confirmacao: str = Form("")):
    atual_hash = db.consultar_um("select senha_hash from painel.usuarios where id = %s", (u["id"],))["senha_hash"]
    erros = []
    if not senhas.conferir(atual_hash, atual):
        erros.append("Senha atual incorreta.")
    if nova != confirmacao:
        erros.append("A confirmação não confere com a nova senha.")
    if nova == atual:
        erros.append("A nova senha deve ser diferente da atual.")
    if not erros:
        erros = senhas.problemas(nova, u["login"], u["nome"], u["id"])
    if erros:
        return render(request, "trocar_senha.html", {"erros": erros}, status=400)

    novo_hash = senhas.gerar_hash(nova)
    with db.transacao() as conn:
        conn.execute(
            "update painel.usuarios set senha_hash = %s, deve_trocar_senha = false, senha_temp_expira_em = null, "
            "senha_alterada_em = now() where id = %s", (novo_hash, u["id"]))
        conn.execute("insert into painel.senhas_historico (usuario_id, senha_hash) values (%s, %s)",
                     (u["id"], novo_hash))
        # Outras sessões abertas do usuário deixam de valer.
        auth.encerrar_sessoes_do_usuario(u["id"], conn, exceto_token=request.cookies.get(auth.COOKIE))
        auditoria.registrar("senha_trocada", u["login"], auth.ip_cliente(request), u["login"], conn=conn)
    return redirecionar("/", "Senha alterada.")
