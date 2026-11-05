"""Laço do concierge: modelo -> ferramentas -> modelo, emitindo eventos SSE para a tela.

Eventos (JSON em "data:"): status, aguardando, delta, ferramenta, link, proposta, erro, metricas, fim.
O modelo é chamado em streaming pelo gateway (master key). Ferramentas de leitura executam na hora; as de escrita
só geram uma proposta (acoes.propor): a execução depende de o usuário confirmar na tela.
"""
import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable

import httpx

from .. import litellm, ollama, validacao
from ..config import config
from ..servicos import NaoEncontrado, RegraViolada
from . import acoes, ferramentas, prompt

log = logging.getLogger("painel.concierge")

MAX_PASSOS = 6
MAX_RESULTADO = 4000          # caracteres por resultado de ferramenta (o contexto do modelo é pequeno)
TIMEOUT_LEITURA = 600.0       # a primeira chamada pode incluir a carga do modelo
INTERVALO_AGUARDANDO = 5.0
_ERROS = (validacao.Invalido, RegraViolada, NaoEncontrado, litellm.ErroLiteLLM, ollama.ErroOllama,
          acoes.AcaoInvalida)


def evento(tipo: str, **dados) -> str:
    return f"data: {json.dumps({'tipo': tipo, **dados}, ensure_ascii=False, default=str)}\n\n"


def _detalhe_erro(bruto: str) -> str:
    try:
        dados = json.loads(bruto)
    except ValueError:
        return bruto.strip()[:300]
    erro = dados.get("error", dados) if isinstance(dados, dict) else dados
    if isinstance(erro, dict):
        erro = erro.get("message") or erro.get("detail") or erro
    return str(erro)[:300]


class Acumulador:
    """Junta os chunks de uma resposta: texto (emitido como delta) e chamadas de ferramenta (acumuladas)."""

    def __init__(self):
        self.texto = ""
        self.chamadas: dict[int, dict] = {}
        self.erro: str | None = None
        self.pensando = False

    def chunk(self, dados) -> list[str]:
        if not isinstance(dados, dict):
            return []
        if "error" in dados:
            self.erro = f"O gateway interrompeu a resposta: {_detalhe_erro(json.dumps(dados))}"
            return []
        eventos = []
        for escolha in dados.get("choices") or []:
            delta = escolha.get("delta") or {}
            if delta.get("reasoning_content") and not self.pensando:
                self.pensando = True
                eventos.append(evento("status", fase="pensando"))
            if delta.get("content"):
                self.texto += delta["content"]
                eventos.append(evento("delta", texto=delta["content"]))
            for posicao, tc in enumerate(delta.get("tool_calls") or []):
                i = tc.get("index", posicao)
                atual = self.chamadas.setdefault(i, {"id": None, "nome": "", "args": ""})
                atual["id"] = tc.get("id") or atual["id"]
                fn = tc.get("function") or {}
                atual["nome"] += fn.get("name") or ""
                arg = fn.get("arguments")
                if isinstance(arg, dict):
                    arg = json.dumps(arg, ensure_ascii=False)
                atual["args"] += arg or ""
        return eventos

    def lista(self) -> list[dict]:
        return [{"id": c["id"] or f"chamada_{i}", "nome": c["nome"], "args": c["args"]}
                for i, c in sorted(self.chamadas.items())]


async def stream_gateway(corpo: dict) -> AsyncIterator[tuple[str, object]]:
    """Chama o gateway e produz ("linha", str), ("espera", s), ("http", (status, corpo)) ou ("falha", msg)."""
    fila: asyncio.Queue = asyncio.Queue()
    inicio = time.monotonic()

    async def ler():
        try:
            async with httpx.AsyncClient(
                    base_url=config.litellm_url, headers={"Authorization": f"Bearer {config.litellm_master_key}"},
                    timeout=httpx.Timeout(20.0, read=TIMEOUT_LEITURA)) as cliente:
                async with cliente.stream("POST", "/v1/chat/completions", json=corpo) as resp:
                    if resp.status_code >= 400:
                        await fila.put(("http", (resp.status_code, (await resp.aread()).decode(errors="replace"))))
                        return
                    async for linha in resp.aiter_lines():
                        await fila.put(("linha", linha))
        except httpx.ReadTimeout:
            await fila.put(("falha", f"O modelo não respondeu em {int(TIMEOUT_LEITURA)} s."))
        except httpx.HTTPError as e:
            await fila.put(("falha", f"Gateway indisponível ({e.__class__.__name__})."))
        finally:
            await fila.put(None)

    tarefa = asyncio.create_task(ler())
    try:
        while True:
            try:
                item = await asyncio.wait_for(fila.get(), timeout=INTERVALO_AGUARDANDO)
            except asyncio.TimeoutError:
                yield "espera", round(time.monotonic() - inicio)
                continue
            if item is None:
                return
            yield item
    finally:
        tarefa.cancel()  # navegador desconectou (Parar): fecha o stream e o gateway interrompe a geração


def _cortar(texto: str) -> str:
    return texto if len(texto) <= MAX_RESULTADO else texto[:MAX_RESULTADO - 30] + " … [resultado truncado]"


