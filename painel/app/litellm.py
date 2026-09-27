"""Cliente da API administrativa do LiteLLM. Toda mutação de chave passa por aqui.

As chaves são identificadas pelo hash (coluna token), nunca pelo segredo: o LiteLLM só aplica
o hash quando o valor começa com "sk-", então o hash é aceito como identificador.
"""
import hashlib
from datetime import datetime, timezone

import httpx

from .config import config

_cliente = httpx.Client(
    base_url=config.litellm_url,
    headers={"Authorization": f"Bearer {config.litellm_master_key}"},
    timeout=httpx.Timeout(20.0),
)


class ErroLiteLLM(Exception):
    pass


def _chamar(metodo: str, caminho: str, corpo: dict | None = None) -> dict:
    try:
        r = _cliente.request(metodo, caminho, json=corpo)
    except httpx.HTTPError as e:
        raise ErroLiteLLM(f"Gateway indisponível ({e.__class__.__name__}).") from e
    if r.status_code >= 400:
        try:
            detalhe = r.json()
            detalhe = (detalhe.get("error") or detalhe.get("detail") or detalhe)
            if isinstance(detalhe, dict):
                detalhe = detalhe.get("message") or detalhe.get("error") or str(detalhe)
        except ValueError:
            detalhe = r.text[:300]
        raise ErroLiteLLM(f"O gateway recusou a operação ({r.status_code}): {detalhe}")
    return r.json() if r.content else {}


def hash_da_chave(segredo: str) -> str:
    """Mesmo hash que o LiteLLM grava em LiteLLM_VerificationToken.token."""
    return hashlib.sha256(segredo.encode()).hexdigest()


def modelos_publicados() -> list[str]:
    dados = _chamar("GET", "/v1/models")
    return sorted(m["id"] for m in dados.get("data", []))


_PREFIXOS_OLLAMA = ("ollama_chat/", "ollama/")


def modelos_gateway() -> list[dict]:
    """Modelos publicados no gateway (todos gravados no banco pelo painel)."""
    publicados = []
    for m in _chamar("GET", "/model/info").get("data", []):
        interno = (m.get("litellm_params") or {}).get("model", "")
        info = m.get("model_info") or {}
        prefixo = next((p for p in _PREFIXOS_OLLAMA if interno.startswith(p)), None)
        publicados.append({
            "nome": m.get("model_name"),
            "id": info.get("id"),
            "interno": interno,
            "ollama": interno[len(prefixo):] if prefixo else None,
            "embedding": info.get("mode") == "embedding" or interno.startswith("ollama/"),
        })
    return sorted(publicados, key=lambda m: m["nome"] or "")


def publicar_modelo(nome_publico: str, nome_ollama: str, embedding: bool) -> None:
    """Publica um modelo do Ollama no gateway (exige STORE_MODEL_IN_DB=True no LiteLLM).

    api_base é uma referência a variável de ambiente: o LiteLLM a resolve no próprio ambiente, então
    vale o endereço do Ollama visto pelo gateway (OLLAMA_API_BASE), não pelo painel.
    """
    api_base = "os.environ/OLLAMA_API_BASE"
    corpo = {
        "model_name": nome_publico,
        # ollama_chat/ usa a API de chat do Ollama; embeddings usam ollama/.
        "litellm_params": {"model": f"{'ollama' if embedding else 'ollama_chat'}/{nome_ollama}", "api_base": api_base},
        "model_info": {"mode": "embedding" if embedding else "chat"},
    }
    _chamar("POST", "/model/new", corpo)


def despublicar_modelo(model_id: str) -> None:
    _chamar("POST", "/model/delete", {"id": model_id})


def gerar_chave(alias: str, modelos: list[str], rpm: int, paralelas: int, validade: str,
                criado_para: str, criado_por: str) -> tuple[str, str]:
    """Cria a chave e devolve (segredo, hash). O segredo não é guardado em lugar nenhum."""
    corpo = {
        "key_alias": alias,
        "models": modelos,
        "rpm_limit": rpm,
        "max_parallel_requests": paralelas,
        # Mesmo formato do scripts/create-key.sh; created_by identifica o operador do painel.
        "metadata": {
            "created_for": criado_para,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_by": criado_por,
        },
    }
    if validade != "never":
        corpo["duration"] = validade
    segredo = _chamar("POST", "/key/generate", corpo)["key"]
    return segredo, hash_da_chave(segredo)


def alterar_chave(token: str, *, modelos: list[str] | None = None, rpm: int | None = None,
                  paralelas: int | None = None, validade: str | None = None, alias: str | None = None) -> None:
    corpo: dict = {"key": token}
    if modelos is not None:
        corpo["models"] = modelos
    if rpm is not None:
        corpo["rpm_limit"] = rpm
    if paralelas is not None:
        corpo["max_parallel_requests"] = paralelas
    if alias is not None:
        corpo["key_alias"] = alias
    if validade == "never":
        corpo.update({"duration": None, "expires": None})
    elif validade is not None:
        corpo["duration"] = validade
    _chamar("POST", "/key/update", corpo)


def bloquear(token: str) -> None:
    _chamar("POST", "/key/block", {"key": token})


def desbloquear(token: str) -> None:
    _chamar("POST", "/key/unblock", {"key": token})


def revogar(token: str) -> None:
    _chamar("POST", "/key/delete", {"keys": [token]})
