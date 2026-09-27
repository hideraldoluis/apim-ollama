"""Logs de requisição. Lista e detalhe NUNCA trazem texto; o conteúdo é só para admin, auditado."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import auditoria, autenticacao as auth, consultas, db
from ..csv_util import resposta_csv
from ..web import redirecionar, render

router = APIRouter()
_POR_PAGINA = 50
_MAX_EXPORTACAO = 50000


def _filtros(desde, ate, alias, chave, modelo, status) -> dict:
    inicio, fim = consultas.periodo(desde, ate, 1)
    de, ate_ = consultas.limites(inicio, fim)
    return {
        "inicio": inicio, "fim": fim, "de": de, "ate": ate_,
        "alias": (alias or "").strip() or None,
        "chave": (chave or "").strip() or None,
        "modelo": (modelo or "").strip() or None,
        "status": status if status in ("success", "failure") else None,
    }


def _consultar(f: dict, cursor_quando=None, cursor_id=None, limite=_POR_PAGINA) -> list[dict]:
    return db.consultar(
        "select * from painel.logs(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (f["de"], f["ate"], f["alias"], f["chave"], f["modelo"], f["status"], cursor_quando, cursor_id, limite),
    )


@router.get("/logs")
def lista(request: Request, desde: str | None = None, ate: str | None = None, alias: str | None = None,
          chave: str | None = None, modelo: str | None = None, status: str | None = None,
          apos: str | None = None, apos_id: str | None = None, u: dict = Depends(auth.usuario_logado)):
    f = _filtros(desde, ate, alias, chave, modelo, status)
    cursor = None
    if apos and apos_id:
        try:
            cursor = datetime.fromisoformat(apos)
        except ValueError:
            cursor = None
    linhas = _consultar(f, cursor, apos_id if cursor else None, _POR_PAGINA + 1)
    proxima = None
    if len(linhas) > _POR_PAGINA:
        linhas = linhas[:_POR_PAGINA]
        proxima = {"apos": linhas[-1]["quando"].isoformat(), "apos_id": linhas[-1]["id"]}
    aliases = sorted({k["key_alias"] for k in consultas.chaves() if k["key_alias"]})
    return render(request, "logs.html", {
        "f": f, "linhas": linhas, "proxima": proxima, "aliases": aliases,
        "modelos": consultas.modelos_nos_logs(), "paginado": cursor is not None,
    })


@router.get("/logs.csv")
def exportar(request: Request, desde: str | None = None, ate: str | None = None, alias: str | None = None,
             chave: str | None = None, modelo: str | None = None, status: str | None = None,
             u: dict = Depends(auth.usuario_logado)):
    """Exportação de metadados (sem texto), para leitor e admin."""
    f = _filtros(desde, ate, alias, chave, modelo, status)
    linhas = _consultar(f, limite=_MAX_EXPORTACAO)
    auditoria.registrar("exportacao", u["login"], auth.ip_cliente(request), "logs",
                        {"filtros": _filtros_auditoria(f), "linhas": len(linhas), "conteudo": False})
    colunas = ["id", "quando", "alias", "chave", "modelo", "status", "codigo_erro",
               "tok_prompt", "tok_resposta", "tok_total", "duracao_ms", "ip"]
    return resposta_csv(f"logs_{f['inicio']}_{f['fim']}.csv", colunas, linhas)


@router.post("/logs/exportar-conteudo")
def exportar_conteudo(request: Request, u: dict = Depends(auth.exigir_admin),
                      desde: str = Form(""), ate: str = Form(""), alias: str = Form(""), chave: str = Form(""),
                      modelo: str = Form(""), status: str = Form(""), justificativa: str = Form("")):
    """Exportação COM texto: só admin, com justificativa registrada."""
    f = _filtros(desde, ate, alias, chave, modelo, status)
    if len(justificativa.strip()) < 5:
        return redirecionar("/logs", "Informe a justificativa para exportar o conteúdo.", "erro")
    metadados = {r["id"]: r for r in _consultar(f, limite=_MAX_EXPORTACAO)}
    auditoria.registrar("exportacao", u["login"], auth.ip_cliente(request), "logs",
                        {"filtros": _filtros_auditoria(f), "linhas": len(metadados), "conteudo": True,
                         "justificativa": justificativa.strip()})
    conteudo = db.consultar_conteudo(
        "select * from painel.logs_conteudo(%s, %s, %s, %s, %s, %s, %s)",
        (f["de"], f["ate"], f["alias"], f["chave"], f["modelo"], f["status"], _MAX_EXPORTACAO))
    linhas = [{**metadados.get(c["id"], {}), "prompt": c["prompt"], "resposta": _texto_resposta(c["resposta"])}
              for c in conteudo]
    colunas = ["id", "quando", "alias", "modelo", "status", "tok_total", "prompt", "resposta"]
    return resposta_csv(f"logs_conteudo_{f['inicio']}_{f['fim']}.csv", colunas, linhas)


@router.get("/logs/{id_}")
def detalhe(request: Request, id_: str, u: dict = Depends(auth.usuario_logado)):
    r = db.consultar_um("select * from painel.log(%s)", (id_[:200],))
    if r is None:
        raise HTTPException(404)
    return render(request, "log.html", {"r": r, "conteudo": None})


@router.post("/logs/{id_}/conteudo")
def conteudo(request: Request, id_: str, u: dict = Depends(auth.exigir_admin)):
    r = db.consultar_um("select * from painel.log(%s)", (id_[:200],))
    if r is None:
        raise HTTPException(404)
    # Auditoria antes da leitura: se o registro falhar, o conteúdo não é exibido.
    auditoria.registrar("log_conteudo_visto", u["login"], auth.ip_cliente(request), r["alias"],
                        {"request_id": r["id"], "token": r["chave"]})
    c = db.consultar_conteudo("select * from painel.log_conteudo(%s)", (r["id"],))
    conteudo_ = c[0] if c else {"prompt": None, "resposta": None}
    return render(request, "log.html", {"r": r, "conteudo": {
        "mensagens": _mensagens(conteudo_["prompt"]),
        "resposta": _texto_resposta(conteudo_["resposta"]),
        "bruto": json.dumps(conteudo_, ensure_ascii=False, indent=2, default=str),
    }})


def _filtros_auditoria(f: dict) -> dict:
    return {k: (str(v) if v is not None else None) for k, v in f.items() if k not in ("de", "ate")}


def _mensagens(prompt) -> list[dict]:
    if not isinstance(prompt, list):
        return []
    saida = []
    for m in prompt:
        if not isinstance(m, dict):
            continue
        conteudo_ = m.get("content")
        if isinstance(conteudo_, list):  # formato multimodal: [{"type": "text", "text": ...}, ...]
            conteudo_ = "\n".join(p.get("text", "") for p in conteudo_ if isinstance(p, dict))
        saida.append({"papel": m.get("role", "?"), "texto": conteudo_ if isinstance(conteudo_, str) else json.dumps(conteudo_, ensure_ascii=False)})
    return saida


def _texto_resposta(resposta) -> str | None:
    if not isinstance(resposta, dict):
        return None
    try:
        escolha = resposta["choices"][0]
        return (escolha.get("message") or {}).get("content") or escolha.get("text")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
