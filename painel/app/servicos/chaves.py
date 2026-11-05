"""Chaves de consumidores: criação, alteração, bloqueio, revogação e rotação."""
from datetime import datetime, timedelta

from .. import auditoria, consultas, db, litellm, rotinas, validacao
from ..config import config
from . import NaoEncontrado, com_origem


def obter(token: str) -> dict:
    k = consultas.chave(token)
    if k is None:
        raise NaoEncontrado("Chave não encontrada.")
    return k


def disponiveis() -> tuple[list[str], str | None]:
    try:
        return litellm.modelos_publicados(), None
    except litellm.ErroLiteLLM as e:
        return [], str(e)


def criar(ator: dict, ip: str | None, alias: str, criado_para: str, modelos: list[str], rpm, paralelas,
          validade: str, origem: str | None = None) -> tuple[str, str, str]:
    """Devolve (segredo, token, alias). O segredo existe só neste retorno."""
    publicados, _ = disponiveis()
    alias = validacao.alias(alias)
    criado_para = validacao.alias(criado_para or alias)
    modelos = validacao.modelos(modelos, publicados)
    rpm = validacao.inteiro_positivo("RPM", rpm)
    paralelas = validacao.inteiro_positivo("requisições simultâneas", paralelas)
    validade = validacao.duracao(validade)
    segredo, token = litellm.gerar_chave(alias, modelos, rpm, paralelas, validade, criado_para, ator["login"])
    auditoria.registrar("chave_criada", ator["login"], ip, alias, com_origem({
        "token": token, "criado_para": criado_para, "modelos": modelos, "rpm": rpm,
        "paralelas": paralelas, "validade": validade}, origem))
    return segredo, token, alias


def alterar(ator: dict, ip: str | None, token: str, modelos: list[str] | None = None, rpm=None,
            paralelas=None, validade: str | None = None, origem: str | None = None) -> tuple[dict, dict]:
    """Altera só o que foi informado (None = manter). Devolve (chave, mudanças); mudanças vazio = nada mudou."""
    k = obter(token)
    publicados, _ = disponiveis()
    mudancas: dict = {}
    if modelos is not None:
        # Modelos já liberados continuam aceitos mesmo que tenham saído do gateway.
        modelos = validacao.modelos(modelos, publicados + k["modelos_reais"])
        if sorted(modelos) != sorted(k["modelos"] or []):
            mudancas["modelos"] = modelos
    if rpm is not None:
        rpm_novo = validacao.inteiro_positivo("RPM", rpm)
        if rpm_novo != k["rpm_limit"]:
            mudancas["rpm"] = rpm_novo
    if paralelas is not None:
        paralelas_novo = validacao.inteiro_positivo("requisições simultâneas", paralelas)
        if paralelas_novo != k["max_parallel_requests"]:
            mudancas["paralelas"] = paralelas_novo
    if validade is not None and validade.strip():
        mudancas["validade"] = validacao.duracao(validade)
    if not mudancas:
        return k, {}
    litellm.alterar_chave(token, **mudancas)
    antes = {"modelos": k["modelos"], "rpm": k["rpm_limit"], "paralelas": k["max_parallel_requests"],
             "expira_em": k["expira_em"].isoformat() if k["expira_em"] else None}
    auditoria.registrar("chave_alterada", ator["login"], ip, k["key_alias"],
                        com_origem({"token": token, "antes": antes, "depois": mudancas}, origem))
    return k, mudancas


def bloquear(ator: dict, ip: str | None, token: str, motivo: str, origem: str | None = None) -> dict:
    k = obter(token)
    motivo = validacao.motivo(motivo)
    litellm.bloquear(token)
    auditoria.registrar("chave_bloqueada", ator["login"], ip, k["key_alias"],
                        com_origem({"token": token, "motivo": motivo}, origem))
    return k


def desbloquear(ator: dict, ip: str | None, token: str, origem: str | None = None) -> dict:
    k = obter(token)
    litellm.desbloquear(token)
    auditoria.registrar("chave_desbloqueada", ator["login"], ip, k["key_alias"],
                        com_origem({"token": token}, origem))
    return k


def revogar(ator: dict, ip: str | None, token: str, motivo: str, confirmacao: str) -> dict:
    k = obter(token)
    motivo = validacao.motivo(motivo)
    if confirmacao.strip() != (k["key_alias"] or ""):
        raise validacao.Invalido("Para revogar, digite o nome da chave exatamente como aparece.")
    rotinas.revogar_com_historico(token, ator["login"], ip, motivo)
    db.executar("update painel.revogacoes_agendadas set executada_em = now() where token = %s and executada_em is null",
                (token,))
    return k


def rotacionar(ator: dict, ip: str | None, token: str, carencia, validade: str) -> dict:
    """Cria a chave nova (mesmo alias e limites) e revoga a antiga após a carência.

    Devolve {"segredo", "token_novo", "alias", "alias_antigo", "dias", "revoga_em"}.
    """
    k = obter(token)
    carencia = str(carencia)
    dias = int(carencia) if carencia.strip().isdigit() else -1
    if not 0 <= dias <= 30:
        raise validacao.Invalido("Carência inválida: use de 0 a 30 dias.")
    validade = validacao.duracao(validade or config.chave_validade)
    alias = k["key_alias"] or k["criado_para"] or token[:12]

    # O alias é único: a chave antiga recebe um sufixo antes da nova ser criada com o nome original.
    alias_antigo = f"{alias} (substituída {datetime.now(config.tz):%Y-%m-%d %H%M})"
    litellm.alterar_chave(token, alias=alias_antigo)
    try:
        segredo, novo = litellm.gerar_chave(alias, k["modelos"] or [], k["rpm_limit"] or config.chave_rpm,
                                            k["max_parallel_requests"] or config.chave_paralelas, validade,
                                            k["criado_para"] or alias, ator["login"])
    except litellm.ErroLiteLLM:
        try:
            litellm.alterar_chave(token, alias=alias)
        except litellm.ErroLiteLLM:
            pass
        raise

    motivo = f"Rotação: substituída pela chave {novo[:12]}…"
    login = ator["login"]
    auditoria.registrar("chave_rotacionada", login, ip, alias,
                        {"token": token, "token_novo": novo, "carencia_dias": dias, "validade": validade})
    auditoria.registrar("chave_criada", login, ip, alias,
                        {"token": novo, "rotacao_de": token, "modelos": k["modelos"], "validade": validade})
    pendente = dias
    if dias == 0:
        try:
            rotinas.revogar_com_historico(token, login, ip, motivo)
        except litellm.ErroLiteLLM:
            pendente = None  # cai no agendamento abaixo; a rotina tenta de novo
    if pendente != 0:
        db.executar(
            "insert into painel.revogacoes_agendadas (token, key_alias, executar_em, agendada_por, motivo) "
            "values (%s, %s, now() + make_interval(days => %s), %s, %s) on conflict (token) do nothing",
            (token, alias_antigo, pendente or 0, login, motivo))
    return {"segredo": segredo, "token_novo": novo, "alias": alias, "alias_antigo": alias_antigo, "dias": dias,
            "revoga_em": None if pendente == 0 else datetime.now(config.tz) + timedelta(days=pendente or 0)}


def limpar_expiradas(ator: dict, ip: str | None) -> tuple[int, list[str]]:
    return rotinas.limpar_modelos_expirados(ator["login"], ip)
