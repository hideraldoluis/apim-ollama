"""Freio do concierge. Ele chama o gateway com a master key, que ignora os limites de chave; a proteção é daqui.

- no máximo N mensagens por minuto por usuário (configurável);
- uma conversa em andamento por usuário;
- teto global de conversas simultâneas (o Ollama atende poucas requisições em paralelo).

Estado em memória: o painel roda em um único processo.
"""
import time
from collections import defaultdict, deque

MAX_SIMULTANEAS = 2
_JANELA_S = 60.0

_envios: dict[int, deque] = defaultdict(deque)
_em_andamento: set[int] = set()


class LimiteExcedido(Exception):
    pass


def iniciar(usuario_id: int, limite_min: int, agora: float | None = None) -> None:
    """Reserva uma vaga; quem chamou deve chamar liberar() ao terminar (mesmo com erro)."""
    agora = time.monotonic() if agora is None else agora
    if usuario_id in _em_andamento:
        raise LimiteExcedido("Já há uma resposta em andamento. Aguarde ou clique em Parar.")
    fila = _envios[usuario_id]
    while fila and agora - fila[0] > _JANELA_S:
        fila.popleft()
    if len(fila) >= limite_min:
        raise LimiteExcedido(f"Limite de {limite_min} mensagens por minuto atingido. Tente em instantes.")
    if len(_em_andamento) >= MAX_SIMULTANEAS:
        raise LimiteExcedido("O concierge está ocupado com outras conversas. Tente em instantes.")
    fila.append(agora)
    _em_andamento.add(usuario_id)


def liberar(usuario_id: int) -> None:
    _em_andamento.discard(usuario_id)


def reiniciar() -> None:
    """Para testes."""
    _envios.clear()
    _em_andamento.clear()
