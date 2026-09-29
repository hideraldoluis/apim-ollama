"""Chat: conversa com um modelo publicado no gateway, direto do painel, para testar respostas e desempenho.

Usa a master key (mesmo cliente de app/litellm.py), então a conversa não passa pelos limites de
uma chave sk- (RPM, paralelismo, allowlist por chave) — é uma via de teste, não a API pública.
A conversa em si não é guardada pelo painel: cada mensagem é só mais uma chamada ao gateway, que
aparece no consumo e nos logs como qualquer outra requisição.

O painel não repassa os chunks do gateway: traduz para eventos próprios (Tradutor), para que a tela
nunca fique sem retorno — erro no meio do stream, resposta vazia e espera longa (modelo carregando)
viram eventos explícitos.
"""
import asyncio
import json
import math
import time

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import auditoria, autenticacao as auth, litellm, ollama, validacao
from ..config import config
from ..web import render

router = APIRouter(prefix="/chat")

_MAX_MENSAGENS = 40
_MAX_CONTEUDO = 8000
_MAX_TOKENS = 32768
# A primeira chamada inclui a carga do modelo (minutos em modelos grandes): mesmo teto do
# request_timeout do LiteLLM. Enquanto nada chega, a tela recebe um evento "aguardando" a cada 5 s.
_TIMEOUT_LEITURA = 600.0
_INTERVALO_AGUARDANDO = 5.0


def _modelos_chat() -> list[dict]:
    """Modelos de chat publicados, com os detalhes do Ollama e se estão carregados agora."""
    publicados = [p for p in litellm.modelos_gateway() if not p["embedding"]]
    try:
        instalados = {m["nome"]: m for m in ollama.instalados()}
        carregados = ollama.carregados()
    except ollama.ErroOllama:
        instalados, carregados = {}, {}
    modelos = []
    for p in publicados:
        alvo = ollama.nome_completo(p["ollama"]) if p["ollama"] else None
        info = instalados.get(alvo) or {}
        modelos.append({
            "nome": p["nome"], "ollama": alvo, "carregado": alvo in carregados,
            "parametros": info.get("parametros"), "quantizacao": info.get("quantizacao"),
            "contexto": info.get("contexto"),
        })
    return sorted(modelos, key=lambda m: m["nome"] or "")


def _validar_mensagens(bruto) -> list[dict]:
    if not isinstance(bruto, list) or not bruto:
        raise validacao.Invalido("Envie ao menos uma mensagem.")
    if len(bruto) > _MAX_MENSAGENS:
        raise validacao.Invalido(f"Conversa longa demais (máximo {_MAX_MENSAGENS} mensagens). Limpe a conversa.")
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


def _validar_parametros(temperatura: str, max_tokens: str, seed: str) -> dict:
    """Parâmetros opcionais de geração; vazio = padrão do modelo."""
    parametros = {}
    if temperatura.strip():
        try:
            valor = float(temperatura.replace(",", "."))
        except ValueError:
            valor = math.nan
        if not 0 <= valor <= 2:
            raise validacao.Invalido("Temperatura inválida: use um número entre 0 e 2.")
        parametros["temperature"] = valor
    if max_tokens.strip():
        valor = validacao.inteiro_positivo("máximo de tokens", max_tokens)
        if valor > _MAX_TOKENS:
            raise validacao.Invalido(f"Máximo de tokens inválido: use até {_MAX_TOKENS}.")
        parametros["max_tokens"] = valor
    if seed.strip():
        parametros["seed"] = validacao.inteiro_positivo("seed", seed)
    return parametros


def _evento(tipo: str, **dados) -> str:
    return f"data: {json.dumps({'tipo': tipo, **dados}, ensure_ascii=False)}\n\n"


def _detalhe_erro(bruto: str) -> str:
    """Extrai a mensagem de um corpo de erro do gateway ({"error": {"message": ...}} ou texto)."""
    try:
        dados = json.loads(bruto)
    except ValueError:
        return bruto.strip()[:300]
    erro = dados.get("error", dados) if isinstance(dados, dict) else dados
    if isinstance(erro, dict):
        erro = erro.get("message") or erro.get("detail") or erro
    return str(erro)[:300]


