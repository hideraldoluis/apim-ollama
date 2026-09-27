"""Chaves de consumidores: consulta (todos) e gestão (admin)."""
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import auditoria, autenticacao as auth, consultas, db, litellm, rotinas, validacao
from ..config import config
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
    try:
        return litellm.modelos_publicados(), None
    except litellm.ErroLiteLLM as e:
        return [], str(e)


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
    return render(request, "chaves.html", {"chaves": chaves})


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
        "trilha": trilha, "disponiveis": sorted(set(disponiveis) | set(k["modelos"] or [])),
        "erro_gateway": erro, "carencia": config.rotacao_carencia_dias, "validade_padrao": config.chave_validade,
    })


# ---------------------------------------------------------------------------
# Gestão (admin)
# ---------------------------------------------------------------------------
@router.post("/chaves/nova")
def criar(request: Request, u: dict = Depends(auth.exigir_admin),
          alias: str = Form(""), criado_para: str = Form(""), modelos: list[str] = Form([]),
          rpm: str = Form(""), paralelas: str = Form(""), validade: str = Form("")):
    dados = {"alias": alias, "criado_para": criado_para, "modelos": modelos,
             "rpm": rpm, "paralelas": paralelas, "validade": validade}
    disponiveis, erro_gateway = _modelos_disponiveis()
    try:
        alias = validacao.alias(dados["alias"])
        criado_para = validacao.alias(dados["criado_para"] or dados["alias"])
        modelos = validacao.modelos(dados["modelos"], disponiveis)
        rpm = validacao.inteiro_positivo("RPM", dados["rpm"])
        paralelas = validacao.inteiro_positivo("requisições simultâneas", dados["paralelas"])
        validade = validacao.duracao(dados["validade"])
        segredo, token = litellm.gerar_chave(alias, modelos, rpm, paralelas, validade, criado_para, u["login"])
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return render(request, "chave_nova.html", {
            "disponiveis": disponiveis, "erro_gateway": erro_gateway, "erros": [str(e)], "form": dados,
        }, status=400)
    auditoria.registrar("chave_criada", u["login"], auth.ip_cliente(request), alias, {
        "token": token, "criado_para": criado_para, "modelos": modelos, "rpm": rpm,
        "paralelas": paralelas, "validade": validade})
    return render(request, "chave_criada.html", {"segredo": segredo, "token": token, "alias": alias,
                                                 "titulo": "Chave criada"})


@router.post("/chaves/{token}/alterar")
def alterar(request: Request, token: str, u: dict = Depends(auth.exigir_admin),
            modelos: list[str] = Form([]), rpm: str = Form(""), paralelas: str = Form(""),
            validade: str = Form("")):
    k = _chave_ou_404(token)
    disponiveis, _ = _modelos_disponiveis()
    mudancas: dict = {}
    try:
        # Modelos já liberados continuam aceitos mesmo que tenham saído do gateway.
        modelos = validacao.modelos(modelos, disponiveis + (k["modelos"] or []))
        if sorted(modelos) != sorted(k["modelos"] or []):
            mudancas["modelos"] = modelos
        rpm_novo = validacao.inteiro_positivo("RPM", rpm)
        if rpm_novo != k["rpm_limit"]:
            mudancas["rpm"] = rpm_novo
        paralelas_novo = validacao.inteiro_positivo("requisições simultâneas", paralelas)
        if paralelas_novo != k["max_parallel_requests"]:
            mudancas["paralelas"] = paralelas_novo
        if validade.strip():
            mudancas["validade"] = validacao.duracao(validade)
        if not mudancas:
            return redirecionar(f"/chaves/{token}", "Nada foi alterado.", "aviso")
        litellm.alterar_chave(token, **mudancas)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    antes = {"modelos": k["modelos"], "rpm": k["rpm_limit"], "paralelas": k["max_parallel_requests"],
             "expira_em": k["expira_em"].isoformat() if k["expira_em"] else None}
    auditoria.registrar("chave_alterada", u["login"], auth.ip_cliente(request), k["key_alias"],
                        {"token": token, "antes": antes, "depois": mudancas})
    return redirecionar(f"/chaves/{token}", "Chave alterada.")


