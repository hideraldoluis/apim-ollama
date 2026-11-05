"""Serviços: as operações do painel, independentes de HTTP.

Usados pelas rotas HTML e pelo concierge, para que as regras (validações, confirmações, auditoria)
existam num só lugar. Cada função recebe o `ator` (dicionário do usuário logado, com ao menos "login") e o
`ip`, valida, executa e audita. Erros de regra viram exceções:
  validacao.Invalido, RegraViolada, NaoEncontrado, litellm.ErroLiteLLM, ollama.ErroOllama.
"""


class RegraViolada(Exception):
    pass


class NaoEncontrado(Exception):
    pass


def com_origem(detalhes: dict, origem: str | None) -> dict:
    """Acrescenta a origem (ex.: "concierge") aos detalhes de auditoria, quando houver."""
    return {**detalhes, "origem": origem} if origem else detalhes
