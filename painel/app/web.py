"""Renderização de templates, mensagens de retorno (flash) e formatação."""
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .auditoria import ACOES
from .config import config

_FLASH = "painel-msg"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _datahora(valor: datetime | None, formato: str = "%d/%m/%Y %H:%M") -> str:
    if valor is None:
        return "—"
    if valor.tzinfo is None:
        return valor.strftime(formato)
    return valor.astimezone(config.tz).strftime(formato)


def _numero(valor) -> str:
    if valor is None:
        return "—"
    return f"{int(valor):,}".replace(",", ".")


def _bytes(valor) -> str:
    if not valor:
        return "—"
    for unidade in ("B", "KB", "MB", "GB"):
        if valor < 1024 or unidade == "GB":
            return f"{valor:.1f} {unidade}".replace(".", ",") if unidade != "B" else f"{valor} B"
        valor /= 1024


def _data(valor) -> str:
    return valor.strftime("%d/%m/%Y") if valor else "—"


templates.env.filters["datahora"] = _datahora
templates.env.filters["numero"] = _numero
templates.env.filters["data"] = _data
templates.env.filters["bytes"] = _bytes
templates.env.globals["ACOES"] = ACOES


def render(request: Request, nome: str, contexto: dict | None = None, status: int = 200):
    ctx = {"usuario": getattr(request.state, "usuario", None), "flash": None, **(contexto or {})}
    bruto = request.cookies.get(_FLASH)
    tipo, sep, texto = unquote(bruto or "").partition("|")
    if sep and tipo in ("ok", "aviso", "erro"):
        ctx["flash"] = {"tipo": tipo, "texto": texto}
    resposta = templates.TemplateResponse(request, nome, ctx, status_code=status)
    if bruto:
        resposta.delete_cookie(_FLASH, path="/")
    return resposta


def redirecionar(url: str, mensagem: str | None = None, tipo: str = "ok") -> RedirectResponse:
    """Redireciona (303) com mensagem opcional. Nunca use para segredos."""
    resposta = RedirectResponse(url, status_code=303)
    if mensagem:
        resposta.set_cookie(_FLASH, quote(f"{tipo}|{mensagem}"), max_age=60, path="/",
                            httponly=True, secure=config.cookie_seguro, samesite="strict")
    return resposta
