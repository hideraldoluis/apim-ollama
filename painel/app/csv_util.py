"""Geração de CSV para exportações."""
import csv
import io
import json

from fastapi.responses import Response


def _celula(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, (dict, list)):
        valor = json.dumps(valor, ensure_ascii=False)
    texto = str(valor)
    # Evita injeção de fórmula ao abrir no Excel/LibreOffice.
    if texto[:1] in ("=", "+", "-", "@", "\t", "\r"):
        texto = "'" + texto
    return texto


def resposta_csv(nome_arquivo: str, colunas: list[str], linhas: list[dict]) -> Response:
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow(colunas)
    for linha in linhas:
        escritor.writerow([_celula(linha.get(c)) for c in colunas])
    # BOM para o Excel reconhecer UTF-8.
    return Response("﻿" + saida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'})