class Tradutor:
    """Converte os chunks do gateway (formato OpenAI) em eventos do painel e mede o desempenho.

    Eventos: status (fase), raciocinio, delta, erro e, ao final, metricas. Separado da rota para ser
    testado sem gateway (painel/tests/test_chat.py).
    """

    def __init__(self, inicio: float, frio: bool):
        self.inicio = inicio
        self.frio = frio
        self.primeiro: float | None = None
        self.fase: str | None = None
        self.texto = False
        self.erro = False
        self.uso: dict | None = None
        self.fim_motivo: str | None = None

    def _marcar(self, agora: float, fase: str) -> list[str]:
        if self.primeiro is None:
            self.primeiro = agora
        if fase == self.fase:
            return []
        self.fase = fase
        return [_evento("status", fase=fase)]

    def chunk(self, dados, agora: float) -> list[str]:
        if not isinstance(dados, dict):
            return []
        if "error" in dados:
            self.erro = True
            return [_evento("erro", mensagem=f"O gateway interrompeu a resposta: {_detalhe_erro(json.dumps(dados))}")]
        eventos = []
        if isinstance(dados.get("usage"), dict):
            self.uso = dados["usage"]
        for escolha in dados.get("choices") or []:
            delta = escolha.get("delta") or {}
            raciocinio = delta.get("reasoning_content") or ""
            conteudo = delta.get("content") or ""
            if raciocinio:
                eventos += self._marcar(agora, "pensando")
                eventos.append(_evento("raciocinio", texto=raciocinio))
            if conteudo:
                eventos += self._marcar(agora, "escrevendo")
                self.texto = True
                eventos.append(_evento("delta", texto=conteudo))
            if escolha.get("finish_reason"):
                self.fim_motivo = escolha["finish_reason"]
        return eventos

    def finalizar(self, agora: float) -> list[str]:
        eventos = []
        if not self.texto and not self.erro:
            eventos.append(_evento("erro", mensagem="O modelo terminou sem devolver texto. Tente de novo ou "
                                                     "verifique o modelo na tela Modelos."))
        total = agora - self.inicio
        metricas = {"total_ms": round(total * 1000), "frio": self.frio, "fim": self.fim_motivo}
        if self.primeiro is not None:
            metricas["ttft_ms"] = round((self.primeiro - self.inicio) * 1000)
        if self.uso:
            saida = self.uso.get("completion_tokens")
            metricas["tokens_entrada"] = self.uso.get("prompt_tokens")
            metricas["tokens_saida"] = saida
            geracao = agora - (self.primeiro or self.inicio)
            if saida and geracao > 0:
                metricas["tokens_s"] = round(saida / geracao, 1)
        eventos.append(_evento("metricas", **metricas))
        return eventos


@router.get("")
def pagina(request: Request, u: dict = Depends(auth.usuario_logado)):
    try:
        modelos, erro = _modelos_chat(), None
    except litellm.ErroLiteLLM as e:
        modelos, erro = [], str(e)
    return render(request, "chat.html", {"modelos": modelos, "erro": erro})


@router.get("/estado.json")
def estado(request: Request, u: dict = Depends(auth.usuario_logado)):
    """Se cada modelo está carregado agora (a tela atualiza o selo depois de cada resposta)."""
    try:
        return JSONResponse({m["nome"]: m["carregado"] for m in _modelos_chat()})
    except litellm.ErroLiteLLM as e:
        return JSONResponse({"erro": str(e)}, status_code=502)


