"""Consumo de tokens por chave, por modelo e por chave × modelo (leitor e admin)."""
from fastapi import APIRouter, Depends, Request

from .. import auditoria, autenticacao as auth, consultas
from ..csv_util import resposta_csv
from ..web import render

router = APIRouter()

_VISOES = ("chave", "modelo", "chave_modelo")
_METRICAS = ("requisicoes", "erros", "tok_prompt", "tok_resposta", "tok_total")


def _somar(linhas: list[dict]) -> dict:
    return {c: sum(r[c] for r in linhas) for c in _METRICAS}


def _montar(inicio, fim, visao: str) -> dict:
    """Uma consulta no nível chave × modelo; as demais visões são agregadas a partir dela."""
    detalhe = consultas.consumo(inicio, fim, "chave_modelo")
    # Dados atuais da chave (o alias do log é o vigente na época da requisição).
    atuais = {k["token"]: k for k in consultas.chaves()}

    chaves: dict[tuple, dict] = {}
    modelos: dict[str, dict] = {}
    for r in detalhe:
        k = atuais.get(r["chave"])
        c = chaves.setdefault((r["chave"], r["alias"]), {
            "chave": r["chave"], "alias": r["alias"], "existe": k is not None,
            "criado_para": k["criado_para"] if k else None, "modelos": [], **dict.fromkeys(_METRICAS, 0)})
        mo = modelos.setdefault(r["modelo"], {"modelo": r["modelo"], "chaves": set(), **dict.fromkeys(_METRICAS, 0)})
        for m in _METRICAS:
            c[m] += r[m]
            mo[m] += r[m]
        c["modelos"].append(r)
        if r["chave"]:
            mo["chaves"].add(r["chave"])

    por_chave = sorted(chaves.values(), key=lambda c: c["tok_total"], reverse=True)
    for c in por_chave:
        c["modelos"].sort(key=lambda r: r["tok_total"], reverse=True)
    por_modelo = sorted(modelos.values(), key=lambda m: m["tok_total"], reverse=True)
    for m in por_modelo:
        m["qtd_chaves"] = len(m.pop("chaves"))

    linhas = {"chave": por_chave, "modelo": por_modelo, "chave_modelo": por_chave}[visao]
    return {
        "linhas": linhas, "total": _somar(detalhe),
        "qtd_chaves": sum(1 for c in por_chave if c["chave"]), "qtd_modelos": sum(1 for m in por_modelo if m["modelo"]),
        "max_tok": max((r["tok_total"] for r in linhas), default=0) or 1,
    }


@router.get("/consumo")
def consumo(request: Request, desde: str | None = None, ate: str | None = None, visao: str = "chave",
            u: dict = Depends(auth.usuario_logado)):
    visao = visao if visao in _VISOES else "chave"
    inicio, fim = consultas.periodo(desde, ate, 30)
    return render(request, "consumo.html", {"inicio": inicio, "fim": fim, "visao": visao,
                                            **_montar(inicio, fim, visao)})


@router.get("/consumo.csv")
def consumo_csv(request: Request, desde: str | None = None, ate: str | None = None, visao: str = "chave",
                u: dict = Depends(auth.usuario_logado)):
    visao = visao if visao in _VISOES else "chave"
    inicio, fim = consultas.periodo(desde, ate, 30)
    dados = _montar(inicio, fim, visao)
    if visao == "modelo":
        colunas, linhas = ["modelo", "qtd_chaves", *_METRICAS], dados["linhas"]
    elif visao == "chave_modelo":
        colunas = ["alias", "chave", "modelo", *_METRICAS]
        linhas = [r for c in dados["linhas"] for r in c["modelos"]]
    else:
        colunas, linhas = ["alias", "criado_para", "chave", *_METRICAS], dados["linhas"]
    auditoria.registrar("exportacao", u["login"], auth.ip_cliente(request), "consumo",
                        {"desde": str(inicio), "ate": str(fim), "visao": visao, "linhas": len(linhas)})
    return resposta_csv(f"consumo_{visao}_{inicio}_{fim}.csv", colunas, linhas)
