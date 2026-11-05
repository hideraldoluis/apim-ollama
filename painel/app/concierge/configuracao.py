"""Configuração do concierge, guardada em painel.configuracoes (editada por admin em /concierge/configuracao)."""
from psycopg.types.json import Jsonb

from .. import auditoria, db, litellm, ollama, validacao

CHAVE = "concierge"
PADRAO = {"modelo": None, "ativo": True, "limite_msg_min": 20}
LIMITE_MIN, LIMITE_MAX = 1, 120


def obter() -> dict:
    linha = db.consultar_um("select valor from painel.configuracoes where chave = %s", (CHAVE,))
    valor = linha["valor"] if linha else {}
    return {**PADRAO, **{k: v for k, v in valor.items() if k in PADRAO}}


def modelos_elegiveis() -> list[dict]:
    """Modelos de chat publicados no gateway cujo modelo no Ollama declara suporte a ferramentas (tools).

    Levanta ErroLiteLLM/ErroOllama se algum serviço estiver fora do ar."""
    instalados = {m["nome"]: m for m in ollama.instalados()}
    elegiveis = []
    for p in litellm.modelos_gateway():
        if p["embedding"] or not p["ollama"]:
            continue
        info = instalados.get(ollama.nome_completo(p["ollama"]))
        if info and "tools" in info["capacidades"]:
            elegiveis.append({"nome": p["nome"], "ollama": info["nome"], "parametros": info["parametros"],
                              "contexto": info["contexto"]})
    return sorted(elegiveis, key=lambda m: m["nome"])


def salvar(ator: dict, ip: str | None, modelo: str, ativo: bool, limite) -> dict:
    limite = validacao.inteiro_positivo("limite de mensagens por minuto", limite)
    if not LIMITE_MIN <= limite <= LIMITE_MAX:
        raise validacao.Invalido(f"Limite inválido: use de {LIMITE_MIN} a {LIMITE_MAX} mensagens por minuto.")
    modelo = (modelo or "").strip()
    if modelo and modelo not in {m["nome"] for m in modelos_elegiveis()}:
        raise validacao.Invalido(f"{modelo} não é um modelo de chat publicado com suporte a ferramentas (tools).")
    if ativo and not modelo:
        raise validacao.Invalido("Escolha um modelo para ativar o concierge.")
    antes = obter()
    novo = {"modelo": modelo or None, "ativo": bool(ativo), "limite_msg_min": limite}
    db.executar(
        "insert into painel.configuracoes (chave, valor, atualizado_por) values (%s, %s, %s) "
        "on conflict (chave) do update set valor = excluded.valor, atualizado_em = now(), "
        "atualizado_por = excluded.atualizado_por", (CHAVE, Jsonb(novo), ator["login"]))
    auditoria.registrar("concierge_configurado", ator["login"], ip, CHAVE, {"antes": antes, "depois": novo})
    return novo
