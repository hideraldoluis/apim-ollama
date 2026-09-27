"""Rotinas periódicas do painel (thread única, a cada 15 minutos e na subida).

- consolida o consumo diário (antes que a retenção de 180 dias apague os logs);
- executa revogações agendadas pela rotação de chaves;
- desativa usuários sem login há PAINEL_INATIVIDADE_DIAS (nunca o último admin ativo);
- remove sessões expiradas.
"""
import logging
import threading
from datetime import timedelta

from psycopg.types.json import Jsonb

from . import auditoria, db, litellm
from .config import config
from .consultas import hoje

log = logging.getLogger("painel.rotinas")
_parar = threading.Event()
_INTERVALO_S = 15 * 60
# Na primeira consolidação, recupera o que ainda existe nos logs (retenção de 180 dias + folga).
_HISTORICO_INICIAL_DIAS = 190


def consolidar_consumo() -> None:
    fim = hoje() - timedelta(days=1)
    with db.transacao() as conn:
        linha = conn.execute("select ate from painel.consolidacao for update").fetchone()
        # Reprocessa o último dia consolidado: logs podem chegar com atraso de alguns segundos.
        inicio = (linha["ate"] - timedelta(days=1)) if linha else (fim - timedelta(days=_HISTORICO_INICIAL_DIAS))
        if inicio > fim:
            return
        conn.execute(
            """
            insert into painel.consumo_diario
                (dia, chave, alias, modelo, requisicoes, erros, tok_prompt, tok_resposta, tok_total)
            select * from painel.agregado_diario(%s, %s, %s)
            on conflict (dia, chave, alias, modelo) do update set
                requisicoes = excluded.requisicoes, erros = excluded.erros,
                tok_prompt = excluded.tok_prompt, tok_resposta = excluded.tok_resposta,
                tok_total = excluded.tok_total
            """,
            (inicio, fim, config.fuso),
        )
        conn.execute(
            "insert into painel.consolidacao (id, ate) values (1, %s) "
            "on conflict (id) do update set ate = excluded.ate",
            (fim,),
        )


def executar_revogacoes() -> None:
    pendentes = db.consultar(
        "select * from painel.revogacoes_agendadas where executada_em is null and executar_em <= now()")
    for r in pendentes:
        try:
            revogar_com_historico(r["token"], "sistema", None, r["motivo"])
        except litellm.ErroLiteLLM as e:
            log.warning("Revogação agendada de %s falhou: %s", r["key_alias"], e)
            auditoria.registrar("chave_revogacao_agendada_falhou", "sistema", None, r["key_alias"], {"erro": str(e)})
            continue
        db.executar("update painel.revogacoes_agendadas set executada_em = now() where token = %s", (r["token"],))


def revogar_com_historico(token: str, por: str, ip: str | None, motivo: str) -> bool:
    """Guarda o retrato da chave e a revoga no gateway. False se a chave já não existia."""
    retrato = db.consultar_um("select painel.chave_retrato(%s) as r", (token,))["r"]
    if retrato is None:
        return False
    # Histórico e revogação na mesma transação: se o gateway recusar, nada é gravado.
    alias = retrato.get("key_alias")
    with db.transacao() as conn:
        conn.execute(
            "insert into painel.chaves_historico (token, key_alias, criado_para, dados, revogada_por, motivo) "
            "values (%s, %s, %s, %s, %s, %s)",
            (token, alias, (retrato.get("metadata") or {}).get("created_for"), Jsonb(retrato), por, motivo),
        )
        litellm.revogar(token)
        auditoria.registrar("chave_revogada", por, ip, alias, {"token": token, "motivo": motivo}, conn)
    return True


def desativar_inativos() -> None:
    with db.transacao() as conn:
        # Trava os usuários para que a regra do último admin não sofra corrida com a tela.
        usuarios = conn.execute(
            "select id, login, papel, ativo, "
            "greatest(ultimo_login, criado_em, reativado_em) < now() - make_interval(days => %s) as inativo "
            "from painel.usuarios order by id for update",
            (config.inatividade_desativa_dias,),
        ).fetchall()
        admins_ativos = sum(1 for u in usuarios if u["ativo"] and u["papel"] == "admin")
        for u in usuarios:
            if not (u["ativo"] and u["inativo"]):
                continue
            if u["papel"] == "admin":
                if admins_ativos <= 1:
                    continue
                admins_ativos -= 1
            conn.execute(
                "update painel.usuarios set ativo = false, desativado_em = now(), desativado_por = 'sistema', "
                "motivo_desativacao = %s where id = %s",
                (f"Sem login há mais de {config.inatividade_desativa_dias} dias", u["id"]),
            )
            conn.execute("delete from painel.sessoes where usuario_id = %s", (u["id"],))
            auditoria.registrar("usuario_desativado", "sistema", None, u["login"], {"motivo": "inatividade"}, conn)


def limpar_sessoes() -> None:
    db.executar(
        "delete from painel.sessoes where ultimo_uso < now() - make_interval(mins => %s) "
        "or criada_em < now() - make_interval(hours => %s)",
        (config.sessao_inatividade_min, config.sessao_maxima_h),
    )


def rodar_uma_vez() -> None:
    for tarefa in (consolidar_consumo, executar_revogacoes, desativar_inativos, limpar_sessoes):
        try:
            tarefa()
        except Exception:
            log.exception("Rotina %s falhou", tarefa.__name__)


def _laco() -> None:
    while not _parar.is_set():
        rodar_uma_vez()
        _parar.wait(_INTERVALO_S)


def iniciar() -> None:
    threading.Thread(target=_laco, name="painel-rotinas", daemon=True).start()


def parar() -> None:
    _parar.set()
