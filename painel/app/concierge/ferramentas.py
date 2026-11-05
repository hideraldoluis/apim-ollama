"""Catálogo de ferramentas do concierge.

Leitura (`mutante=False`): executa na hora e devolve dados compactos ao modelo.
Escrita (`mutante=True`): o modelo só PROPÕE. `preparar` valida e resolve os alvos (sem alterar nada) e devolve
a proposta mostrada no cartão de confirmação; `executar` só roda depois que o usuário confirma (acoes.confirmar),
chamando os serviços de app/servicos (as mesmas regras das telas).

Regras gerais:
- ferramentas `admin=True` não são oferecidas ao modelo para o papel leitor e são recusadas de novo na execução;
- nada devolvido ao modelo inclui segredos (chaves sk-, senhas) nem texto de prompt/resposta dos logs;
- resultados são enxutos: o contexto do modelo é pequeno.
"""
from dataclasses import dataclass, field
from typing import Callable

from .. import auditoria, consultas, db, downloads, litellm, modelos_chaves, ollama, validacao
from ..config import config
from ..servicos import chaves as s_chaves, modelos as s_modelos, usuarios as s_usuarios

ORIGEM = "concierge"
MAX_LINHAS = 25


@dataclass
class Proposta:
    args: dict
    resumo: str
    campos: list[str] = field(default_factory=list)   # o que o humano preenche no cartão: motivo, confirmacao
    confirmar_texto: str | None = None                # o que deve ser digitado em "confirmacao"
    motivo_sugerido: str | None = None


@dataclass
class Ferramenta:
    nome: str
    descricao: str
    props: dict
    obrigatorios: list[str]
    admin: bool
    mutante: bool
    executar: Callable
    preparar: Callable | None = None

    def esquema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.nome, "description": self.descricao,
            "parameters": {"type": "object", "properties": self.props, "required": self.obrigatorios}}}


REGISTRO: dict[str, Ferramenta] = {}


def _registrar(nome, descricao, props=None, obrigatorios=(), *, admin=False, preparar=None):
    def deco(fn):
        REGISTRO[nome] = Ferramenta(nome, descricao, props or {}, list(obrigatorios), admin,
                                    preparar is not None, fn, preparar)
        return fn
    return deco


def leitura(nome, descricao, props=None, obrigatorios=(), admin=False):
    return _registrar(nome, descricao, props, obrigatorios, admin=admin)


def escrita(nome, descricao, props, obrigatorios, preparar):
    return _registrar(nome, "PROPÕE (o usuário confirma na tela): " + descricao, props, obrigatorios,
                      admin=True, preparar=preparar)


def _s(desc): return {"type": "string", "description": desc}
def _i(desc): return {"type": "integer", "description": desc}
def _b(desc): return {"type": "boolean", "description": desc}
def _e(valores, desc=""): return {"type": "string", "enum": list(valores), "description": desc}
def _l(desc): return {"type": "array", "items": {"type": "string"}, "description": desc}


def obter(nome: str) -> Ferramenta | None:
    return REGISTRO.get(nome)


def permitida(f: Ferramenta, ator: dict) -> bool:
    return not f.admin or ator["papel"] == "admin"


def disponiveis(ator: dict) -> list[Ferramenta]:
    return [f for f in REGISTRO.values() if permitida(f, ator)]


def esquemas(ator: dict) -> list[dict]:
    return [f.esquema() for f in disponiveis(ator)]


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------
def _iso(valor):
    return valor.isoformat(timespec="minutes") if valor else None


def _chave(ref) -> dict:
    """Resolve uma chave pelo nome (alias, sem diferenciar maiúsculas) ou pelo início do hash."""
    ref = str(ref or "").strip()
    if not ref:
        raise validacao.Invalido("Informe o nome da chave.")
    todas = consultas.chaves()
    achadas = [k for k in todas if (k["key_alias"] or "").casefold() == ref.casefold()]
    if not achadas and len(ref) >= 8:
        achadas = [k for k in todas if k["token"].startswith(ref.lower())]
    if len(achadas) == 1:
        return achadas[0]
    if achadas:
        raise validacao.Invalido(f"Mais de uma chave corresponde a '{ref}'. Seja mais específico.")
    parecidas = [k["key_alias"] for k in todas if k["key_alias"] and ref.casefold() in k["key_alias"].casefold()][:5]
    dica = f" Parecidas: {', '.join(parecidas)}." if parecidas else ""
    raise validacao.Invalido(f"Chave '{ref}' não encontrada.{dica}")


def _resumo_chave(k: dict) -> dict:
    return {"nome": k["key_alias"], "criado_para": k["criado_para"], "situacao": k["status"],
            "modelos": k["modelos_reais"] if not k["sem_acesso"] else [],
            "sem_acesso": k["sem_acesso"] or None, "todos_os_modelos": not (k["modelos"] or []) or None,
            "rpm": k["rpm_limit"], "simultaneas": k["max_parallel_requests"],
            "expira_em": _iso(k["expira_em"]), "ultimo_uso": _iso(k["ultimo_uso"])}


