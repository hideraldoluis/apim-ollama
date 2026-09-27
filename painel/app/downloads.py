"""Downloads de modelos no Ollama, em segundo plano (uma thread por download).

O progresso fica em memória e é gravado em painel.downloads a cada poucos segundos. Se o painel
reiniciar no meio, o registro vira "interrompido"; o Ollama retoma de onde parou num novo download.
"""
import logging
import threading
import time

from . import auditoria, db, ollama

log = logging.getLogger("painel.downloads")
_trava = threading.Lock()
_ativos: dict[int, dict] = {}  # id -> {"modelo", "total", "baixado", "mensagem"}
_GRAVAR_A_CADA_S = 5


def marcar_interrompidos() -> None:
    """Na subida: downloads que estavam em andamento no processo anterior não continuam."""
    db.executar("update painel.downloads set status = 'interrompido', finalizado_em = now(), "
                "mensagem = 'O painel reiniciou durante o download' where status = 'baixando'")


def em_andamento(modelo: str) -> bool:
    with _trava:
        return any(ollama.nome_completo(d["modelo"]) == ollama.nome_completo(modelo) for d in _ativos.values())


def iniciar(modelo: str, por: str, ip: str | None) -> int:
    with _trava:
        if any(ollama.nome_completo(d["modelo"]) == ollama.nome_completo(modelo) for d in _ativos.values()):
            raise ValueError(f"O download de {modelo} já está em andamento.")
        id_ = db.consultar_um(
            "insert into painel.downloads (modelo, status, iniciado_por) values (%s, 'baixando', %s) returning id",
            (modelo, por))["id"]
        _ativos[id_] = {"modelo": modelo, "total": None, "baixado": None, "mensagem": "iniciando"}
    auditoria.registrar("modelo_download_iniciado", por, ip, modelo, {"download": id_})
    threading.Thread(target=_executar, args=(id_, modelo, por), name=f"download-{id_}", daemon=True).start()
    return id_


def _executar(id_: int, modelo: str, por: str) -> None:
    camadas: dict[str, tuple[int, int]] = {}  # digest -> (total, concluído)
    ultimo = 0.0
    try:
        for evento in ollama.baixar(modelo):
            if evento.get("digest") and evento.get("total"):
                camadas[evento["digest"]] = (evento["total"], evento.get("completed") or 0)
            total = sum(t for t, _ in camadas.values()) or None
            baixado = sum(c for _, c in camadas.values()) if camadas else None
            mensagem = evento.get("status", "")
            with _trava:
                _ativos[id_].update(total=total, baixado=baixado, mensagem=mensagem)
            if time.monotonic() - ultimo > _GRAVAR_A_CADA_S:
                ultimo = time.monotonic()
                db.executar("update painel.downloads set total = %s, baixado = %s, mensagem = %s where id = %s",
                            (total, baixado, mensagem[:200], id_))
        db.executar("update painel.downloads set status = 'concluido', finalizado_em = now(), mensagem = 'ok', "
                    "baixado = total where id = %s", (id_,))
        auditoria.registrar("modelo_download_concluido", por, None, modelo, {"download": id_})
    except Exception as e:  # noqa: BLE001 - qualquer falha precisa encerrar o registro
        log.warning("Download %s (%s) falhou: %s", id_, modelo, e)
        db.executar("update painel.downloads set status = 'falhou', finalizado_em = now(), mensagem = %s "
                    "where id = %s", (str(e)[:300], id_))
        auditoria.registrar("modelo_download_falhou", por, None, modelo, {"download": id_, "erro": str(e)[:300]})
    finally:
        with _trava:
            _ativos.pop(id_, None)


def recentes(limite: int = 10) -> list[dict]:
    """Últimos downloads, com o progresso em memória dos que estão em andamento."""
    linhas = db.consultar("select * from painel.downloads order by id desc limit %s", (limite,))
    with _trava:
        for r in linhas:
            if r["id"] in _ativos:
                r.update(_ativos[r["id"]])
    for r in linhas:
        r["percentual"] = (round(100 * r["baixado"] / r["total"]) if r.get("total") and r.get("baixado") is not None
                           else None)
    return linhas
