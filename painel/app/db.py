"""Conexões com o Postgres.

Dois pools com roles distintos:
  app       metadados, usuários, sessões e auditoria. Não tem permissão para ler prompt/resposta.
  conteudo  só executa painel.log_conteudo/logs_conteudo. Usado exclusivamente pelas rotas de admin.
"""
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import config

_app = ConnectionPool(config.dsn_app, min_size=1, max_size=10, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": False})
_conteudo = ConnectionPool(config.dsn_conteudo, min_size=0, max_size=2, open=False,
                           kwargs={"row_factory": dict_row, "autocommit": True})


def abrir() -> None:
    _app.open(wait=True)
    _conteudo.open()


def fechar() -> None:
    _app.close()
    _conteudo.close()


@contextmanager
def transacao():
    """Conexão do painel_app em transação (commit ao sair sem erro)."""
    with _app.connection() as conn:
        yield conn


def consultar(sql: str, params=None) -> list[dict]:
    with _app.connection() as conn:
        return conn.execute(sql, params).fetchall()


def consultar_um(sql: str, params=None) -> dict | None:
    with _app.connection() as conn:
        return conn.execute(sql, params).fetchone()


def executar(sql: str, params=None) -> None:
    with _app.connection() as conn:
        conn.execute(sql, params)


def consultar_conteudo(sql: str, params=None) -> list[dict]:
    with _conteudo.connection() as conn:
        return conn.execute(sql, params).fetchall()
