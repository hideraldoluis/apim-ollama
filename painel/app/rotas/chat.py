"""Chat: conversa com um modelo publicado no gateway, direto do painel.

Usa a master key (mesmo cliente de app/litellm.py), então a conversa não passa pelos limites de
uma chave sk- (RPM, paralelismo, allowlist por chave) — é uma via de teste, não a API pública.
A conversa em si não é guardada pelo painel: cada mensagem é só mais uma chamada ao gateway, que
aparece no consumo e nos logs como qualquer outra requisição.
"""
import json

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import autenticacao as auth, litellm, validacao
from ..config import config
from ..web import render

router = APIRouter(prefix="/chat")

_MAX_MENSAGENS = 40
_MAX_CONTEUDO = 8000


def _modelos_chat() -> list[str]:
    return sorted(p["nome"] for p in litellm.modelos_gateway() if not p["embedding"])


def _validar_mensagens(bruto) -> list[dict]:
    if not isinstance(bruto, list) or not bruto:
        raise validacao.Invalido("Envie ao menos uma mensagem.")
    if len(bruto) > _MAX_MENSAGENS:
        raise validacao.Invalido(f"Conversa longa demais (máximo {_MAX_MENSAGENS} mensagens).")
    mensagens = []
    for m in bruto:
        papel = m.get("role") if isinstance(m, dict) else None
        conteudo = m.get("content") if isinstance(m, dict) else None
        if papel not in ("system", "user", "assistant"):
            raise validacao.Invalido("Mensagem inválida.")
        if not isinstance(conteudo, str) or not conteudo.strip():
            raise validacao.Invalido("Mensagem vazia.")
        if len(conteudo) > _MAX_CONTEUDO:
            raise validacao.Invalido(f"Mensagem longa demais (máximo {_MAX_CONTEUDO} caracteres).")
        mensagens.append({"role": papel, "content": conteudo})
    return mensagens


def _evento_erro(mensagem: str) -> str:
    return f"data: {json.dumps({'erro': mensagem})}\n\n"


@router.get("")
def pagina(request: Request, u: dict = Depends(auth.usuario_logado)):
    return render(request, "chat.html", {"modelos": _modelos_chat()})


@router.post("/enviar")
async def enviar(request: Request, u: dict = Depends(auth.usuario_logado),
                  modelo: str = Form(...), mensagens: str = Form(...)):
    if modelo not in _modelos_chat():
        return JSONResponse({"erro": "Modelo não disponível. Recarregue a página."}, status_code=400)
    try:
        lista = _validar_mensagens(json.loads(mensagens))
    except (ValueError, validacao.Invalido) as e:
        return JSONResponse({"erro": str(e) or "Mensagens inválidas."}, status_code=400)

    async def gerar():
        try:
            async with httpx.AsyncClient(base_url=config.litellm_url,
                                         headers={"Authorization": f"Bearer {config.litellm_master_key}"},
                                         timeout=httpx.Timeout(20.0, read=300.0)) as cliente:
                async with cliente.stream("POST", "/v1/chat/completions",
                                          json={"model": modelo, "messages": lista, "stream": True}) as resp:
                    if resp.status_code >= 400:
                        detalhe = (await resp.aread()).decode(errors="replace")[:300]
                        yield _evento_erro(f"O gateway recusou a operação ({resp.status_code}): {detalhe}")
                        return
                    async for linha in resp.aiter_lines():
                        if not linha.startswith("data:"):
                            continue
                        payload = linha[len("data:"):].strip()
                        if payload == "[DONE]":
                            break
                        yield f"data: {payload}\n\n"
        except httpx.HTTPError as e:
            yield _evento_erro(f"Gateway indisponível ({e.__class__.__name__}).")

    return StreamingResponse(gerar(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
