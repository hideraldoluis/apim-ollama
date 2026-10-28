"""Validação de entradas (mesmas regras dos scripts/*.sh)."""
import re

_DURACAO = re.compile(r"^([0-9]+(s|m|h|d|w|mo)|never)$")
_LOGIN = re.compile(r"^[a-z0-9._-]{3,64}$")


class Invalido(ValueError):
    pass


# No LiteLLM, models=[] significa "todos os modelos". Uma chave que perde o último modelo recebe este
# nome, que nunca é publicado no gateway, e passa a ser recusada em tudo (em vez de liberar tudo).
SEM_ACESSO = "__sem-acesso__"


def alias(valor: str) -> str:
    valor = valor.strip()
    if not valor or len(valor) > 100:
        raise Invalido("Informe um nome de consumidor com até 100 caracteres.")
    if any(c in valor for c in '"\\') or any(ord(c) < 32 or ord(c) == 127 for c in valor):
        raise Invalido("Nome inválido: não use aspas duplas, barra invertida nem caracteres de controle.")
    return valor


def duracao(valor: str) -> str:
    valor = valor.strip()
    if not _DURACAO.match(valor):
        raise Invalido(f"Validade inválida: '{valor}'. Use, por exemplo, 30d, 12h, 6mo ou never.")
    return valor


def inteiro_positivo(nome: str, valor: str | int) -> int:
    texto = str(valor).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,6}", texto):
        raise Invalido(f"Valor inválido para {nome}: use um inteiro positivo.")
    return int(texto)


def modelos(selecionados: list[str], disponiveis: list[str]) -> list[str]:
    escolhidos = [m for m in dict.fromkeys(s.strip() for s in selecionados) if m]
    if not escolhidos:
        raise Invalido("Selecione ao menos um modelo.")
    fora = [m for m in escolhidos if m not in disponiveis]
    if fora:
        raise Invalido(f"Modelos não publicados no gateway: {', '.join(fora)}.")
    return escolhidos


_MODELO_OLLAMA = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:-]{0,199}$")
_MODELO_PUBLICO = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,99}$")


def modelo_ollama(valor: str) -> str:
    """Nome no catálogo do Ollama: llama3.2, qwen2.5:7b, hf.co/usuario/repo:Q4_K_M..."""
    valor = valor.strip()
    if not _MODELO_OLLAMA.match(valor) or ".." in valor or "//" in valor:
        raise Invalido("Nome de modelo inválido. Exemplos: llama3.2, qwen2.5:7b, nomic-embed-text.")
    return valor


def modelo_publico(valor: str) -> str:
    """Nome que os consumidores usam no campo "model" da API."""
    valor = valor.strip()
    if not _MODELO_PUBLICO.match(valor):
        raise Invalido("Nome público inválido: use até 100 caracteres entre letras, números, ponto, hífen, "
                       "sublinhado e dois-pontos.")
    return valor


def login(valor: str) -> str:
    valor = valor.strip().lower()
    if not _LOGIN.match(valor):
        raise Invalido("Login inválido: use de 3 a 64 caracteres entre a-z, 0-9, ponto, hífen e sublinhado.")
    return valor


def nome(valor: str) -> str:
    valor = " ".join(valor.split())
    if not valor or len(valor) > 120:
        raise Invalido("Informe o nome com até 120 caracteres.")
    return valor


def motivo(valor: str) -> str:
    valor = valor.strip()
    if len(valor) < 5 or len(valor) > 500:
        raise Invalido("Informe o motivo (de 5 a 500 caracteres).")
    return valor


def papel(valor: str) -> str:
    if valor not in ("admin", "leitor"):
        raise Invalido("Papel inválido.")
    return valor
