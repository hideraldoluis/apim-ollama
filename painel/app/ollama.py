"""Cliente da API do Ollama (rede interna do compose; o Ollama não tem autenticação)."""
import json
from collections.abc import Iterator

import httpx

from .config import config

_cliente = httpx.Client(base_url=config.ollama_url, timeout=httpx.Timeout(30.0))


class ErroOllama(Exception):
    pass


def _chamar(metodo: str, caminho: str, corpo: dict | None = None, timeout: float | None = None) -> dict:
    try:
        r = _cliente.request(metodo, caminho, json=corpo, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
    except httpx.HTTPError as e:
        raise ErroOllama(f"Ollama indisponível ({e.__class__.__name__}).") from e
    if r.status_code >= 400:
        try:
            detalhe = r.json().get("error") or r.text
        except ValueError:
            detalhe = r.text
        raise ErroOllama(f"O Ollama recusou a operação ({r.status_code}): {str(detalhe)[:300]}")
    return r.json() if r.content else {}


def nome_completo(nome: str) -> str:
    """O Ollama trata "modelo" e "modelo:latest" como o mesmo modelo."""
    return nome if ":" in nome.rsplit("/", 1)[-1] else f"{nome}:latest"


def versao() -> str:
    return _chamar("GET", "/api/version").get("version", "?")


def instalados() -> list[dict]:
    modelos = []
    for m in _chamar("GET", "/api/tags").get("models", []):
        d = m.get("details") or {}
        modelos.append({
            "nome": m["name"],
            "tamanho": m.get("size"),
            "modificado_em": m.get("modified_at"),
            "familia": d.get("family"),
            "parametros": d.get("parameter_size"),
            "quantizacao": d.get("quantization_level"),
            "contexto": d.get("context_length"),
            "capacidades": m.get("capabilities") or [],
        })
    return sorted(modelos, key=lambda m: m["nome"])


def carregados() -> dict[str, dict]:
    """Modelos na memória agora, por nome: tamanho total, parte na GPU e quando expiram."""
    return {m["name"]: {"tamanho": m.get("size"), "vram": m.get("size_vram"), "expira_em": m.get("expires_at")}
            for m in _chamar("GET", "/api/ps").get("models", [])}


def e_embedding(capacidades: list[str]) -> bool:
    return "embedding" in capacidades and "completion" not in capacidades


def baixar(nome: str) -> Iterator[dict]:
    """Baixa (ou atualiza) um modelo. Produz os eventos de progresso do Ollama."""
    try:
        with _cliente.stream("POST", "/api/pull", json={"model": nome, "stream": True},
                             timeout=httpx.Timeout(60.0, read=600.0)) as r:
            if r.status_code >= 400:
                r.read()
                raise ErroOllama(f"O Ollama recusou o download ({r.status_code}): {r.text[:300]}")
            for linha in r.iter_lines():
                if not linha.strip():
                    continue
                evento = json.loads(linha)
                if "error" in evento:
                    raise ErroOllama(str(evento["error"])[:300])
                yield evento
    except httpx.HTTPError as e:
        raise ErroOllama(f"Conexão com o Ollama interrompida ({e.__class__.__name__}).") from e


def remover(nome: str) -> None:
    _chamar("DELETE", "/api/delete", {"model": nome})


def carregar(nome: str, embedding: bool) -> None:
    """Carrega na memória (usa o OLLAMA_KEEP_ALIVE do servidor). Pode levar minutos em modelos grandes."""
    if embedding:
        _chamar("POST", "/api/embed", {"model": nome, "input": "aquecer"}, timeout=600.0)
    else:
        _chamar("POST", "/api/generate", {"model": nome, "prompt": "", "stream": False}, timeout=600.0)


def descarregar(nome: str, embedding: bool) -> None:
    if embedding:
        _chamar("POST", "/api/embed", {"model": nome, "input": "x", "keep_alive": 0}, timeout=120.0)
    else:
        _chamar("POST", "/api/generate", {"model": nome, "prompt": "", "keep_alive": 0, "stream": False}, timeout=120.0)
