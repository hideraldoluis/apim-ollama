"""Telas exclusivas de admin: trilha de auditoria e retenção."""
from fastapi import APIRouter, Depends, Request

from .. import autenticacao as auth, consultas, db
from ..auditoria import ACOES
from ..web import render

router = APIRouter()
_POR_PAGINA = 100


@router.get("/auditoria")
def auditoria(request: Request, desde: str | None = None, ate: str | None = None, acao: str | None = None,
              usuario: str | None = None, antes_id: int | None = None, u: dict = Depends(auth.exigir_admin)):
    inicio, fim = consultas.periodo(desde, ate, 30)
    de, ate_ = consultas.limites(inicio, fim)
    acao = acao if acao in ACOES else None
    usuario = (usuario or "").strip() or None
    linhas = db.consultar(
        """
        select id, quando, usuario, ip, acao, alvo, detalhes from painel.auditoria
         where quando >= %(de)s and quando < %(ate)s
           and (%(acao)s::text is null or acao = %(acao)s)
           and (%(usuario)s::text is null or usuario = %(usuario)s)
           and (%(antes)s::bigint is null or id < %(antes)s)
         order by id desc limit %(lim)s
        """,
        {"de": de, "ate": ate_, "acao": acao, "usuario": usuario, "antes": antes_id, "lim": _POR_PAGINA + 1})
    proxima = linhas[_POR_PAGINA - 1]["id"] if len(linhas) > _POR_PAGINA else None
    return render(request, "auditoria.html", {
        "linhas": linhas[:_POR_PAGINA], "proxima": proxima, "inicio": inicio, "fim": fim,
        "acao": acao, "filtro_usuario": usuario,
    })


@router.get("/retencao")
def retencao(request: Request, u: dict = Depends(auth.exigir_admin)):
    r = db.consultar_um("select * from painel.retencao()")
    consolidacao = db.consultar_um(
        "select (select ate from painel.consolidacao) as ate, min(dia) as desde, count(*) as linhas "
        "from painel.consumo_diario")
    return render(request, "retencao.html", {"r": r, "consolidacao": consolidacao, "periodo_dias": 180})