@router.post("/chaves/{token}/bloquear")
def bloquear(request: Request, token: str, u: dict = Depends(auth.exigir_admin), motivo: str = Form("")):
    k = _chave_ou_404(token)
    try:
        motivo = validacao.motivo(motivo)
        litellm.bloquear(token)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    auditoria.registrar("chave_bloqueada", u["login"], auth.ip_cliente(request), k["key_alias"],
                        {"token": token, "motivo": motivo})
    return redirecionar(f"/chaves/{token}", "Chave bloqueada. As requisições com ela passam a ser recusadas.")


@router.post("/chaves/{token}/desbloquear")
def desbloquear(request: Request, token: str, u: dict = Depends(auth.exigir_admin)):
    k = _chave_ou_404(token)
    try:
        litellm.desbloquear(token)
    except litellm.ErroLiteLLM as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    auditoria.registrar("chave_desbloqueada", u["login"], auth.ip_cliente(request), k["key_alias"], {"token": token})
    return redirecionar(f"/chaves/{token}", "Chave desbloqueada.")


@router.post("/chaves/{token}/revogar")
def revogar(request: Request, token: str, u: dict = Depends(auth.exigir_admin),
            motivo: str = Form(""), confirmacao: str = Form("")):
    k = _chave_ou_404(token)
    try:
        motivo = validacao.motivo(motivo)
        if confirmacao.strip() != (k["key_alias"] or ""):
            raise validacao.Invalido("Para revogar, digite o nome da chave exatamente como aparece.")
        rotinas.revogar_com_historico(token, u["login"], auth.ip_cliente(request), motivo)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    db.executar("update painel.revogacoes_agendadas set executada_em = now() where token = %s and executada_em is null",
                (token,))
    return redirecionar("/chaves/revogadas", f"Chave {k['key_alias']} revogada.")


@router.post("/chaves/{token}/rotacionar")
def rotacionar(request: Request, token: str, u: dict = Depends(auth.exigir_admin),
               carencia: str = Form(""), validade: str = Form("")):
    k = _chave_ou_404(token)
    ip = auth.ip_cliente(request)
    try:
        dias = int(carencia) if carencia.strip().isdigit() else -1
        if not 0 <= dias <= 30:
            raise validacao.Invalido("Carência inválida: use de 0 a 30 dias.")
        validade = validacao.duracao(validade or config.chave_validade)
        alias = k["key_alias"] or k["criado_para"] or token[:12]
    except validacao.Invalido as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")

    # O alias é único: a chave antiga recebe um sufixo antes da nova ser criada com o nome original.
    alias_antigo = f"{alias} (substituída {datetime.now(config.tz):%Y-%m-%d %H%M})"
    try:
        litellm.alterar_chave(token, alias=alias_antigo)
    except litellm.ErroLiteLLM as e:
        return redirecionar(f"/chaves/{token}", str(e), "erro")
    try:
        segredo, novo = litellm.gerar_chave(alias, k["modelos"] or [], k["rpm_limit"] or config.chave_rpm,
                                            k["max_parallel_requests"] or config.chave_paralelas, validade,
                                            k["criado_para"] or alias, u["login"])
    except litellm.ErroLiteLLM as e:
        try:
            litellm.alterar_chave(token, alias=alias)
        except litellm.ErroLiteLLM:
            pass
        return redirecionar(f"/chaves/{token}", str(e), "erro")

    motivo = f"Rotação: substituída pela chave {novo[:12]}…"
    auditoria.registrar("chave_rotacionada", u["login"], ip, alias,
                        {"token": token, "token_novo": novo, "carencia_dias": dias, "validade": validade})
    auditoria.registrar("chave_criada", u["login"], ip, alias,
                        {"token": novo, "rotacao_de": token, "modelos": k["modelos"], "validade": validade})
    if dias == 0:
        try:
            rotinas.revogar_com_historico(token, u["login"], ip, motivo)
        except litellm.ErroLiteLLM:
            dias = None  # cai no agendamento abaixo; a rotina tenta de novo
    if dias != 0:
        db.executar(
            "insert into painel.revogacoes_agendadas (token, key_alias, executar_em, agendada_por, motivo) "
            "values (%s, %s, now() + make_interval(days => %s), %s, %s) on conflict (token) do nothing",
            (token, alias_antigo, dias or 0, u["login"], motivo))
    return render(request, "chave_criada.html", {
        "segredo": segredo, "token": novo, "alias": alias, "titulo": "Chave rotacionada",
        "antiga": {"token": token, "alias": alias_antigo,
                   "revoga_em": None if dias == 0 else datetime.now(config.tz) + timedelta(days=dias or 0)},
    })
