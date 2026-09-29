"""Teste do chat sem serviços externos: tradutor de stream e rota /chat/enviar com um gateway falso.

Cobre os casos em que a tela ficava sem resposta: erro no meio do stream, stream sem texto, modelo
só com raciocínio, gateway recusando a requisição, espera longa (modelo carregando) e queda de conexão.
  python painel/tests/test_chat.py      (a partir da raiz do repositório; precisa de painel/requirements.txt)
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

for nome, valor in (("PAINEL_DSN_APP", "postgresql://x@localhost/x"), ("PAINEL_DSN_CONTEUDO", "postgresql://x@localhost/x"),
                    ("LITELLM_MASTER_KEY", "sk-teste")):
    os.environ.setdefault(nome, valor)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rotas import chat  # noqa: E402

falhas = 0


def ok(cond, msg):
    global falhas
    print(("OK    " if cond else "FALHA ") + msg)
    falhas += 0 if cond else 1


def eventos(linhas: list[str]) -> list[dict]:
    return [json.loads(e[len("data: "):]) for e in linhas]


def sse(*chunks) -> bytes:
    return "".join(f"data: {json.dumps(c) if not isinstance(c, str) else c}\n\n" for c in chunks).encode()


def delta(texto="", raciocinio="", fim=None):
    d = {}
    if texto:
        d["content"] = texto
    if raciocinio:
        d["reasoning_content"] = raciocinio
    return {"choices": [{"delta": d, "finish_reason": fim}]}


# --- Tradutor -------------------------------------------------------------------------------------
t = chat.Tradutor(inicio=0.0, frio=True)
ev = eventos(t.chunk(delta("Olá"), 2.0) + t.chunk(delta(" mundo", fim="stop"), 3.0)
             + t.chunk({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}}, 3.0)
             + t.finalizar(4.0))
ok([e["tipo"] for e in ev] == ["status", "delta", "delta", "metricas"], "texto normal: status, deltas e métricas")
m = ev[-1]
ok(m["ttft_ms"] == 2000 and m["total_ms"] == 4000 and m["tokens_saida"] == 20 and m["tokens_s"] == 10.0,
   "métricas: TTFT, total e tokens/s medidos a partir do 1º token")
ok(m["frio"] is True and m["fim"] == "stop", "métricas informam partida a frio e motivo do fim")

t = chat.Tradutor(0.0, False)
ev = eventos(t.chunk(delta(raciocinio="hmm"), 1.0) + t.chunk(delta("42"), 2.0) + t.finalizar(2.0))
ok([(e["tipo"], e.get("fase")) for e in ev][:4] == [("status", "pensando"), ("raciocinio", None),
                                                     ("status", "escrevendo"), ("delta", None)],
   "raciocínio (reasoning_content) vira evento próprio, não é descartado")

t = chat.Tradutor(0.0, False)
ev = eventos(t.chunk(delta(raciocinio="só pensei"), 1.0) + t.finalizar(2.0))
ok(any(e["tipo"] == "erro" for e in ev), "stream sem texto final gera erro explícito")

t = chat.Tradutor(0.0, False)
ev = eventos(t.chunk(delta("parcial"), 1.0) + t.chunk({"error": {"message": "CUDA out of memory"}}, 2.0) + t.finalizar(2.0))
erros = [e for e in ev if e["tipo"] == "erro"]
ok(len(erros) == 1 and "CUDA out of memory" in erros[0]["mensagem"], "erro no meio do stream chega à tela com a mensagem")

t = chat.Tradutor(0.0, False)
ev = eventos(t.finalizar(1.0))
ok(ev[0]["tipo"] == "erro" and "ttft_ms" not in ev[-1], "stream vazio: erro e métricas sem TTFT")

# --- Parâmetros -----------------------------------------------------------------------------------
ok(chat._validar_parametros("", "", "") == {}, "parâmetros vazios usam o padrão do modelo")
ok(chat._validar_parametros("0,7", "256", "42") == {"temperature": 0.7, "max_tokens": 256, "seed": 42},
   "parâmetros válidos (vírgula decimal aceita)")
for args, nome in ((("3", "", ""), "temperatura > 2"), (("abc", "", ""), "temperatura não numérica"),
                   (("nan", "", ""), "temperatura NaN"), (("", "0", ""), "máx. tokens zero"),
                   (("", "999999", ""), "máx. tokens acima do teto"), (("", "", "-1"), "seed negativa")):
    try:
        chat._validar_parametros(*args)
        ok(False, f"{nome} recusado")
    except chat.validacao.Invalido:
        ok(True, f"{nome} recusado")


# --- Rota /chat/enviar com gateway falso ------------------------------------------------------------
MODELO = {"nome": "llama3.3", "ollama": "llama3.3:latest", "carregado": False,
          "parametros": "70.6B", "quantizacao": "Q4_K_M", "contexto": 131072}
chat._modelos_chat = lambda: [MODELO]
_AsyncClient = httpx.AsyncClient
recebido = {}


def usar_gateway(tratar):
    async def handler(req: httpx.Request):
        recebido["corpo"] = json.loads(req.content)
        return await tratar(req)
    chat.httpx.AsyncClient = lambda **kw: _AsyncClient(transport=httpx.MockTransport(handler), **kw)


async def conversar(**extra) -> list[dict]:
    campos = {"modelo": "llama3.3", "mensagens": json.dumps([{"role": "user", "content": "oi"}]),
              "temperatura": "", "max_tokens": "", "seed": "", **extra}
    resp = await chat.enviar(request=None, u={"login": "teste"}, **campos)
    if not hasattr(resp, "body_iterator"):
        return [{"tipo": "http", "status": resp.status_code, **json.loads(resp.body)}]
    return eventos([e async for e in resp.body_iterator])


async def normal(req):
    return httpx.Response(200, content=sse(delta("Olá!", fim="stop"),
                                           {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
                                           "[DONE]"), headers={"content-type": "text/event-stream"})


async def recusa(req):
    return httpx.Response(400, json={"error": {"message": "context length exceeded"}})


async def lento(req):
    await asyncio.sleep(0.35)
    return await normal(req)


async def queda(req):
    raise httpx.ConnectError("sem rota")


async def principal():
    usar_gateway(normal)
    ev = await conversar(temperatura="0.2", max_tokens="100")
    tipos = [e["tipo"] for e in ev]
    ok(tipos[0] == "status" and ev[0]["fase"] == "carregando", "modelo fora da memória: tela avisa que está carregando")
    ok("delta" in tipos and tipos[-2:] == ["metricas", "fim"], "rota entrega texto, métricas e fim")
    ok(ev[-2].get("tokens_saida") == 3 and ev[-2]["frio"] is True, "métricas com uso de tokens do gateway")
    corpo = recebido["corpo"]
    ok(corpo["stream_options"] == {"include_usage": True} and corpo["temperature"] == 0.2 and corpo["max_tokens"] == 100,
       "requisição pede uso de tokens e leva os parâmetros")

    usar_gateway(recusa)
    ev = await conversar()
    ok(any(e["tipo"] == "erro" and "context length exceeded" in e["mensagem"] for e in ev) and ev[-1]["tipo"] == "fim",
       "gateway recusando: erro legível e stream encerrado")
    ok(sum(e["tipo"] == "erro" for e in ev) == 1, "recusa não gera um segundo erro de 'sem texto'")

    usar_gateway(queda)
    ev = await conversar()
    ok(any(e["tipo"] == "erro" and "Gateway indisponível" in e["mensagem"] for e in ev), "queda de conexão vira erro")

    usar_gateway(lento)
    chat._INTERVALO_AGUARDANDO = 0.1
    ev = await conversar()
    ok(any(e["tipo"] == "aguardando" for e in ev) and "delta" in [e["tipo"] for e in ev],
       "espera longa emite 'aguardando' (mantém a conexão e a tela viva) e depois responde")

    ev = await conversar(modelo="outro")
    ok(ev[0]["tipo"] == "http" and ev[0]["status"] == 400, "modelo não publicado recusado")
    ev = await conversar(max_tokens="0")
    ok(ev[0]["tipo"] == "http" and ev[0]["status"] == 400, "parâmetro inválido recusado antes de chamar o gateway")
    ev = await conversar(mensagens=json.dumps([{"role": "tool", "content": "x"}]))
    ok(ev[0]["tipo"] == "http" and ev[0]["status"] == 400, "papel de mensagem inválido recusado")


asyncio.run(principal())
print(f"\n{falhas} falha(s)")
sys.exit(1 if falhas else 0)
