"""Chaves de consumidores: consulta (todos) e gestão (admin)."""
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import autenticacao as auth, consultas, db, litellm, validacao
from ..config import config
from ..servicos import chaves as servico
from ..web import redirecionar, render

router = APIRouter()
_TOKEN = re.compile(r"^[0-9a-f]{64}$")


def _token_valido(token: str) -> str:
    if not _TOKEN.match(token):
        raise HTTPException(404)
    return token


def _chave_ou_404(token: str) -> dict:
    k = consultas.chave(_token_valido(token))
    if k is None:
        raise HTTPException(404)
    return k


def _modelos_disponiveis() -> tuple[list[str], str | None]:
    return servico.disponiveis()


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------
@router.get("/chaves")
def lista(request: Request, u: dict = Depends(auth.usuario_logado)):
    inicio, fim = consultas.periodo(None, None, 30)
    tokens = {}
    for r in consultas.consumo(inicio, fim, "chave"):
        tokens[r["chave"]] = tokens.get(r["chave"], 0) + r["tok_total"]
    chaves = consultas.chaves()
    for k in chaves:
        k["tok_30d"] = tokens.get(k["token"], 0)
    expiradas = sum(1 for k in chaves if k["status"] == "expirada" and k["modelos_reais"])
    return render(request, "chaves.html", {"chaves": chaves, "expiradas_com_modelos": expiradas})


@router.get("/chaves/revogadas")
def revogadas(request: Request, u: dict = Depends(auth.usuario_logado)):
    linhas = db.consultar(
        "select token, key_alias, criado_para, revogada_em, revogada_por, motivo "
        "from painel.chaves_historico order by revogada_em desc limit 500")
    return render(request, "chaves_revogadas.html", {"linhas": linhas})


@router.get("/chaves/nova")
def nova_form(request: Request, u: dict = Depends(auth.exigir_admin)):
    disponiveis, erro = _modelos_disponiveis()
    return render(request, "chave_nova.html", {
        "disponiveis": disponiveis, "erro_gateway": erro, "erros": [],
        "form": {"alias": "", "criado_para": "", "modelos": [m.strip() for m in config.chave_modelos.split(",") if m.strip()],
                 "rpm": config.chave_rpm, "paralelas": config.chave_paralelas, "validade": config.chave_validade},
    })


@router.get("/chaves/{token}")
def detalhe(request: Request, token: str, u: dict = Depends(auth.usuario_logado)):
    k = consultas.chave(_token_valido(token))
    if k is None:
        hist = db.consultar_um(
            "select token, key_alias, criado_para, revogada_em, revogada_por, motivo from painel.chaves_historico "
            "where token = %s order by revogada_em desc limit 1", (token,))
        if hist is None:
            raise HTTPException(404)
        return render(request, "chave.html", {"k": None, "hist": hist})
    inicio, fim = consultas.periodo(None, None, 30)
    por_modelo = [r for r in consultas.consumo(inicio, fim, "modelo", chave=token)]
    agendada = db.consultar_um(
        "select executar_em, agendada_por, motivo from painel.revogacoes_agendadas "
        "where token = %s and executada_em is null", (token,))
    trilha = []
    if u["papel"] == "admin":
        trilha = db.consultar(
            "select quando, usuario, acao, alvo, detalhes from painel.auditoria "
            "where detalhes->>'token' = %s order by quando desc limit 50", (token,))
    disponiveis, erro = _modelos_disponiveis() if u["papel"] == "admin" else ([], None)
    return render(request, "chave.html", {
        "k": k, "por_modelo": por_modelo, "inicio": inicio, "fim": fim, "agendada": agendada,
        "trilha": trilha, "disponiveis": sorted(set(disponiveis) | set(k["modelos_reais"])),
        "erro_gateway": erro, "carencia": config.rotacao_carencia_dias, "validade_padrao": config.chave_validade,
    })


