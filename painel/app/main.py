"""Painel de governança de chaves e logs do apim-ollama."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, downloads, rotinas
from .autenticacao import Proibido, Redirecionar
from .rotas import acesso, admin, chat, chaves, consumo, inicio, logs, modelos, usuarios
from .web import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    db.abrir()
    downloads.marcar_interrompidos()
    rotinas.iniciar()
    yield
    rotinas.parar()
    db.fechar()


app = FastAPI(lifespan=ciclo_de_vida, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

for modulo in (acesso, inicio, chat, chaves, consumo, logs, modelos, usuarios, admin):
    app.include_router(modulo.router)

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


@app.middleware("http")
async def seguranca(request: Request, chamar):
    # Defesa extra contra CSRF: POST de outra origem é recusado antes de qualquer processamento.
    if request.method == "POST":
        origem = request.headers.get("origin")
        if origem and urlsplit(origem).netloc != request.headers.get("host"):
            return PlainTextResponse("Origem não permitida.", status_code=403)
    resposta = await chamar(request)
    resposta.headers.setdefault("Content-Security-Policy", _CSP)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["X-Frame-Options"] = "DENY"
    # "same-origin", e não "no-referrer": com no-referrer o navegador envia "Origin: null" nos POSTs
    # e a checagem de origem acima recusaria o próprio painel. Para outros sites nada é enviado.
    resposta.headers["Referrer-Policy"] = "same-origin"
    if not request.url.path.startswith("/static/"):
        # Páginas contêm dados sensíveis: nada de cache no navegador ou em proxies.
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.exception_handler(Redirecionar)
async def _redirecionar(request: Request, exc: Redirecionar):
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(Proibido)
async def _proibido(request: Request, exc: Proibido):
    return render(request, "erro.html", {"titulo": "Acesso negado", "mensagem": exc.motivo}, status=403)


@app.exception_handler(404)
async def _nao_encontrado(request: Request, exc):
    return render(request, "erro.html", {"titulo": "Não encontrado",
                                         "mensagem": "A página ou o registro não existe."}, status=404)


@app.get("/saude", include_in_schema=False)
def saude():
    db.consultar_um("select 1")
    return PlainTextResponse("ok")
