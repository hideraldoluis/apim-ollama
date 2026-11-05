"""Modelos: download, publicação no gateway, retirada, remoção e carga na memória.

Regras de proteção:
- não se retira do gateway um modelo liberado em alguma chave não expirada (use a retirada em massa, ou
  "retirar de todas e despublicar");
- não se remove do Ollama um modelo publicado no gateway ou com download em andamento.
"""
from .. import auditoria, consultas, downloads, litellm, modelos_chaves, ollama, validacao
from . import NaoEncontrado, com_origem


def publicado(nome: str) -> dict:
    alvo = next((p for p in litellm.modelos_gateway() if p["nome"] == nome), None)
    if alvo is None:
        raise NaoEncontrado(f"{nome} não está publicado no gateway.")
    return alvo


def _publicado_por_id(model_id: str) -> dict:
    alvo = next((p for p in litellm.modelos_gateway() if p["id"] == model_id), None)
    if alvo is None:
        raise validacao.Invalido("Modelo não encontrado no gateway.")
    return alvo


def baixar(ator: dict, ip: str | None, modelo: str) -> str:
    modelo = validacao.modelo_ollama(modelo)
    try:
        downloads.iniciar(modelo, ator["login"], ip)
    except ValueError as e:  # download já em andamento
        raise validacao.Invalido(str(e)) from e
    return modelo


def publicar(ator: dict, ip: str | None, modelo: str, nome_publico: str, origem: str | None = None) -> bool:
    """Publica um modelo instalado. Devolve True se for de embedding."""
    nome_publico = validacao.modelo_publico(nome_publico)
    instalado = next((m for m in ollama.instalados() if m["nome"] == modelo), None)
    if instalado is None:
        raise validacao.Invalido(f"{modelo} não está instalado no Ollama.")
    if any(p["nome"] == nome_publico for p in litellm.modelos_gateway()):
        raise validacao.Invalido(f"Já existe um modelo publicado como {nome_publico}.")
    embedding = ollama.e_embedding(instalado["capacidades"])
    litellm.publicar_modelo(nome_publico, modelo, embedding)
    auditoria.registrar("modelo_publicado", ator["login"], ip, nome_publico,
                        com_origem({"ollama": modelo, "tipo": "embedding" if embedding else "chat"}, origem))
    return embedding


def despublicar(ator: dict, ip: str | None, model_id: str, confirmacao: str, origem: str | None = None) -> dict:
    alvo = _publicado_por_id(model_id)
    em_uso = [k["key_alias"] or k["token"][:12] for k in consultas.chaves()
              if k["status"] != "expirada" and alvo["nome"] in (k["modelos"] or [])]
    if em_uso:
        raise validacao.Invalido(f"{alvo['nome']} está liberado nas chaves: {', '.join(em_uso)}. "
                                 "Retire-o dessas chaves (Chaves do modelo) ou use \"Retirar de todas e despublicar\".")
    if confirmacao.strip() != alvo["nome"]:
        raise validacao.Invalido("Para retirar, digite o nome público do modelo exatamente como aparece.")
    litellm.despublicar_modelo(model_id)
    auditoria.registrar("modelo_despublicado", ator["login"], ip, alvo["nome"],
                        com_origem({"ollama": alvo["ollama"], "id": model_id}, origem))
    return alvo


def despublicar_total(ator: dict, ip: str | None, model_id: str, confirmacao: str,
                      origem: str | None = None) -> tuple[dict, int]:
    """Retira o modelo de todas as chaves e depois do gateway. Chave que ficaria sem modelo passa a SEM_ACESSO."""
    alvo = _publicado_por_id(model_id)
    if confirmacao.strip() != alvo["nome"]:
        raise validacao.Invalido("Para retirar, digite o nome público do modelo exatamente como aparece.")
    plano = modelos_chaves.planejar(alvo["nome"], consultas.chaves(), set(), ultimo_vira_sem_acesso=True)
    res = modelos_chaves.executar(plano, ator["login"], ip, "despublicar-total")
    if res.falhas:
        raise validacao.Invalido("O modelo não foi retirado do gateway porque algumas chaves não foram alteradas. "
                                 + modelos_chaves.resumo(plano, res))
    litellm.despublicar_modelo(model_id)
    auditoria.registrar("modelo_despublicado", ator["login"], ip, alvo["nome"],
                        com_origem({"ollama": alvo["ollama"], "id": model_id, "chaves_alteradas": res.alteradas},
                                   origem))
    return alvo, res.alteradas


def remover(ator: dict, ip: str | None, modelo: str, confirmacao: str, origem: str | None = None) -> None:
    if modelo not in {m["nome"] for m in ollama.instalados()}:
        raise validacao.Invalido(f"{modelo} não está instalado no Ollama.")
    publicados = [p["nome"] for p in litellm.modelos_gateway()
                  if p["ollama"] and ollama.nome_completo(p["ollama"]) == modelo]
    if publicados:
        raise validacao.Invalido(f"{modelo} está publicado no gateway como {', '.join(publicados)}. "
                                 "Retire-o do gateway antes.")
    if downloads.em_andamento(modelo):
        raise validacao.Invalido(f"Há um download de {modelo} em andamento.")
    if confirmacao.strip() != modelo:
        raise validacao.Invalido("Para remover, digite o nome do modelo exatamente como aparece.")
    ollama.remover(modelo)
    auditoria.registrar("modelo_removido", ator["login"], ip, modelo, com_origem({}, origem))


def memoria(ator: dict, ip: str | None, modelo: str, carregar: bool, origem: str | None = None) -> None:
    instalado = next((m for m in ollama.instalados() if m["nome"] == modelo), None)
    if instalado is None:
        raise validacao.Invalido(f"{modelo} não está instalado no Ollama.")
    embedding = ollama.e_embedding(instalado["capacidades"])
    (ollama.carregar if carregar else ollama.descarregar)(modelo, embedding)
    auditoria.registrar("modelo_carregado" if carregar else "modelo_descarregado", ator["login"], ip, modelo,
                        com_origem({}, origem))


def ajustar_chaves(ator: dict, ip: str | None, nome: str, marcadas: set[str], origem: str = "massa"):
    """Libera/retira `nome` em várias chaves (marcadas = tokens que devem ter o modelo). Devolve (plano, resultado)."""
    publicado(nome)
    return modelos_chaves.aplicar(nome, marcadas, ator["login"], ip, origem)
