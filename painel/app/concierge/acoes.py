"""Ações propostas pelo concierge que aguardam confirmação humana (painel.concierge_acoes).

O modelo nunca executa uma mutação: ele só gera a proposta. A execução acontece em confirmar(), chamada
por um POST do próprio usuário (cookie + CSRF), que revalida o papel e roda o serviço com as mesmas regras
das telas. Cada proposta vale uma vez só e expira em EXPIRA_MIN minutos.
"""
from psycopg.types.json import Jsonb

from .. import auditoria, db, litellm, ollama, validacao
from ..servicos import NaoEncontrado, RegraViolada
from . import ferramentas

EXPIRA_MIN = 10
MAX_PENDENTES = 8
_ERROS = (validacao.Invalido, RegraViolada, NaoEncontrado, litellm.ErroLiteLLM, ollama.ErroOllama)


class AcaoInvalida(Exception):
    """Proposta inexistente, de outro usuário, já resolvida ou expirada."""


def propor(ator: dict, ip: str | None, ferramenta: ferramentas.Ferramenta, args: dict) -> dict:
    """Valida os argumentos, grava a proposta e devolve os dados para o cartão de confirmação."""
    prop = ferramenta.preparar(ator, args)
    pendentes = db.consultar_um(
        "select count(*) as n from painel.concierge_acoes "
        "where usuario_id = %s and estado = 'pendente' and expira_em > now()", (ator["id"],))["n"]
    if pendentes >= MAX_PENDENTES:
        raise validacao.Invalido("Há ações demais aguardando confirmação. Confirme ou cancele algumas antes.")
    linha = db.consultar_um(
        "insert into painel.concierge_acoes (usuario_id, ferramenta, args, resumo, expira_em) "
        "values (%s, %s, %s, %s, now() + make_interval(mins => %s)) returning id::text, expira_em",
        (ator["id"], ferramenta.nome, Jsonb(prop.args), prop.resumo, EXPIRA_MIN))
    auditoria.registrar("concierge_proposta", ator["login"], ip, ferramenta.nome,
                        {"acao_id": linha["id"], "resumo": prop.resumo})
    return {"acao_id": linha["id"], "ferramenta": ferramenta.nome, "resumo": prop.resumo,
            "campos": prop.campos, "confirmar_texto": prop.confirmar_texto,
            "motivo_sugerido": prop.motivo_sugerido, "expira_em": linha["expira_em"].isoformat()}


def _reservar(ator: dict, acao_id: str, novo_estado: str) -> dict:
    """Muda pendente -> novo_estado de forma atômica (garante execução única)."""
    try:
        linha = db.consultar_um(
            "update painel.concierge_acoes set estado = %s, resolvida_em = now() "
            "where id = %s::uuid and usuario_id = %s and estado = 'pendente' and expira_em > now() "
            "returning id::text, ferramenta, args, resumo", (novo_estado, acao_id, ator["id"]))
    except Exception:  # id malformado (cast para uuid falhou)
        linha = None
    if linha is None:
        raise AcaoInvalida("Esta ação não existe mais, já foi resolvida ou expirou.")
    return linha


def cancelar(ator: dict, ip: str | None, acao_id: str) -> None:
    linha = _reservar(ator, acao_id, "cancelada")
    auditoria.registrar("concierge_cancelada", ator["login"], ip, linha["ferramenta"], {"acao_id": acao_id})


def confirmar(ator: dict, ip: str | None, acao_id: str, motivo: str, confirmacao: str) -> dict:
    """Executa a ação. Devolve {"ok", "mensagem", "segredos"?, "link"?}; segredos nunca vão ao banco."""
    ferramenta = ferramentas.obter(linha_ferramenta(acao_id, ator))
    if ferramenta is None or not ferramentas.permitida(ferramenta, ator):
        raise AcaoInvalida("Você não tem permissão para esta ação.")
    linha = _reservar(ator, acao_id, "confirmada")
    try:
        resultado = ferramenta.executar(ator, ip, linha["args"], {"motivo": motivo, "confirmacao": confirmacao})
    except validacao.Invalido as e:
        # Entrada recusada (ex.: nome digitado errado): nada foi executado, a proposta continua valendo.
        db.executar("update painel.concierge_acoes set estado = 'pendente', resolvida_em = null where id = %s::uuid",
                    (acao_id,))
        return {"ok": False, "mensagem": str(e), "repetir": True}
    except _ERROS as e:
        _registrar_resultado(acao_id, "falhou", {"erro": str(e)})
        auditoria.registrar("concierge_falhou", ator["login"], ip, ferramenta.nome,
                            {"acao_id": acao_id, "erro": str(e)})
        return {"ok": False, "mensagem": str(e)}
    segredos = resultado.pop("segredos", [])
    _registrar_resultado(acao_id, "confirmada", {"mensagem": resultado.get("mensagem")})
    auditoria.registrar("concierge_confirmada", ator["login"], ip, ferramenta.nome,
                        {"acao_id": acao_id, "resumo": linha["resumo"]})
    return {"ok": True, **resultado, **({"segredos": segredos} if segredos else {})}


def linha_ferramenta(acao_id: str, ator: dict) -> str:
    """Nome da ferramenta de uma proposta do usuário (sem alterar o estado)."""
    try:
        linha = db.consultar_um("select ferramenta from painel.concierge_acoes where id = %s::uuid and usuario_id = %s",
                                (acao_id, ator["id"]))
    except Exception:
        linha = None
    if linha is None:
        raise AcaoInvalida("Esta ação não existe mais, já foi resolvida ou expirou.")
    return linha["ferramenta"]


def _registrar_resultado(acao_id: str, estado: str, resultado: dict) -> None:
    db.executar("update painel.concierge_acoes set estado = %s, resultado = %s where id = %s::uuid",
                (estado, Jsonb(resultado), acao_id))