async def _usar_ferramenta(ator: dict, ip: str | None, chamada: dict, ja_propostas: set) -> tuple[str, list[str]]:
    """Executa (leitura) ou propõe (escrita) uma chamada. Devolve (texto para o modelo, eventos para a tela)."""
    nome, id_ = chamada["nome"], chamada["id"]
    f = ferramentas.obter(nome)
    if f is None or not ferramentas.permitida(f, ator):
        return json.dumps({"erro": f"Ferramenta '{nome}' indisponível."}), [evento("ferramenta", id=id_, nome=nome, estado="erro")]
    try:
        args = json.loads(chamada["args"] or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return json.dumps({"erro": "Argumentos inválidos: envie um objeto JSON."}), [
            evento("ferramenta", id=id_, nome=nome, estado="erro")]
    eventos = [evento("ferramenta", id=id_, nome=nome, estado="executando")]
    try:
        if f.mutante:
            chave = (nome, json.dumps(args, sort_keys=True))
            if chave in ja_propostas:
                return json.dumps({"aviso": "Esta proposta já foi registrada nesta conversa."}), eventos
            proposta = await asyncio.to_thread(acoes.propor, ator, ip, f, args)
            ja_propostas.add(chave)
            eventos += [evento("ferramenta", id=id_, nome=nome, estado="ok", resumo="proposta registrada"),
                        evento("proposta", **proposta)]
            return json.dumps({"resultado": "Proposta registrada. Aguarda a confirmação do usuário na tela; "
                                            "NÃO foi executada ainda."}, ensure_ascii=False), eventos
        resultado = await asyncio.to_thread(f.executar, ator, ip, args)
        link = resultado.pop("link", None) if isinstance(resultado, dict) else None
        if link:
            eventos.append(evento("link", **link))
        eventos.append(evento("ferramenta", id=id_, nome=nome, estado="ok"))
        return _cortar(json.dumps(resultado, ensure_ascii=False, default=str)), eventos
    except _ERROS as e:
        eventos.append(evento("ferramenta", id=id_, nome=nome, estado="erro", resumo=str(e)[:200]))
        return json.dumps({"erro": str(e)}, ensure_ascii=False), eventos
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        # Argumentos com tipo errado vindos do modelo (ex.: lista no lugar de texto).
        eventos.append(evento("ferramenta", id=id_, nome=nome, estado="erro"))
        return json.dumps({"erro": f"Argumentos inválidos para {nome} ({e.__class__.__name__})."}), eventos
    except Exception:
        log.exception("Falha inesperada na ferramenta %s", nome)
        eventos.append(evento("ferramenta", id=id_, nome=nome, estado="erro"))
        return json.dumps({"erro": "Falha interna ao executar a ferramenta."}), eventos


async def conversar(ator: dict, ip: str | None, modelo: str, historico: list[dict], frio: bool = False,
                    abrir_stream: Callable | None = None, max_passos: int = MAX_PASSOS) -> AsyncIterator[str]:
    """Gera os eventos SSE de uma rodada de conversa. `historico`: mensagens user/assistant (texto)."""
    abrir_stream = abrir_stream or stream_gateway
    inicio = time.monotonic()
    mensagens = [{"role": "system", "content": prompt.sistema(ator)}, *historico]
    esquemas = ferramentas.esquemas(ator)
    ja_propostas: set = set()
    yield evento("status", fase="carregando" if frio else "aguardando")

    for passo in range(max_passos):
        corpo = {"model": modelo, "messages": mensagens, "tools": esquemas, "stream": True, "temperature": 0.2}
        acumulador = Acumulador()
        falhou = False
        async for tipo, valor in abrir_stream(corpo):
            if tipo == "espera":
                yield evento("aguardando", segundos=valor)
            elif tipo == "http":
                yield evento("erro", mensagem=f"O gateway recusou a requisição ({valor[0]}): {_detalhe_erro(valor[1])}")
                falhou = True
            elif tipo == "falha":
                yield evento("erro", mensagem=valor)
                falhou = True
            elif valor.startswith("data:"):
                payload = valor[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    dados = json.loads(payload)
                except ValueError:
                    continue
                for e in acumulador.chunk(dados):
                    yield e
        if acumulador.erro:
            yield evento("erro", mensagem=acumulador.erro)
            falhou = True
        if falhou:
            break

        chamadas = acumulador.lista()
        if not chamadas:
            if not acumulador.texto.strip():
                yield evento("erro", mensagem="O modelo terminou sem responder. Tente reformular o pedido.")
            yield evento("metricas", total_ms=round((time.monotonic() - inicio) * 1000), passos=passo + 1)
            break

        mensagens.append({"role": "assistant", "content": acumulador.texto or None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["nome"], "arguments": c["args"] or "{}"}}
            for c in chamadas]})
        for c in chamadas:
            texto, eventos = await _usar_ferramenta(ator, ip, c, ja_propostas)
            for e in eventos:
                yield e
            mensagens.append({"role": "tool", "tool_call_id": c["id"], "content": texto})
    else:
        yield evento("erro", mensagem="O concierge precisou de passos demais para este pedido. "
                                      "Divida em pedidos menores.")
    yield evento("fim")
