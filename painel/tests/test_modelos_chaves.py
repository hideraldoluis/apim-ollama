"""Teste da liberação/retirada de modelos em massa, sem serviços externos (gateway e banco simulados).

Cobre o cálculo da diferença, a regra da última permissão, chaves "todos"/expiradas, falha parcial,
a limpeza das chaves expiradas (SEM_ACESSO) e a renderização das telas.
  python painel/tests/test_modelos_chaves.py      (a partir da raiz do repositório; precisa de painel/requirements.txt)
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for nome, valor in (("PAINEL_DSN_APP", "postgresql://x@localhost/x"), ("PAINEL_DSN_CONTEUDO", "postgresql://x@localhost/x"),
                    ("LITELLM_MASTER_KEY", "sk-teste")):
    os.environ.setdefault(nome, valor)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auditoria, consultas, litellm, modelos_chaves, rotinas, validacao  # noqa: E402
from app.web import templates  # noqa: E402

falhas = 0


def ok(cond, msg):
    global falhas
    print(("OK    " if cond else "FALHA ") + msg)
    falhas += 0 if cond else 1


AGORA = datetime.now(timezone.utc)


def chave(alias, modelos, expira_em=None, bloqueada=False):
    k = {"token": alias * 8, "key_alias": alias, "key_name": "sk-..x", "criado_para": alias, "modelos": modelos,
         "rpm_limit": 10, "max_parallel_requests": 2, "expira_em": expira_em, "bloqueada": bloqueada,
         "criada_em": AGORA, "ultimo_uso": None, "tok_30d": 0}
    k["status"] = consultas.status_chave(k, AGORA)
    k["modelos_reais"] = [m for m in modelos if m != validacao.SEM_ACESSO]
    k["sem_acesso"] = validacao.SEM_ACESSO in modelos
    return k


chaves = [
    chave("a", ["m1"]),                                   # só m1: retirar deixaria sem modelo
    chave("b", ["m1", "m2"]),
    chave("c", ["m2"]),
    chave("d", []),                                       # "todos": fora da seleção
    chave("e", ["m1"], expira_em=AGORA - timedelta(days=1)),  # expirada: fora
    chave("f", ["m2"], bloqueada=True),                   # bloqueada continua elegível
]
tok = {k["key_alias"]: k["token"] for k in chaves}

# --- planejar ---
p = modelos_chaves.planejar("m1", chaves, {tok["a"], tok["b"], tok["c"]})
ok([i["chave"]["key_alias"] for i in p.adicionar] == ["c"] and p.adicionar[0]["novos"] == ["m2", "m1"], "libera m1 em c, mantendo m2")
ok(not p.retirar and not p.bloqueadas, "quem já tinha e continua marcado não muda")
p = modelos_chaves.planejar("m1", chaves, set())
ok([i["chave"]["key_alias"] for i in p.retirar] == ["b"] and p.retirar[0]["novos"] == ["m2"], "retira m1 de b")
ok([i["chave"]["key_alias"] for i in p.bloqueadas] == ["a"], "a ficaria sem modelo: bloqueada")
ok(not any(i["chave"]["key_alias"] in ("d", "e") for i in p.alteracoes + p.bloqueadas), "chave 'todos' e expirada ficam de fora")
p = modelos_chaves.planejar("m1", chaves, set(), ultimo_vira_sem_acesso=True)
ok(any(i["chave"]["key_alias"] == "a" and i["novos"] == [validacao.SEM_ACESSO] for i in p.retirar), "despublicar total: última vira SEM_ACESSO")
p = modelos_chaves.planejar("m2", chaves, {tok["c"], tok["f"], "token-que-nao-existe"})
ok([i["chave"]["key_alias"] for i in p.retirar] == ["b"], "token desconhecido é ignorado; b perde m2")

# --- executar com falha parcial ---
chamadas, eventos = [], []
litellm.alterar_chave = lambda token, **kw: (_ for _ in ()).throw(litellm.ErroLiteLLM("recusou")) if token == tok["c"] \
    else chamadas.append((token, kw))
auditoria.registrar = lambda acao, usuario, ip, alvo=None, detalhes=None, conn=None: eventos.append((acao, usuario, alvo, detalhes))
consultas.chaves = lambda: chaves
plano, res = modelos_chaves.aplicar("m1", {tok["a"], tok["b"], tok["c"]}, "admin", None)
ok(res.alteradas == 0 and len(res.falhas) == 1 and not chamadas, "falha de uma chave é contada e não aborta")
plano, res = modelos_chaves.aplicar("m1", set(), "admin", None)
ok(res.alteradas == 1 and chamadas == [(tok["b"], {"modelos": ["m2"]})], "aplica só o que muda; a (última) é mantida")
ok(sum(1 for e in eventos if e[0] == "chave_alterada") == 1 and eventos[-1][0] == "modelo_chaves_ajustadas",
   "uma auditoria por chave + resumo")
ok("mantida" in modelos_chaves.resumo(plano, res), "resumo cita as chaves mantidas")

# --- limpeza de chaves expiradas ---
chamadas.clear(); eventos.clear()
n, f = rotinas.limpar_modelos_expirados()
ok(n == 1 and chamadas == [(tok["e"], {"modelos": [validacao.SEM_ACESSO]})], "expirada recebe SEM_ACESSO (nunca lista vazia)")
ok(eventos[0][0] == "chave_modelos_removidos" and eventos[0][3]["modelos"] == ["m1"], "auditoria guarda os modelos retirados")
chaves[4] = chave("e", [validacao.SEM_ACESSO], expira_em=AGORA - timedelta(days=1))
chamadas.clear()
ok(rotinas.limpar_modelos_expirados() == (0, []) and not chamadas, "idempotente: expirada sem modelos não é tocada")
ok(chaves[4]["sem_acesso"] and chaves[4]["modelos_reais"] == [], "SEM_ACESSO não conta como modelo")
try:
    validacao.modelos([], ["m1"])
    ok(False, "salvar chave sem modelos deve ser recusado")
except validacao.Invalido:
    ok(True, "reativar chave sem escolher modelo é recusado")
try:
    validacao.modelos([validacao.SEM_ACESSO], ["m1"])
    ok(False, "sentinela não pode ser escolhida")
except validacao.Invalido:
    ok(True, "sentinela não é aceita como modelo")

# --- telas ---
from types import SimpleNamespace  # noqa: E402

usuario = {"papel": "admin", "csrf": "x"}
request = SimpleNamespace(url=SimpleNamespace(path="/modelos"))
html = templates.get_template("modelo_chaves.html").render(
    request=request, flash=None, usuario=usuario, modelo="m1", chaves=modelos_chaves.elegiveis(chaves), todos=[chaves[3]], expiradas=1)
ok("data-tinha" in html and 'value="' + tok["a"] + '"' in html and "sem lista de modelos" in html, "tela de seleção renderiza")
plano = modelos_chaves.planejar("m1", chaves, set())
html = templates.get_template("modelo_chaves_revisao.html").render(request=request, flash=None, usuario=usuario, modelo="m1", plano=plano, marcadas=[])
ok("Perderão o modelo" in html and "não serão alteradas" in html, "tela de revisão renderiza")
html = templates.get_template("chaves.html").render(request=request, usuario=usuario, chaves=chaves, expiradas_com_modelos=1, flash=None)
ok("Limpar 1 chave" in html and "sem modelos" in html, "lista de chaves mostra a limpeza e 'sem modelos'")

sys.exit(1 if falhas else 0)
