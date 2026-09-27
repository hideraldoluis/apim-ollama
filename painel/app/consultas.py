"""Consultas de leitura compartilhadas pelas telas (sem texto de prompt/resposta)."""
from datetime import date, datetime, time, timedelta

from . import db
from .config import config

# Agrupamentos permitidos no consumo -> colunas SQL (lista fechada: nada vem do usuário).
_AGRUPAR = {
    "chave": "chave, alias",
    "dia": "dia",
    "modelo": "modelo",
    "chave_modelo": "chave, alias, modelo",
}


def hoje() -> date:
    return datetime.now(config.tz).date()


def periodo(desde: str | None, ate: str | None, padrao_dias: int) -> tuple[date, date]:
    """Converte filtros AAAA-MM-DD (fuso do painel) em datas, com padrão e limites sensatos."""
    fim = _data(ate) or hoje()
    inicio = _data(desde) or (fim - timedelta(days=padrao_dias - 1))
    if inicio > fim:
        inicio, fim = fim, inicio
    return inicio, fim


def limites(inicio: date, fim: date) -> tuple[datetime, datetime]:
    """Intervalo [inicio 00:00, fim+1 00:00) no fuso do painel."""
    return (datetime.combine(inicio, time.min, config.tz),
            datetime.combine(fim + timedelta(days=1), time.min, config.tz))


def _data(texto: str | None) -> date | None:
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def consumo(inicio: date, fim: date, agrupar: str = "chave", chave: str | None = None) -> list[dict]:
    """Requisições e tokens no período. Dias já consolidados vêm de painel.consumo_diario;
    os demais (tipicamente hoje) são agregados na hora a partir dos logs."""
    colunas = _AGRUPAR[agrupar]
    sql = f"""
        with corte as (
            select coalesce((select ate from painel.consolidacao), date '1900-01-01') as c
        ), base as (
            select d.dia, d.chave, d.alias, d.modelo, d.requisicoes, d.erros,
                   d.tok_prompt, d.tok_resposta, d.tok_total
              from painel.consumo_diario d, corte
             where d.dia between %(de)s and least(%(ate)s, corte.c)
            union all
            select a.dia, a.chave, a.alias, a.modelo, a.requisicoes, a.erros,
                   a.tok_prompt, a.tok_resposta, a.tok_total
              from corte, painel.agregado_diario(greatest(%(de)s, corte.c + 1), %(ate)s, %(fuso)s) a
        )
        select {colunas},
               sum(requisicoes)::bigint  as requisicoes,
               sum(erros)::bigint        as erros,
               sum(tok_prompt)::bigint   as tok_prompt,
               sum(tok_resposta)::bigint as tok_resposta,
               sum(tok_total)::bigint    as tok_total
          from base
         where (%(chave)s::text is null or chave = %(chave)s)
         group by {colunas}
         order by {"dia" if agrupar == "dia" else "tok_total desc"}
    """
    return db.consultar(sql, {"de": inicio, "ate": fim, "fuso": config.fuso, "chave": chave})


def chaves() -> list[dict]:
    """Chaves existentes com status calculado."""
    agora = datetime.now(config.tz)
    linhas = db.consultar("select * from painel.chaves()")
    for k in linhas:
        k["status"] = status_chave(k, agora)
    return linhas


def chave(token: str) -> dict | None:
    for k in chaves():
        if k["token"] == token:
            return k
    return None


def status_chave(k: dict, agora: datetime) -> str:
    if k["bloqueada"]:
        return "bloqueada"
    if k["expira_em"] is not None:
        if k["expira_em"] <= agora:
            return "expirada"
        if k["expira_em"] - agora <= timedelta(days=7):
            return "expirando"
    return "ativa"


def modelos_nos_logs() -> list[str]:
    linhas = db.consultar("select distinct modelo from painel.consumo_diario where modelo <> '' order by 1")
    return [r["modelo"] for r in linhas]
