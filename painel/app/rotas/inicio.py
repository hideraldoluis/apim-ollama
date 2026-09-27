"""Visão geral: uso dos últimos dias, chaves que pedem atenção e desempenho por modelo."""
from fastapi import APIRouter, Depends, Request

from .. import autenticacao as auth, consultas, db
from ..web import render

router = APIRouter()


@router.get("/")
def visao_geral(request: Request, u: dict = Depends(auth.usuario_logado)):
    inicio, fim = consultas.periodo(None, None, 7)
    por_dia = consultas.consumo(inicio, fim, "dia")
    por_chave = consultas.consumo(inicio, fim, "chave")
    de, ate = consultas.limites(inicio, fim)
    modelos = db.consultar("select * from painel.resumo_modelos(%s, %s)", (de, ate))
    chaves = consultas.chaves()
    atencao = [k for k in chaves if k["status"] in ("expirando", "expirada", "bloqueada")]
    total = {
        "requisicoes": sum(r["requisicoes"] for r in por_dia),
        "erros": sum(r["erros"] for r in por_dia),
        "tok_total": sum(r["tok_total"] for r in por_dia),
        "chaves_ativas": sum(1 for k in chaves if k["status"] in ("ativa", "expirando")),
    }
    return render(request, "inicio.html", {
        "inicio": inicio, "fim": fim, "por_dia": por_dia, "top": por_chave[:10], "modelos": modelos,
        "atencao": atencao, "total": total,
        "max_dia": max((r["tok_total"] for r in por_dia), default=0) or 1,
    })