def _enxuto(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _periodo(desde, ate, padrao):
    return consultas.periodo(str(desde) if desde else None, str(ate) if ate else None, padrao)


def _publicado(nome: str) -> dict:
    nome = str(nome or "").strip()
    alvo = next((p for p in litellm.modelos_gateway() if p["nome"] == nome), None)
    if alvo is None:
        nomes = ", ".join(p["nome"] for p in litellm.modelos_gateway()) or "nenhum"
        raise validacao.Invalido(f"{nome} não está publicado no gateway. Publicados: {nomes}.")
    return alvo


def _instalado(nome: str) -> dict:
    nome = str(nome or "").strip()
    instalados = ollama.instalados()
    alvo = next((m for m in instalados if m["nome"] == nome or m["nome"] == ollama.nome_completo(nome)), None)
    if alvo is None:
        nomes = ", ".join(m["nome"] for m in instalados) or "nenhum"
        raise validacao.Invalido(f"{nome} não está instalado no Ollama. Instalados: {nomes}.")
    return alvo


def _usuario(login) -> dict:
    u = db.consultar_um("select id, login, nome, papel, ativo from painel.usuarios where login = %s",
                        (str(login or "").strip().lower(),))
    if u is None:
        raise validacao.Invalido(f"Usuário '{login}' não encontrado.")
    return u


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
@leitura("visao_geral", "Resumo do uso no período: requisições, tokens, erros, chaves que pedem atenção, maiores "
                        "consumidores e latência por modelo.",
         {"dias": _i("Últimos N dias (1 a 30, padrão 7).")})
def visao_geral(ator, ip, args):
    dias = min(max(int(args.get("dias") or 7), 1), 30)
    inicio, fim = consultas.periodo(None, None, dias)
    por_dia = consultas.consumo(inicio, fim, "dia")
    por_chave = consultas.consumo(inicio, fim, "chave")
    de, ate = consultas.limites(inicio, fim)
    modelos = db.consultar("select * from painel.resumo_modelos(%s, %s)", (de, ate))
    chaves = consultas.chaves()
    return {
        "periodo": f"{inicio} a {fim}",
        "requisicoes": sum(r["requisicoes"] for r in por_dia), "erros": sum(r["erros"] for r in por_dia),
        "tokens": sum(r["tok_total"] for r in por_dia),
        "chaves_validas": sum(1 for k in chaves if k["status"] in ("ativa", "expirando")),
        "chaves_que_pedem_atencao": [{"nome": k["key_alias"], "situacao": k["status"],
                                      "expira_em": _iso(k["expira_em"])}
                                     for k in chaves if k["status"] in ("expirando", "expirada", "bloqueada")][:10],
        "maiores_consumidores": [{"nome": r["alias"] or "(sem chave válida)", "requisicoes": r["requisicoes"],
                                  "tokens": r["tok_total"]} for r in por_chave[:5]],
        "modelos": [{"modelo": r["modelo"], "requisicoes": r["requisicoes"], "erros": r["erros"],
                     "p50_ms": r["p50_ms"], "p95_ms": r["p95_ms"]} for r in modelos[:10]],
    }


@leitura("consumo", "Consumo (requisições e tokens) por chave, modelo, chave+modelo ou dia.",
         {"visao": _e(["chave", "modelo", "chave_modelo", "dia"], "Agrupamento (padrão chave)."),
          "desde": _s("AAAA-MM-DD"), "ate": _s("AAAA-MM-DD"), "chave": _s("Filtrar por uma chave (nome).")})
def consumo(ator, ip, args):
    visao = args.get("visao") if args.get("visao") in ("chave", "modelo", "chave_modelo", "dia") else "chave"
    inicio, fim = _periodo(args.get("desde"), args.get("ate"), 30)
    token = _chave(args["chave"])["token"] if args.get("chave") else None
    linhas = consultas.consumo(inicio, fim, visao, chave=token)
    return {"periodo": f"{inicio} a {fim}", "agrupado_por": visao, "total_de_linhas": len(linhas),
            "linhas": [_enxuto({"dia": str(r["dia"]) if "dia" in r else None, "chave": r.get("alias"),
                                "modelo": r.get("modelo"), "requisicoes": r["requisicoes"], "erros": r["erros"],
                                "tokens": r["tok_total"]}) for r in linhas[:MAX_LINHAS]]}


@leitura("listar_chaves", "Lista chaves de consumidores com situação, modelos liberados, limites e validade.",
         {"situacao": _e(["ativa", "expirando", "expirada", "bloqueada"], "Filtro opcional."),
          "busca": _s("Trecho do nome."), "modelo": _s("Só chaves que liberam este modelo.")})
def listar_chaves(ator, ip, args):
    chaves = consultas.chaves()
    if args.get("situacao"):
        chaves = [k for k in chaves if k["status"] == args["situacao"]]
    if args.get("busca"):
        b = str(args["busca"]).casefold()
        chaves = [k for k in chaves if b in (k["key_alias"] or "").casefold() or b in (k["criado_para"] or "").casefold()]
    if args.get("modelo"):
        chaves = [k for k in chaves if args["modelo"] in k["modelos_reais"]]
    return {"total": len(chaves), "chaves": [_resumo_chave(k) for k in chaves[:MAX_LINHAS]]}


@leitura("detalhar_chave", "Detalhes de uma chave e seu consumo por modelo nos últimos 30 dias.",
         {"chave": _s("Nome da chave.")}, ["chave"])
def detalhar_chave(ator, ip, args):
    k = _chave(args["chave"])
    inicio, fim = consultas.periodo(None, None, 30)
    por_modelo = consultas.consumo(inicio, fim, "modelo", chave=k["token"])
    return {**_resumo_chave(k), "criada_em": _iso(k["criada_em"]),
            "consumo_30d_por_modelo": [{"modelo": r["modelo"], "requisicoes": r["requisicoes"],
                                        "erros": r["erros"], "tokens": r["tok_total"]} for r in por_modelo[:10]]}


@leitura("listar_logs", "Lista requisições recentes (metadados: quando, chave, modelo, status, tokens, duração). "
                        "Nunca traz o texto das mensagens.",
         {"desde": _s("AAAA-MM-DD (padrão hoje)"), "ate": _s("AAAA-MM-DD"), "chave": _s("Nome da chave."),
          "modelo": _s("Nome do modelo."), "status": _e(["success", "failure"]),
          "limite": _i("Até 20 (padrão 10).")})
def listar_logs(ator, ip, args):
    inicio, fim = _periodo(args.get("desde"), args.get("ate"), 1)
    de, ate = consultas.limites(inicio, fim)
    limite = min(max(int(args.get("limite") or 10), 1), 20)
    alias = _chave(args["chave"])["key_alias"] if args.get("chave") else None
    status = args.get("status") if args.get("status") in ("success", "failure") else None
    linhas = db.consultar("select * from painel.logs(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                          (de, ate, alias, None, args.get("modelo") or None, status, None, None, limite))
    return {"periodo": f"{inicio} a {fim}", "requisicoes": [
        _enxuto({"quando": _iso(r["quando"]), "chave": r["alias"], "modelo": r["modelo"], "status": r["status"],
                 "erro": r["codigo_erro"], "tokens": r["tok_total"], "duracao_ms": r["duracao_ms"]})
        for r in linhas]}


@leitura("listar_modelos", "Modelos instalados no Ollama, publicados no gateway e carregados na memória.")
def listar_modelos(ator, ip, args):
    instalados = ollama.instalados()
    carregados = ollama.carregados()
    publicados = litellm.modelos_gateway()
    chaves = [k for k in consultas.chaves() if k["status"] != "expirada"]
    return {
        "instalados": [{"nome": m["nome"], "parametros": m["parametros"], "tools": "tools" in m["capacidades"],
                        "embedding": ollama.e_embedding(m["capacidades"]), "carregado": m["nome"] in carregados,
                        "publicado_como": [p["nome"] for p in publicados
                                           if p["ollama"] and ollama.nome_completo(p["ollama"]) == m["nome"]]}
                       for m in instalados],
        "publicados": [{"nome": p["nome"], "tipo": "embedding" if p["embedding"] else "chat",
                        "chaves_com_acesso": sum(1 for k in chaves if p["nome"] in k["modelos_reais"])}
                       for p in publicados],
        "versao_ollama": ollama.versao()}


@leitura("chaves_que_liberam", "Quais chaves têm acesso a um modelo publicado.",
         {"modelo": _s("Nome público do modelo.")}, ["modelo"])
def chaves_que_liberam(ator, ip, args):
    nome = _publicado(args["modelo"])["nome"]
    vivas = [k for k in consultas.chaves() if k["status"] != "expirada"]
    return {"modelo": nome, "chaves": [k["key_alias"] for k in vivas if nome in k["modelos_reais"]],
            "chaves_sem_restricao_de_modelo": [k["key_alias"] for k in vivas if not (k["modelos"] or [])]}


@leitura("listar_downloads", "Downloads de modelos recentes e seu andamento.")
def listar_downloads(ator, ip, args):
    return {"downloads": [{"modelo": d["modelo"], "situacao": d["status"], "percentual": d.get("percentual"),
                           "mensagem": d.get("mensagem")} for d in downloads.recentes(5)]}


@leitura("listar_usuarios", "Usuários do painel (login, nome, papel, situação, último login).", admin=True)
def listar_usuarios(ator, ip, args):
    linhas = db.consultar(
        "select login, nome, papel, ativo, ultimo_login, bloqueado_ate is not null and bloqueado_ate > now() "
        "as bloqueado from painel.usuarios order by ativo desc, login limit %s", (MAX_LINHAS * 2,))
    return {"usuarios": [{"login": u["login"], "nome": u["nome"], "papel": u["papel"], "ativo": u["ativo"],
                          "bloqueado": u["bloqueado"] or None, "ultimo_login": _iso(u["ultimo_login"])}
                         for u in linhas]}


@leitura("consultar_auditoria", "Trilha de auditoria (quem fez o quê e quando).",
         {"acao": _e(sorted(auditoria.ACOES), "Tipo de evento."), "usuario": _s("Login."),
          "desde": _s("AAAA-MM-DD"), "ate": _s("AAAA-MM-DD"), "limite": _i("Até 20 (padrão 10).")}, admin=True)
def consultar_auditoria(ator, ip, args):
    inicio, fim = _periodo(args.get("desde"), args.get("ate"), 30)
    de, ate = consultas.limites(inicio, fim)
    acao = args.get("acao") if args.get("acao") in auditoria.ACOES else None
    limite = min(max(int(args.get("limite") or 10), 1), 20)
    linhas = db.consultar(
        "select quando, usuario, acao, alvo from painel.auditoria where quando >= %s and quando < %s "
        "and (%s::text is null or acao = %s) and (%s::text is null or usuario = %s) order by id desc limit %s",
        (de, ate, acao, acao, args.get("usuario") or None, args.get("usuario") or None, limite))
    return {"eventos": [_enxuto({"quando": _iso(r["quando"]), "usuario": r["usuario"],
                                 "acao": auditoria.ACOES.get(r["acao"], r["acao"]), "alvo": r["alvo"]})
                        for r in linhas]}


_DESTINOS = {
    "visao_geral": ("Visão geral", "/", False), "chat": ("Chat", "/chat", False),
    "chaves": ("Chaves", "/chaves", False), "consumo": ("Consumo", "/consumo", False),
    "logs": ("Logs", "/logs", False), "modelos": ("Modelos", "/modelos", False),
    "usuarios": ("Usuários", "/usuarios", True), "auditoria": ("Auditoria", "/auditoria", True),
    "retencao": ("Retenção", "/retencao", True), "revisao_de_acessos": ("Revisão de acessos", "/usuarios/revisao", True),
    "configuracao_do_concierge": ("Configuração do concierge", "/concierge/configuracao", True),
    "conteudo_dos_logs": ("Logs (texto das mensagens: só admin, na tela)", "/logs", False),
}


@leitura("navegar", "Devolve um link para uma tela do painel. Use para o que o concierge não faz "
                    "(ex.: ver o texto das mensagens nos logs, exportar CSV, revisão de acessos).",
         {"destino": _e(_DESTINOS, "Tela de destino.")}, ["destino"])
def navegar(ator, ip, args):
    rotulo, href, so_admin = _DESTINOS.get(args.get("destino"), (None, None, False))
    if rotulo is None:
        raise validacao.Invalido("Destino desconhecido.")
    if so_admin and ator["papel"] != "admin":
        raise validacao.Invalido("Esta tela é restrita a administradores.")
    return {"link": {"rotulo": rotulo, "href": href}, "mensagem": f"Link para {rotulo} exibido ao usuário."}


# ---------------------------------------------------------------------------
# Escrita: chaves
# ---------------------------------------------------------------------------
def _prep_criar_chave(ator, args):
    alias = validacao.alias(str(args.get("alias") or ""))
    if any((k["key_alias"] or "").casefold() == alias.casefold() for k in consultas.chaves()):
        raise validacao.Invalido(f"Já existe uma chave chamada '{alias}'.")
    modelos = validacao.modelos(list(args.get("modelos") or []), litellm.modelos_publicados())
    rpm = validacao.inteiro_positivo("RPM", args.get("rpm") or config.chave_rpm)
    par = validacao.inteiro_positivo("requisições simultâneas", args.get("paralelas") or config.chave_paralelas)
    validade = validacao.duracao(str(args.get("validade") or config.chave_validade))
    criado_para = validacao.alias(str(args.get("criado_para") or alias))
    return Proposta(
        {"alias": alias, "criado_para": criado_para, "modelos": modelos, "rpm": rpm, "paralelas": par,
         "validade": validade},
        f"Criar a chave '{alias}' (para {criado_para}) com os modelos {', '.join(modelos)}: {rpm} req/min, "
        f"{par} simultâneas, validade {validade}.")


@escrita("criar_chave", "criar uma chave de consumidor. O segredo aparece só para o usuário.",
         {"alias": _s("Nome da chave/consumidor."), "modelos": _l("Nomes públicos dos modelos liberados."),
          "criado_para": _s("Time/sistema dono (padrão = nome)."), "rpm": _i("Requisições por minuto."),
          "paralelas": _i("Requisições simultâneas."), "validade": _s("Ex.: 30d, 6mo, never.")},
         ["alias", "modelos"], _prep_criar_chave)
def criar_chave(ator, ip, a, extra):
    segredo, token, alias = s_chaves.criar(ator, ip, a["alias"], a["criado_para"], a["modelos"], a["rpm"],
                                           a["paralelas"], a["validade"], ORIGEM)
    return {"mensagem": f"Chave '{alias}' criada. Copie o segredo agora: ele não será mostrado de novo.",
            "segredos": [{"rotulo": f"Chave {alias}", "valor": segredo}], "link": {"rotulo": alias, "href": f"/chaves/{token}"}}


def _prep_alterar_chave(ator, args):
    k = _chave(args.get("chave"))
    novos = {}
    if args.get("modelos") is not None:
        novos["modelos"] = validacao.modelos(list(args["modelos"]), litellm.modelos_publicados() + k["modelos_reais"])
    if args.get("rpm") is not None:
        novos["rpm"] = validacao.inteiro_positivo("RPM", args["rpm"])
    if args.get("paralelas") is not None:
        novos["paralelas"] = validacao.inteiro_positivo("requisições simultâneas", args["paralelas"])
    if args.get("validade"):
        novos["validade"] = validacao.duracao(str(args["validade"]))
    if not novos:
        raise validacao.Invalido("Informe ao menos um campo a alterar (modelos, rpm, paralelas ou validade).")
    partes = []
    if "modelos" in novos:
        partes.append(f"modelos: {', '.join(k['modelos_reais']) or 'nenhum'} → {', '.join(novos['modelos'])}")
    if "rpm" in novos:
        partes.append(f"RPM: {k['rpm_limit']} → {novos['rpm']}")
    if "paralelas" in novos:
        partes.append(f"simultâneas: {k['max_parallel_requests']} → {novos['paralelas']}")
    if "validade" in novos:
        partes.append(f"validade: {novos['validade']} a partir de agora")
    return Proposta({"token": k["token"], "alias": k["key_alias"], **novos},
                    f"Alterar a chave '{k['key_alias']}': " + "; ".join(partes) + ".")


@escrita("alterar_chave", "alterar modelos (lista COMPLETA que a chave terá), RPM, simultâneas ou validade de uma chave.",
         {"chave": _s("Nome da chave."), "modelos": _l("Lista completa de modelos."), "rpm": _i("Req/min."),
          "paralelas": _i("Simultâneas."), "validade": _s("Nova validade a partir de agora (ex.: 90d, never).")},
         ["chave"], _prep_alterar_chave)
def alterar_chave(ator, ip, a, extra):
    _, mudancas = s_chaves.alterar(ator, ip, a["token"], a.get("modelos"), a.get("rpm"), a.get("paralelas"),
                                   a.get("validade"), ORIGEM)
    return {"mensagem": f"Chave '{a['alias']}' alterada." if mudancas else "Nada foi alterado (já estava assim).",
            "link": {"rotulo": a["alias"], "href": f"/chaves/{a['token']}"}}


def _prep_bloqueio(ator, args):
    k = _chave(args.get("chave"))
    if args.get("acao") == "desbloquear":
        return Proposta({"token": k["token"], "alias": k["key_alias"], "acao": "desbloquear"},
                        f"Desbloquear a chave '{k['key_alias']}'.")
    if args.get("acao") != "bloquear":
        raise validacao.Invalido("Ação deve ser 'bloquear' ou 'desbloquear'.")
    return Proposta({"token": k["token"], "alias": k["key_alias"], "acao": "bloquear"},
                    f"Bloquear a chave '{k['key_alias']}': as requisições com ela passam a ser recusadas.",
                    ["motivo"], motivo_sugerido=(str(args.get("motivo") or "").strip() or None))


@escrita("bloqueio_chave", "bloquear (exige motivo) ou desbloquear uma chave.",
         {"chave": _s("Nome da chave."), "acao": _e(["bloquear", "desbloquear"]),
          "motivo": _s("Motivo sugerido (o usuário confirma).")}, ["chave", "acao"], _prep_bloqueio)
def bloqueio_chave(ator, ip, a, extra):
    if a["acao"] == "bloquear":
        s_chaves.bloquear(ator, ip, a["token"], extra["motivo"], ORIGEM)
        return {"mensagem": f"Chave '{a['alias']}' bloqueada."}
    s_chaves.desbloquear(ator, ip, a["token"], ORIGEM)
    return {"mensagem": f"Chave '{a['alias']}' desbloqueada."}


def _prep_revogar(ator, args):
    k = _chave(args.get("chave"))
    return Proposta({"token": k["token"], "alias": k["key_alias"]},
                    f"REVOGAR a chave '{k['key_alias']}' (definitivo: os consumidores perdem o acesso).",
                    ["motivo", "confirmacao"], k["key_alias"], str(args.get("motivo") or "").strip() or None)


@escrita("revogar_chave", "revogar (apagar) uma chave, definitivo. Exige motivo e digitar o nome.",
         {"chave": _s("Nome da chave."), "motivo": _s("Motivo sugerido.")}, ["chave"], _prep_revogar)
def revogar_chave(ator, ip, a, extra):
    s_chaves.revogar(ator, ip, a["token"], extra["motivo"], extra["confirmacao"])
    return {"mensagem": f"Chave '{a['alias']}' revogada.", "link": {"rotulo": "Chaves revogadas", "href": "/chaves/revogadas"}}


def _prep_rotacionar(ator, args):
    k = _chave(args.get("chave"))
    dias = args.get("carencia_dias", config.rotacao_carencia_dias)
    if not str(dias).isdigit() or not 0 <= int(dias) <= 30:
        raise validacao.Invalido("Carência inválida: use de 0 a 30 dias.")
    validade = validacao.duracao(str(args.get("validade") or config.chave_validade))
    return Proposta({"token": k["token"], "alias": k["key_alias"], "carencia": int(dias), "validade": validade},
                    f"Rotacionar a chave '{k['key_alias']}': cria uma nova (validade {validade}) e revoga a antiga "
                    f"{'agora' if int(dias) == 0 else f'em {int(dias)} dia(s)'}.")


@escrita("rotacionar_chave", "rotacionar uma chave: cria a nova com os mesmos limites e revoga a antiga após a carência.",
         {"chave": _s("Nome da chave."), "carencia_dias": _i("0 a 30 (padrão da configuração)."),
          "validade": _s("Validade da nova chave.")}, ["chave"], _prep_rotacionar)
def rotacionar_chave(ator, ip, a, extra):
    r = s_chaves.rotacionar(ator, ip, a["token"], a["carencia"], a["validade"])
    quando = "revogada agora" if r["dias"] == 0 else f"será revogada em {r['dias']} dia(s)"
    return {"mensagem": f"Chave '{r['alias']}' rotacionada; a antiga {quando}. Copie o segredo da nova agora.",
            "segredos": [{"rotulo": f"Nova chave {r['alias']}", "valor": r["segredo"]}]}


def _prep_limpar(ator, args):
    n = sum(1 for k in consultas.chaves() if k["status"] == "expirada" and k["modelos_reais"])
    if not n:
        raise validacao.Invalido("Não há chaves expiradas com modelos para limpar.")
    return Proposta({}, f"Retirar os modelos de {n} chave(s) expirada(s) (elas ficam sem acesso).")


@escrita("limpar_chaves_expiradas", "retirar os modelos de todas as chaves expiradas.", {}, [], _prep_limpar)
def limpar_chaves_expiradas(ator, ip, a, extra):
    alteradas, falhas = s_chaves.limpar_expiradas(ator, ip)
    if falhas:
        return {"mensagem": f"{alteradas} chave(s) limpa(s); falharam: {', '.join(falhas[:5])}."}
    return {"mensagem": f"Modelos retirados de {alteradas} chave(s) expirada(s)."}


# ---------------------------------------------------------------------------
# Escrita: modelos
# ---------------------------------------------------------------------------
def _prep_ajustar(ator, args):
    nome = _publicado(args.get("modelo"))["nome"]
    if args.get("acao") not in ("liberar", "retirar"):
        raise validacao.Invalido("Ação deve ser 'liberar' ou 'retirar'.")
    todas = consultas.chaves()
    elegiveis = modelos_chaves.elegiveis(todas)
    if args.get("todas"):
        alvos = elegiveis
    else:
        alvos = [_chave(r) for r in (args.get("chaves") or [])]
        if not alvos:
            raise validacao.Invalido("Informe as chaves (nomes) ou use todas=true.")
        fora = [k["key_alias"] for k in alvos if k not in elegiveis]
        if fora:
            raise validacao.Invalido("Estas chaves não podem ser ajustadas (expiradas ou sem lista de modelos): "
                                     + ", ".join(fora))
    marcadas = {k["token"] for k in elegiveis if nome in k["modelos_reais"]}
    tokens = {k["token"] for k in alvos}
    marcadas = marcadas | tokens if args["acao"] == "liberar" else marcadas - tokens
    plano = modelos_chaves.planejar(nome, todas, marcadas)
    if not plano.alteracoes and not plano.bloqueadas:
        raise validacao.Invalido("Nada mudaria: as chaves já estão assim.")
    texto = (f"{'Liberar' if args['acao'] == 'liberar' else 'Retirar'} o modelo '{nome}': "
             f"{len(plano.adicionar)} chave(s) ganham acesso, {len(plano.retirar)} perdem")
    if plano.bloqueadas:
        texto += f"; {len(plano.bloqueadas)} ficariam sem nenhum modelo e serão mantidas ("
        texto += ", ".join(i["chave"]["key_alias"] for i in plano.bloqueadas[:5]) + ")"
    afetadas = [i["chave"]["key_alias"] for i in plano.alteracoes][:8]
    return Proposta({"modelo": nome, "marcadas": sorted(marcadas)},
                    texto + (f". Afetadas: {', '.join(afetadas)}{'…' if len(plano.alteracoes) > 8 else ''}." if afetadas else "."))


@escrita("ajustar_modelo_em_chaves", "liberar ou retirar um modelo publicado em várias chaves de uma vez.",
         {"modelo": _s("Nome público do modelo."), "acao": _e(["liberar", "retirar"]),
          "chaves": _l("Nomes das chaves."), "todas": _b("Aplicar a todas as chaves elegíveis.")},
         ["modelo", "acao"], _prep_ajustar)
def ajustar_modelo_em_chaves(ator, ip, a, extra):
    plano, res = s_modelos.ajustar_chaves(ator, ip, a["modelo"], set(a["marcadas"]), ORIGEM)
    return {"mensagem": modelos_chaves.resumo(plano, res)}


def _prep_baixar(ator, args):
    modelo = validacao.modelo_ollama(str(args.get("modelo") or ""))
    return Proposta({"modelo": modelo}, f"Baixar o modelo '{modelo}' do catálogo do Ollama (pode levar vários minutos).")


@escrita("baixar_modelo", "baixar um modelo do catálogo do Ollama.", {"modelo": _s("Ex.: llama3.2, qwen2.5:7b.")},
         ["modelo"], _prep_baixar)
def baixar_modelo(ator, ip, a, extra):
    s_modelos.baixar(ator, ip, a["modelo"])
    return {"mensagem": f"Download de {a['modelo']} iniciado. O progresso aparece em Modelos.",
            "link": {"rotulo": "Modelos", "href": "/modelos"}}


def _prep_publicar(ator, args):
    inst = _instalado(args.get("modelo"))
    sugestao = inst["nome"].removesuffix(":latest").rsplit("/", 1)[-1]
    publico = validacao.modelo_publico(str(args.get("nome_publico") or sugestao))
    if any(p["nome"] == publico for p in litellm.modelos_gateway()):
        raise validacao.Invalido(f"Já existe um modelo publicado como {publico}.")
    return Proposta({"modelo": inst["nome"], "nome_publico": publico},
                    f"Publicar '{inst['nome']}' no gateway como '{publico}'.")


@escrita("publicar_modelo", "publicar no gateway um modelo já instalado no Ollama.",
         {"modelo": _s("Nome instalado no Ollama."), "nome_publico": _s("Nome que os consumidores usarão.")},
         ["modelo"], _prep_publicar)
def publicar_modelo(ator, ip, a, extra):
    s_modelos.publicar(ator, ip, a["modelo"], a["nome_publico"], ORIGEM)
    return {"mensagem": f"{a['modelo']} publicado como {a['nome_publico']}. Libere-o nas chaves que devem usá-lo."}


def _prep_despublicar(ator, args):
    alvo = _publicado(args.get("modelo"))
    total = bool(args.get("retirar_das_chaves"))
    em_uso = [k["key_alias"] for k in consultas.chaves() if k["status"] != "expirada" and alvo["nome"] in (k["modelos"] or [])]
    if em_uso and not total:
        raise validacao.Invalido(f"{alvo['nome']} está liberado em: {', '.join(em_uso[:8])}. Para retirar mesmo assim, "
                                 "use retirar_das_chaves=true (ou ajuste as chaves antes).")
    texto = f"Retirar '{alvo['nome']}' do gateway"
    if total and em_uso:
        texto += f", antes retirando-o de {len(em_uso)} chave(s) ({', '.join(em_uso[:8])}); chaves que ficarem sem modelo perdem o acesso"
    return Proposta({"id": alvo["id"], "nome": alvo["nome"], "total": total}, texto + ". O modelo continua instalado no Ollama.",
                    ["confirmacao"], alvo["nome"])


@escrita("despublicar_modelo", "retirar um modelo do gateway (continua instalado). Exige digitar o nome.",
         {"modelo": _s("Nome público."), "retirar_das_chaves": _b("Retirar antes de todas as chaves que o liberam.")},
         ["modelo"], _prep_despublicar)
def despublicar_modelo(ator, ip, a, extra):
    if a["total"]:
        _, n = s_modelos.despublicar_total(ator, ip, a["id"], extra["confirmacao"], ORIGEM)
        return {"mensagem": f"{a['nome']} retirado de {n} chave(s) e do gateway."}
    s_modelos.despublicar(ator, ip, a["id"], extra["confirmacao"], ORIGEM)
    return {"mensagem": f"{a['nome']} retirado do gateway."}


def _prep_remover(ator, args):
    inst = _instalado(args.get("modelo"))
    return Proposta({"modelo": inst["nome"]}, f"REMOVER '{inst['nome']}' do Ollama (apaga os arquivos do disco).",
                    ["confirmacao"], inst["nome"])


@escrita("remover_modelo", "apagar um modelo do Ollama (precisa estar despublicado). Exige digitar o nome.",
         {"modelo": _s("Nome instalado no Ollama.")}, ["modelo"], _prep_remover)
def remover_modelo(ator, ip, a, extra):
    s_modelos.remover(ator, ip, a["modelo"], extra["confirmacao"], ORIGEM)
    return {"mensagem": f"{a['modelo']} removido do Ollama."}


def _prep_memoria(ator, args):
    inst = _instalado(args.get("modelo"))
    if args.get("acao") not in ("carregar", "descarregar"):
        raise validacao.Invalido("Ação deve ser 'carregar' ou 'descarregar'.")
    return Proposta({"modelo": inst["nome"], "acao": args["acao"]},
                    f"{'Carregar' if args['acao'] == 'carregar' else 'Descarregar'} '{inst['nome']}' "
                    f"{'na' if args['acao'] == 'carregar' else 'da'} memória do Ollama"
                    + (" (pode descarregar outro modelo e demorar)." if args["acao"] == "carregar" else "."))


@escrita("memoria_modelo", "carregar um modelo na memória (aquecer) ou descarregá-lo.",
         {"modelo": _s("Nome instalado no Ollama."), "acao": _e(["carregar", "descarregar"])},
         ["modelo", "acao"], _prep_memoria)
def memoria_modelo(ator, ip, a, extra):
    s_modelos.memoria(ator, ip, a["modelo"], a["acao"] == "carregar", ORIGEM)
    return {"mensagem": f"{a['modelo']} {'carregado' if a['acao'] == 'carregar' else 'descarregado'}."}


# ---------------------------------------------------------------------------
# Escrita: usuários
# ---------------------------------------------------------------------------
def _alvo_usuario(ator, args) -> dict:
    u = _usuario(args.get("login"))
    if u["id"] == ator["id"]:
        raise validacao.Invalido("Você não pode executar esta operação sobre o próprio usuário.")
    return u


def _prep_criar_usuario(ator, args):
    login = validacao.login(str(args.get("login") or ""))
    nome = validacao.nome(str(args.get("nome") or ""))
    papel = validacao.papel(str(args.get("papel") or "leitor"))
    if db.consultar_um("select 1 as x from painel.usuarios where login = %s", (login,)):
        raise validacao.Invalido("Já existe um usuário com esse login (inclusive desativado).")
    return Proposta({"login": login, "nome": nome, "papel": papel},
                    f"Criar o usuário '{login}' ({nome}) com papel {papel} e senha temporária de {config.senha_temp_validade_h} h.")


@escrita("criar_usuario", "criar um usuário do painel; a senha temporária aparece só para o admin.",
         {"login": _s("a-z, 0-9, ponto, hífen, sublinhado."), "nome": _s("Nome completo."),
          "papel": _e(["leitor", "admin"])}, ["login", "nome"], _prep_criar_usuario)
def criar_usuario(ator, ip, a, extra):
    alvo, temp = s_usuarios.criar(ator, ip, a["login"], a["nome"], a["papel"], ORIGEM)
    return {"mensagem": f"Usuário {alvo['login']} criado. Entregue a senha temporária (vale {config.senha_temp_validade_h} h).",
            "segredos": [{"rotulo": f"Senha temporária de {alvo['login']}", "valor": temp}]}


def _prep_papel(ator, args):
    u = _alvo_usuario(ator, args)
    papel = validacao.papel(str(args.get("papel") or ""))
    return Proposta({"id": u["id"], "login": u["login"], "papel": papel},
                    f"Mudar o papel de '{u['login']}' de {u['papel']} para {papel} (as sessões dele são encerradas).")


@escrita("mudar_papel_usuario", "alterar o papel (admin/leitor) de um usuário.",
         {"login": _s("Login."), "papel": _e(["leitor", "admin"])}, ["login", "papel"], _prep_papel)
def mudar_papel_usuario(ator, ip, a, extra):
    mudou = s_usuarios.alterar_papel(ator, ip, a["id"], a["papel"], ORIGEM)
    return {"mensagem": f"Papel de {a['login']} alterado para {a['papel']}." if mudou else "Nada foi alterado."}


def _prep_simples(texto):
    def prep(ator, args):
        u = _alvo_usuario(ator, args)
        return Proposta({"id": u["id"], "login": u["login"]}, texto.format(login=u["login"]))
    return prep


@escrita("redefinir_senha_usuario", "redefinir a senha de um usuário (gera uma temporária).",
         {"login": _s("Login.")}, ["login"],
         _prep_simples("Redefinir a senha de '{login}' (gera senha temporária e encerra as sessões dele)."))
def redefinir_senha_usuario(ator, ip, a, extra):
    _, temp = s_usuarios.redefinir_senha(ator, ip, a["id"], ORIGEM)
    return {"mensagem": f"Senha de {a['login']} redefinida.",
            "segredos": [{"rotulo": f"Senha temporária de {a['login']}", "valor": temp}]}


@escrita("desbloquear_usuario", "desbloquear um usuário bloqueado por tentativas de login.",
         {"login": _s("Login.")}, ["login"], _prep_simples("Desbloquear o usuário '{login}'."))
def desbloquear_usuario(ator, ip, a, extra):
    s_usuarios.desbloquear(ator, ip, a["id"], ORIGEM)
    return {"mensagem": f"Usuário {a['login']} desbloqueado."}


@escrita("reativar_usuario", "reativar um usuário desativado (gera senha temporária).",
         {"login": _s("Login.")}, ["login"], _prep_simples("Reativar o usuário '{login}' com nova senha temporária."))
def reativar_usuario(ator, ip, a, extra):
    alvo, temp = s_usuarios.reativar(ator, ip, a["id"], ORIGEM)
    if alvo is None:
        return {"mensagem": f"{a['login']} já estava ativo."}
    return {"mensagem": f"Usuário {a['login']} reativado.",
            "segredos": [{"rotulo": f"Senha temporária de {a['login']}", "valor": temp}]}


def _prep_desativar(ator, args):
    u = _alvo_usuario(ator, args)
    return Proposta({"id": u["id"], "login": u["login"]}, f"Desativar o usuário '{u['login']}' (encerra as sessões; não apaga o registro).",
                    ["motivo"], motivo_sugerido=str(args.get("motivo") or "").strip() or None)


@escrita("desativar_usuario", "desativar um usuário (exige motivo).",
         {"login": _s("Login."), "motivo": _s("Motivo sugerido.")}, ["login"], _prep_desativar)
def desativar_usuario(ator, ip, a, extra):
    mudou = s_usuarios.desativar(ator, ip, a["id"], extra["motivo"], ORIGEM)
    return {"mensagem": f"Usuário {a['login']} desativado." if mudou else f"{a['login']} já estava desativado."}