@router.post("/aquecer")
def aquecer(request: Request, u: dict = Depends(auth.exigir_admin), modelo: str = Form("")):
    """Carrega o modelo na memória antes do teste, para a primeira resposta não incluir a carga."""
    alvo = next((m for m in _modelos_chat() if m["nome"] == modelo), None)
    if alvo is None or not alvo["ollama"]:
        return JSONResponse({"erro": "Modelo não disponível. Recarregue a página."}, status_code=400)
    inicio = time.monotonic()
    try:
        ollama.carregar(alvo["ollama"], embedding=False)
    except ollama.ErroOllama as e:
        return JSONResponse({"erro": str(e)}, status_code=502)
    auditoria.registrar("modelo_carregado", u["login"], auth.ip_cliente(request), alvo["ollama"], {"via": "chat"})
    return JSONResponse({"ok": True, "segundos": round(time.monotonic() - inicio, 1)})


@router.post("/enviar")
async def enviar(request: Request, u: dict = Depends(auth.usuario_logado),
                 modelo: str = Form(...), mensagens: str = Form(...), temperatura: str = Form(""),
                 max_tokens: str = Form(""), seed: str = Form("")):
    try:
        disponiveis = await asyncio.to_thread(_modelos_chat)
    except litellm.ErroLiteLLM as e:
        return JSONResponse({"erro": str(e)}, status_code=502)
    alvo = next((m for m in disponiveis if m["nome"] == modelo), None)
    if alvo is None:
        return JSONResponse({"erro": "Modelo não disponível. Recarregue a página."}, status_code=400)
    try:
        lista = _validar_mensagens(json.loads(mensagens))
        parametros = _validar_parametros(temperatura, max_tokens, seed)
    except (ValueError, validacao.Invalido) as e:
        return JSONResponse({"erro": str(e) or "Mensagens inválidas."}, status_code=400)

    corpo = {"model": modelo, "messages": lista, "stream": True,
             "stream_options": {"include_usage": True}, **parametros}

    async def gerar():
        inicio = time.monotonic()
        frio = not alvo["carregado"]
        tradutor = Tradutor(inicio, frio)
        yield _evento("status", fase="carregando" if frio else "aguardando")
        fila: asyncio.Queue = asyncio.Queue()

        async def ler():
            # Produz ("linha", str), ("http", (status, corpo)) ou ("falha", mensagem); None ao terminar.
            try:
                async with httpx.AsyncClient(
                        base_url=config.litellm_url,
                        headers={"Authorization": f"Bearer {config.litellm_master_key}"},
                        timeout=httpx.Timeout(20.0, read=_TIMEOUT_LEITURA)) as cliente:
                    async with cliente.stream("POST", "/v1/chat/completions", json=corpo) as resp:
                        if resp.status_code >= 400:
                            bruto = (await resp.aread()).decode(errors="replace")
                            await fila.put(("http", (resp.status_code, bruto)))
                            return
                        async for linha in resp.aiter_lines():
                            await fila.put(("linha", linha))
            except httpx.ReadTimeout:
                await fila.put(("falha", f"O modelo não respondeu em {int(_TIMEOUT_LEITURA)} s."))
            except httpx.HTTPError as e:
                await fila.put(("falha", f"Gateway indisponível ({e.__class__.__name__})."))
            finally:
                await fila.put(None)

        # Se o navegador desconectar (botão Parar), o gerador é cancelado e o finally fecha o stream
        # com o gateway, que interrompe a geração no Ollama.
        leitor = asyncio.create_task(ler())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(fila.get(), timeout=_INTERVALO_AGUARDANDO)
                except asyncio.TimeoutError:
                    yield _evento("aguardando", segundos=round(time.monotonic() - inicio))
                    continue
                if item is None:
                    break
                tipo, valor = item
                if tipo == "http":
                    status, bruto = valor
                    tradutor.erro = True
                    yield _evento("erro", mensagem=f"O gateway recusou a requisição ({status}): {_detalhe_erro(bruto)}")
                elif tipo == "falha":
                    tradutor.erro = True
                    yield _evento("erro", mensagem=valor)
                elif valor.startswith("data:"):
                    payload = valor[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        dados = json.loads(payload)
                    except ValueError:
                        continue
                    for evento in tradutor.chunk(dados, time.monotonic()):
                        yield evento
            for evento in tradutor.finalizar(time.monotonic()):
                yield evento
            yield _evento("fim")
        finally:
            leitor.cancel()

    return StreamingResponse(gerar(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