# ---------------------------------------------------------------------------
# Gestão (admin): as regras ficam em app/servicos/chaves.py (compartilhadas com o concierge)
# ---------------------------------------------------------------------------
@router.post("/chaves/nova")
def criar(request: Request, u: dict = Depends(auth.exigir_admin),
          alias: str = Form(""), criado_para: str = Form(""), modelos: list[str] = Form([]),
          rpm: str = Form(""), paralelas: str = Form(""), validade: str = Form("")):
    dados = {"alias": alias, "criado_para": criado_para, "modelos": modelos,
             "rpm": rpm, "paralelas": paralelas, "validade": validade}
    try:
        segredo, token, alias = servico.criar(u, auth.ip_cliente(request), alias, criado_para, modelos,
                                              rpm, paralelas, validade)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        disponiveis, erro_gateway = _modelos_disponiveis()
        return render(request, "chave_nova.html", {
            "disponiveis": disponiveis, "erro_gateway": erro_gateway, "erros": [str(e)], "form": dados,
        }, status=400)
    return render(request, "chave_criada.html", {"segredo": segredo, "token": token, "alias": alias,
                                                 "titulo": "Chave criada"})


@router.post("/chaves/limpar-expiradas")
def limpar_expiradas(request: Request, u: dict = Depends(auth.exigir_admin)):
    alteradas, falhas = servico.limpar_expiradas(u, auth.ip_cliente(request))
    if falhas:
        return redirecionar("/chaves", f"{alteradas} chave(s) limpa(s); falharam: {', '.join(falhas[:5])}.", "erro")
    return redirecionar("/chaves", f"Modelos retirados de {alteradas} chave(s) expirada(s).")


@router.post("/chaves/{token}/alterar")
def alterar(request: Request, token: str, u: dict = Depends(auth.exigir_admin),
            modelos: list[str] = Form([]), rpm: str = Form(""), paralelas: str = Form(""),
            validade: str = Form("")):
    _chave_ou_404(token)
    try:
        _, mudancas = servico.alterar(u, auth.ip_cliente(request), token, modelos, rpm, paralelas, validade)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    if not mudancas:
        return redirecionar(f"/chaves/{token}", "Nada foi alterado.", "aviso")
    return redirecionar(f"/chaves/{token}", "Chave alterada.")


@router.post("/chaves/{token}/bloquear")
def bloquear(request: Request, token: str, u: dict = Depends(auth.exigir_admin), motivo: str = Form("")):
    _chave_ou_404(token)
    try:
        servico.bloquear(u, auth.ip_cliente(request), token, motivo)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    return redirecionar(f"/chaves/{token}", "Chave bloqueada. As requisições com ela passam a ser recusadas.")


@router.post("/chaves/{token}/desbloquear")
def desbloquear(request: Request, token: str, u: dict = Depends(auth.exigir_admin)):
    _chave_ou_404(token)
    try:
        servico.desbloquear(u, auth.ip_cliente(request), token)
    except litellm.ErroLiteLLM as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    return redirecionar(f"/chaves/{token}", "Chave desbloqueada.")


@router.post("/chaves/{token}/revogar")
def revogar(request: Request, token: str, u: dict = Depends(auth.exigir_admin),
            motivo: str = Form(""), confirmacao: str = Form("")):
    _chave_ou_404(token)
    try:
        k = servico.revogar(u, auth.ip_cliente(request), token, motivo, confirmacao)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    return redirecionar("/chaves/revogadas", f"Chave {k['key_alias']} revogada.")


@router.post("/chaves/{token}/rotacionar")
def rotacionar(request: Request, token: str, u: dict = Depends(auth.exigir_admin),
               carencia: str = Form(""), validade: str = Form("")):
    _chave_ou_404(token)
    try:
        r = servico.rotacionar(u, auth.ip_cliente(request), token, carencia, validade)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    return render(request, "chave_criada.html", {
        "segredo": r["segredo"], "token": r["token_novo"], "alias": r["alias"], "titulo": "Chave rotacionada",
        "antiga": {"token": token, "alias": r["alias_antigo"], "revoga_em": r["revoga_em"]},
    })
