"""Concierge: chat em linguagem natural (na home) que opera o painel, e sua configuração (admin).

- POST /concierge/enviar            SSE: uma rodada de conversa (ferramentas de leitura executam; escrita só propõe)
- POST /concierge/confirmar/{id}    executa uma ação proposta (cookie + CSRF + mesmo usuário), com as regras das telas
- POST /concierge/cancelar/{id}
- GET/POST /concierge/configuracao  modelo, limite e liga/desliga (admin); o modelo fica no banco, não no .env

Todo usuário logado usa o concierge; o leitor só recebe ferramentas de leitura (validado no servidor).
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import autenticacao as auth, litellm, ollama, validacao
from ..concierge import acoes, agente, configuracao, limites
from ..web import redirecionar, render

router = APIRouter(prefix="/concierge")

MAX_MENSAGENS = 20
MAX_CONTEUDO = 8000


def validar_historico(bruto) -> list[dict]:
    """Aceita só mensagens user/assistant em texto; a última precisa ser do usuário."""
    if not isinstance(bruto, list) or not bruto:
        raise validacao.Invalido("Envie ao menos uma mensagem.")
    if len(bruto) > MAX_MENSAGENS:
        raise validacao.Invalido("Conversa longa demais. Comece uma nova conversa.")
    mensagens = []
    for m in bruto:
        papel = m.get("role") if isinstance(m, dict) else None
        conteudo = m.get("content") if isinstance(m, dict) else None
        if papel not in ("user", "assistant") or not isinstance(conteudo, str) or not conteudo.strip():
            raise validacao.Invalido("Mensagem inválida.")
        if len(conteudo) > MAX_CONTEUDO:
            raise validacao.Invalido(f"Mensagem longa demais (máximo {MAX_CONTEUDO} caracteres).")
        mensagens.append({"role": papel, "content": conteudo})
    if mensagens[-1]["role"] != "user":
        raise validacao.Invalido("A última mensagem deve ser do usuário.")
    return mensagens


def _modelo_pronto(cfg: dict) -> tuple[str | None, bool]:
    """(erro, frio): confere que o modelo configurado ainda está publicado e se está carregado na memória."""
    alvo = next((p for p in litellm.modelos_gateway() if p["nome"] == cfg["modelo"]), None)
    if alvo is None or alvo["embedding"]:
        return f"O modelo do concierge ({cfg['modelo']}) não está mais publicado. Um administrador deve revisar a configuração.", False
    try:
        frio = ollama.nome_completo(alvo["ollama"]) not in ollama.carregados()
    except ollama.ErroOllama:
        frio = False
    return None, frio


@router.post("/enviar")
async def enviar(request: Request, u: dict = Depends(auth.usuario_logado), mensagens: str = Form(...)):
    cfg = await asyncio.to_thread(configuracao.obter)
    if not cfg["ativo"] or not cfg["modelo"]:
        return JSONResponse({"erro": "O concierge não está ativo."}, status_code=409)
    try:
        historico = validar_historico(json.loads(mensagens))
    except (ValueError, validacao.Invalido) as e:
        return JSONResponse({"erro": str(e) or "Mensagens inválidas."}, status_code=400)
    ip = auth.ip_cliente(request)

    async def gerar():
        # A vaga é reservada aqui dentro: se o cliente sumir antes de ler o stream, o finally a devolve.
        try:
            limites.iniciar(u["id"], cfg["limite_msg_min"])
        except limites.LimiteExcedido as e:
            yield agente.evento("erro", mensagem=str(e))
            yield agente.evento("fim")
            return
        try:
            try:
                erro, frio = await asyncio.to_thread(_modelo_pronto, cfg)
            except litellm.ErroLiteLLM as e:
                erro, frio = str(e), False
            if erro:
                yield agente.evento("erro", mensagem=erro)
                yield agente.evento("fim")
                return
            async for e in agente.conversar(u, ip, cfg["modelo"], historico, frio):
                yield e
        finally:
            limites.liberar(u["id"])

    return StreamingResponse(gerar(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/confirmar/{acao_id}")
def confirmar(request: Request, acao_id: str, u: dict = Depends(auth.usuario_logado),
              motivo: str = Form(""), confirmacao: str = Form("")):
    try:
        return JSONResponse(acoes.confirmar(u, auth.ip_cliente(request), acao_id, motivo, confirmacao))
    except acoes.AcaoInvalida as e:
        return JSONResponse({"ok": False, "mensagem": str(e)}, status_code=410)


@router.post("/cancelar/{acao_id}")
def cancelar(request: Request, acao_id: str, u: dict = Depends(auth.usuario_logado)):
    try:
        acoes.cancelar(u, auth.ip_cliente(request), acao_id)
    except acoes.AcaoInvalida as e:
        return JSONResponse({"ok": False, "mensagem": str(e)}, status_code=410)
    return JSONResponse({"ok": True, "mensagem": "Ação cancelada."})


@router.get("/configuracao")
def configuracao_form(request: Request, u: dict = Depends(auth.exigir_admin)):
    erro, elegiveis = None, []
    try:
        elegiveis = configuracao.modelos_elegiveis()
    except (litellm.ErroLiteLLM, ollama.ErroOllama) as e:
        erro = str(e)
    return render(request, "concierge_configuracao.html", {
        "cfg": configuracao.obter(), "elegiveis": elegiveis, "erro": erro,
        "limite_min": configuracao.LIMITE_MIN, "limite_max": configuracao.LIMITE_MAX})


@router.post("/configuracao")
def configuracao_salvar(request: Request, u: dict = Depends(auth.exigir_admin), modelo: str = Form(""),
                        ativo: str = Form(""), limite_msg_min: str = Form("")):
    try:
        configuracao.salvar(u, auth.ip_cliente(request), modelo, ativo == "1", limite_msg_min)
    except (validacao.Invalido, litellm.ErroLiteLLM, ollama.ErroOllama) as e:
        return redirecionar("/concierge/configuracao", str(e), "erro")
    return redirecionar("/concierge/configuracao", "Configuração do concierge salva.")
