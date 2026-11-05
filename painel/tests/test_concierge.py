"""Teste do concierge sem serviços externos (gateway, Ollama e banco simulados).

Cobre: filtro de ferramentas por papel, montagem das chamadas de ferramenta no stream, leitura executando na hora,
escrita só virando proposta, confirmação única (outro usuário, expirada, papel rebaixado, entrada recusada),
segredos fora do banco e do histórico do modelo, limites, validação do histórico e as rotas.
  python painel/tests/test_concierge.py      (a partir da raiz do repositório; precisa de painel/requirements.txt)
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for nome, valor in (("PAINEL_DSN_APP", "postgresql://x@localhost/x"), ("PAINEL_DSN_CONTEUDO", "postgresql://x@localhost/x"),
                    ("LITELLM_MASTER_KEY", "sk-teste")):
    os.environ.setdefault(nome, valor)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auditoria, consultas, db, litellm, validacao  # noqa: E402
from app.concierge import acoes, agente, ferramentas, limites  # noqa: E402
from app.rotas import concierge as rota  # noqa: E402

falhas = 0


def ok(cond, msg):
    global falhas
    print(("OK    " if cond else "FALHA ") + msg)
    falhas += 0 if cond else 1


ADMIN = {"id": 1, "login": "ana", "nome": "Ana", "papel": "admin", "csrf": "c"}
LEITOR = {"id": 2, "login": "leo", "nome": "Leo", "papel": "leitor", "csrf": "c"}
AGORA = datetime.now(timezone.utc)

# --- Banco e auditoria simulados -----------------------------------------------------------------------
tabela: dict[str, dict] = {}
trilha: list[tuple] = []
auditoria.registrar = lambda acao, usuario, ip, alvo=None, detalhes=None, conn=None: trilha.append((acao, alvo, detalhes))


def fake_um(sql, params=None):
    if "select count(*) as n from painel.concierge_acoes" in sql:
        return {"n": sum(1 for a in tabela.values() if a["usuario_id"] == params[0] and a["estado"] == "pendente")}
    if "insert into painel.concierge_acoes" in sql:
        usuario_id, ferramenta, args, resumo, _min = params
        id_ = f"00000000-0000-0000-0000-{len(tabela):012d}"
        tabela[id_] = {"id": id_, "usuario_id": usuario_id, "ferramenta": ferramenta, "args": args.obj, "resumo": resumo,
                       "estado": "pendente", "expirada": False}
        return {"id": id_, "expira_em": AGORA + timedelta(minutes=10)}
    if "update painel.concierge_acoes set estado = %s, resolvida_em = now()" in sql:
        novo, id_, usuario_id = params
        a = tabela.get(id_)
        if a is None or a["usuario_id"] != usuario_id or a["estado"] != "pendente" or a["expirada"]:
            return None
        a["estado"] = novo
        return {"id": id_, "ferramenta": a["ferramenta"], "args": a["args"], "resumo": a["resumo"]}
    if "select ferramenta from painel.concierge_acoes" in sql:
        a = tabela.get(params[0])
        return {"ferramenta": a["ferramenta"]} if a and a["usuario_id"] == params[1] else None
    if "from painel.usuarios where login" in sql:
        return {"id": 9, "login": params[0], "nome": "X", "papel": "leitor", "ativo": True}
    return None


def fake_exec(sql, params=None):
    if "set estado = 'pendente'" in sql:
        tabela[params[0]]["estado"] = "pendente"
    elif "set estado = %s, resultado" in sql:
        tabela[params[2]]["estado"] = params[0]
        tabela[params[2]]["resultado"] = params[1].obj


db.consultar_um, db.executar = fake_um, fake_exec
db.consultar = lambda sql, params=None: []


def chave(alias, modelos=("m1",), status="ativa"):
    return {"token": alias * 8, "key_alias": alias, "key_name": "sk-..", "criado_para": alias, "modelos": list(modelos),
            "modelos_reais": list(modelos), "sem_acesso": False, "rpm_limit": 10, "max_parallel_requests": 2,
            "expira_em": None, "bloqueada": status == "bloqueada", "criada_em": AGORA, "ultimo_uso": None,
            "status": status, "tok_30d": 0}


consultas.chaves = lambda: [chave("time-a"), chave("time-b", ("m1", "m2"), "bloqueada")]
litellm.modelos_publicados = lambda: ["m1", "m2"]

# serviços simulados: registram a chamada
chamadas: list[tuple] = []
ferramentas.s_chaves.criar = lambda ator, ip, alias, cp, modelos, rpm, par, val, origem=None: (
    chamadas.append(("criar", alias, origem)) or ("sk-SEGREDO-123", "t" * 64, alias))
def _revogar(ator, ip, token, motivo, confirmacao):
    chamadas.append(("revogar", token, motivo, confirmacao))
    if confirmacao != "time-a":
        raise validacao.Invalido("Para revogar, digite o nome da chave exatamente como aparece.")
    return {}


ferramentas.s_chaves.revogar = _revogar


# --- Ferramentas por papel -----------------------------------------------------------------------------
adm, lei = ferramentas.disponiveis(ADMIN), ferramentas.disponiveis(LEITOR)
ok(len(lei) < len(adm) and all(not f.admin for f in lei), "leitor só recebe ferramentas de leitura")
ok(all(f.admin for f in adm if f.mutante), "toda ferramenta de escrita é de admin")
ok(not any(f.mutante for f in lei), "leitor não recebe nenhuma ferramenta de escrita")
ok(all(e["function"]["parameters"]["type"] == "object" for e in ferramentas.esquemas(ADMIN)), "esquemas válidos")

# --- Acumulador ----------------------------------------------------------------------------------------
ac = agente.Acumulador()
ac.chunk({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "listar_cha", "arguments": ""}}]}}]})
ac.chunk({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "ves", "arguments": '{"busca":'}}]}}]})
ac.chunk({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]}}]})
ac.chunk({"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "x", "arguments": {"a": 1}}}]}}]})
ok(ac.lista()[0] == {"id": "c1", "nome": "listar_chaves", "args": '{"busca":"a"}'}, "junta nome e argumentos da chamada em pedaços")
ok(ac.lista()[1]["args"] == '{"a": 1}', "argumentos já em objeto viram JSON")


# --- Laço do agente ------------------------------------------------------------------------------------
def sse(*chunks):
    return [("linha", "data: " + json.dumps(c)) for c in chunks] + [("linha", "data: [DONE]")]


def chamada(nome, args, id_="c1"):
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": id_, "function": {"name": nome, "arguments": json.dumps(args)}}]}}]}


def texto(t):
    return {"choices": [{"delta": {"content": t}, "finish_reason": "stop"}]}


def roteiro(*respostas):
    """Stream falso: cada chamada ao modelo consome uma resposta do roteiro e guarda o corpo recebido."""
    corpos, fila = [], list(respostas)

    async def abrir(corpo):
        corpos.append(json.loads(json.dumps(corpo)))
        for item in (fila.pop(0) if fila else sse(texto("fim"))):
            yield item
    return abrir, corpos


async def rodar(ator, abrir, historico=None, **kw):
    tabela.clear()
    trilha.clear()
    chamadas.clear()
    return [json.loads(e[len("data: "):]) for e in
            [e async for e in agente.conversar(ator, "1.1.1.1", "mod", historico or [{"role": "user", "content": "oi"}],
                                               abrir_stream=abrir, **kw)]]


def tipos(ev):
    return [e["tipo"] for e in ev]


abrir, corpos = roteiro(sse(chamada("listar_chaves", {})), sse(texto("Há "), texto("2 chaves.")))
ev = asyncio.run(rodar(LEITOR, abrir))
ok(tipos(ev) == ["status", "ferramenta", "ferramenta", "delta", "delta", "metricas", "fim"], f"leitura: ferramenta e resposta ({tipos(ev)})")
msg_tool = [m for m in corpos[1]["messages"] if m["role"] == "tool"][0]
ok("time-a" in msg_tool["content"] and "sk-" not in msg_tool["content"] and "t" * 64 not in msg_tool["content"],
   "resultado de leitura chega ao modelo sem hash nem segredo")
ok(len(corpos[0]["tools"]) == len(ferramentas.esquemas(LEITOR)), "o modelo recebe só as ferramentas do papel")
ok("Leo" in corpos[0]["messages"][0]["content"] and "leitor" in corpos[0]["messages"][0]["content"], "prompt de sistema traz o usuário e o papel")

# leitor forjando uma ferramenta de admin
abrir, corpos = roteiro(sse(chamada("criar_chave", {"alias": "x", "modelos": ["m1"]})), sse(texto("não posso")))
ev = asyncio.run(rodar(LEITOR, abrir))
ok(not tabela and not chamadas, "chamada forjada de ferramenta de admin por leitor não gera proposta nem execução")
ok(any(e["tipo"] == "ferramenta" and e["estado"] == "erro" for e in ev), "e aparece como erro na tela")

# admin: escrita vira proposta, sem executar
abrir, corpos = roteiro(sse(chamada("criar_chave", {"alias": "novo", "modelos": ["m1"]})), sse(texto("Aguardo sua confirmação.")))
ev = asyncio.run(rodar(ADMIN, abrir))
prop = next(e for e in ev if e["tipo"] == "proposta")
ok(not chamadas, "escrita não executa o serviço ao ser proposta")
ok(len(tabela) == 1 and prop["acao_id"] in tabela and "novo" in prop["resumo"], "proposta gravada com resumo")
ok("NÃO foi executada" in [m for m in corpos[1]["messages"] if m["role"] == "tool"][0]["content"], "modelo é avisado de que não executou")
ok(any(a[0] == "concierge_proposta" for a in trilha), "proposta auditada")

# proposta repetida na mesma conversa
abrir, _ = roteiro(sse(chamada("criar_chave", {"alias": "novo", "modelos": ["m1"]}, "a")),
                   sse(chamada("criar_chave", {"alias": "novo", "modelos": ["m1"]}, "b")), sse(texto("ok")))
asyncio.run(rodar(ADMIN, abrir))
ok(len(tabela) == 1, "proposta idêntica repetida pelo modelo não duplica")

# argumentos inválidos / alias já existente: erro devolvido ao modelo, nada gravado
abrir, corpos = roteiro(sse(chamada("criar_chave", {"alias": "time-a", "modelos": ["m1"]})), sse(texto("existe")))
asyncio.run(rodar(ADMIN, abrir))
ok(not tabela and "Já existe" in [m for m in corpos[1]["messages"] if m["role"] == "tool"][0]["content"],
   "alias duplicado é recusado na proposta")
abrir, corpos = roteiro([("linha", "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
    {"index": 0, "id": "c", "function": {"name": "listar_chaves", "arguments": "{nao json"}}]}}]}))], sse(texto("x")))
ev = asyncio.run(rodar(LEITOR, abrir))
ok(any(e["tipo"] == "ferramenta" and e["estado"] == "erro" for e in ev) and tipos(ev)[-1] == "fim", "argumentos que não são JSON não derrubam o laço")

# gateway com erro / sem resposta / passos demais
async def com_erro(corpo):
    yield "http", (500, '{"error": {"message": "boom"}}')


ev = asyncio.run(rodar(LEITOR, com_erro))
ok(tipos(ev) == ["status", "erro", "fim"] and "boom" in ev[1]["mensagem"], "erro do gateway vira evento de erro")
abrir, _ = roteiro(sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
ev = asyncio.run(rodar(LEITOR, abrir))
ok(any(e["tipo"] == "erro" for e in ev), "resposta vazia vira erro explícito")
abrir, _ = roteiro(*[sse(chamada("listar_chaves", {}, f"c{i}")) for i in range(10)])
ev = asyncio.run(rodar(LEITOR, abrir, max_passos=3))
ok(sum(1 for e in ev if e["tipo"] == "ferramenta" and e["estado"] == "ok") == 3 and "passos demais" in
   [e for e in ev if e["tipo"] == "erro"][0]["mensagem"], "teto de passos do laço")

# link de navegação
abrir, _ = roteiro(sse(chamada("navegar", {"destino": "logs"})), sse(texto("aqui")))
ev = asyncio.run(rodar(LEITOR, abrir))
ok(any(e["tipo"] == "link" and e["href"] == "/logs" for e in ev), "navegar emite link interno")
abrir, corpos = roteiro(sse(chamada("navegar", {"destino": "usuarios"})), sse(texto("x")))
ev = asyncio.run(rodar(LEITOR, abrir))
ok(not any(e["tipo"] == "link" for e in ev), "leitor não recebe link de tela de admin")


# --- Confirmação ---------------------------------------------------------------------------------------
def propor_criar():
    tabela.clear(), trilha.clear(), chamadas.clear()
    return acoes.propor(ADMIN, "ip", ferramentas.obter("criar_chave"), {"alias": "novo", "modelos": ["m1"]})


p = propor_criar()
r = acoes.confirmar(ADMIN, "ip", p["acao_id"], "", "")
ok(r["ok"] and chamadas == [("criar", "novo", "concierge")], "confirmar executa o serviço com origem 'concierge'")
ok(r["segredos"][0]["valor"] == "sk-SEGREDO-123", "o segredo volta ao navegador")
ok("SEGREDO" not in json.dumps(tabela[p["acao_id"]]) and "SEGREDO" not in json.dumps(trilha), "segredo não vai ao banco nem à auditoria")
ok(any(a[0] == "concierge_confirmada" for a in trilha), "confirmação auditada")
try:
    acoes.confirmar(ADMIN, "ip", p["acao_id"], "", "")
    ok(False, "segunda confirmação deveria falhar")
except acoes.AcaoInvalida:
    ok(len(chamadas) == 1, "a mesma proposta não executa duas vezes")

p = propor_criar()
try:
    acoes.confirmar({**ADMIN, "id": 5, "login": "outro"}, "ip", p["acao_id"], "", "")
    ok(False, "outro usuário não pode confirmar")
except acoes.AcaoInvalida:
    ok(not chamadas, "proposta de outro usuário não pode ser confirmada")
try:
    acoes.confirmar({**ADMIN, "papel": "leitor"}, "ip", p["acao_id"], "", "")
    ok(False, "papel rebaixado")
except acoes.AcaoInvalida:
    ok(not chamadas, "papel rebaixado entre a proposta e a confirmação: recusado")
tabela[p["acao_id"]]["expirada"] = True
try:
    acoes.confirmar(ADMIN, "ip", p["acao_id"], "", "")
    ok(False, "expirada")
except acoes.AcaoInvalida:
    ok(not chamadas, "proposta expirada não executa")
try:
    acoes.confirmar(ADMIN, "ip", "não-é-uuid", "", "")
except acoes.AcaoInvalida:
    ok(True, "id inexistente é recusado")

p = propor_criar()
acoes.cancelar(ADMIN, "ip", p["acao_id"])
try:
    acoes.confirmar(ADMIN, "ip", p["acao_id"], "", "")
    ok(False, "cancelada")
except acoes.AcaoInvalida:
    ok(tabela[p["acao_id"]]["estado"] == "cancelada" and not chamadas, "cancelada não executa")

# revogar: nome digitado errado mantém a proposta; certo executa
tabela.clear(), chamadas.clear()
p = acoes.propor(ADMIN, "ip", ferramentas.obter("revogar_chave"), {"chave": "time-a", "motivo": "vazou"})
ok(p["campos"] == ["motivo", "confirmacao"] and p["confirmar_texto"] == "time-a" and p["motivo_sugerido"] == "vazou",
   "revogar pede motivo e a digitação do nome (o motivo do modelo é só sugestão)")
r = acoes.confirmar(ADMIN, "ip", p["acao_id"], "vazou a chave", "errado")
ok(not r["ok"] and r.get("repetir") and tabela[p["acao_id"]]["estado"] == "pendente", "nome errado: recusa e a proposta continua pendente")
r = acoes.confirmar(ADMIN, "ip", p["acao_id"], "vazou a chave", "time-a")
ok(r["ok"], "nome certo: executa")
ok(chamadas[-1] == ("revogar", "time-a" * 8, "vazou a chave", "time-a"), "confirmação digitada vai ao serviço com o motivo do humano")

# limite de propostas pendentes
tabela.clear()
try:
    for _ in range(acoes.MAX_PENDENTES + 1):
        acoes.propor(ADMIN, "ip", ferramentas.obter("criar_chave"), {"alias": "novo", "modelos": ["m1"]})
    ok(False, "limite de pendentes")
except validacao.Invalido:
    ok(len(tabela) == acoes.MAX_PENDENTES, "no máximo 8 propostas pendentes por usuário")

# --- Limites -------------------------------------------------------------------------------------------
limites.reiniciar()
limites.iniciar(1, 2, agora=0.0)
try:
    limites.iniciar(1, 2, agora=1.0)
    ok(False, "uma por vez")
except limites.LimiteExcedido:
    ok(True, "uma conversa por vez por usuário")
limites.liberar(1)
limites.iniciar(1, 2, agora=2.0)
limites.liberar(1)
try:
    limites.iniciar(1, 2, agora=3.0)
    ok(False, "limite por minuto")
except limites.LimiteExcedido:
    ok(True, "limite de mensagens por minuto")
limites.iniciar(1, 2, agora=70.0)
ok(True, "a janela de 60 s libera de novo")
limites.liberar(1)
limites.reiniciar()
limites.iniciar(1, 9, agora=0.0)
limites.iniciar(2, 9, agora=0.0)
try:
    limites.iniciar(3, 9, agora=0.0)
    ok(False, "teto global")
except limites.LimiteExcedido:
    ok(True, "teto global de conversas simultâneas")
limites.reiniciar()

# --- Validação do histórico ----------------------------------------------------------------------------
for ruim, nome in (([], "vazio"), ([{"role": "system", "content": "x"}], "papel system"),
                   ([{"role": "assistant", "content": "x"}], "terminando no assistente"),
                   ([{"role": "user", "content": " "}], "mensagem vazia"), ("x", "não é lista"),
                   ([{"role": "user", "content": "x" * 9000}], "longa demais"),
                   ([{"role": "user", "content": "x"}] * 21, "conversa longa")):
    try:
        rota.validar_historico(ruim)
        ok(False, f"histórico {nome} deveria ser recusado")
    except validacao.Invalido:
        ok(True, f"histórico recusado: {nome}")
ok(len(rota.validar_historico([{"role": "user", "content": "oi"}])) == 1, "histórico válido")

# --- Rotas ---------------------------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from app import autenticacao as auth  # noqa: E402
from app.concierge import configuracao  # noqa: E402
from app.main import app  # noqa: E402

usuario_atual = {"u": ADMIN}


async def _usuario():
    return usuario_atual["u"]


app.dependency_overrides[auth.usuario_logado] = _usuario
cliente = TestClient(app)
cfg = {"modelo": None, "ativo": True, "limite_msg_min": 20}
configuracao.obter = lambda: dict(cfg)
r = cliente.post("/concierge/enviar", data={"mensagens": "[]"})
ok(r.status_code == 409, "concierge sem modelo configurado: 409")
cfg["modelo"] = "mod"
r = cliente.post("/concierge/enviar", data={"mensagens": json.dumps([{"role": "system", "content": "x"}])})
ok(r.status_code == 400, "histórico inválido: 400")
rota._modelo_pronto = lambda c: (None, False)
abrir, corpos = roteiro(sse(chamada("listar_chaves", {})), sse(texto("Duas chaves.")))
agente.stream_gateway = abrir
limites.reiniciar()
r = cliente.post("/concierge/enviar", data={"mensagens": json.dumps([{"role": "user", "content": "quais chaves?"}])})
ev = [json.loads(l[6:]) for l in r.text.split("\n\n") if l.startswith("data:")]
ok(r.status_code == 200 and tipos(ev)[-1] == "fim" and any(e["tipo"] == "delta" for e in ev), "rota /concierge/enviar transmite os eventos")
ok(not limites._em_andamento, "a vaga do usuário é devolvida ao fim")
cfg["limite_msg_min"] = 1
limites.reiniciar()
cliente.post("/concierge/enviar", data={"mensagens": json.dumps([{"role": "user", "content": "a"}])})
r = cliente.post("/concierge/enviar", data={"mensagens": json.dumps([{"role": "user", "content": "b"}])})
ok("mensagens por minuto" in r.text, "limite por minuto aparece como erro na tela")
limites.reiniciar()
cfg["ativo"] = False
ok(cliente.post("/concierge/enviar", data={"mensagens": "[]"}).status_code == 409, "concierge desativado: 409")

tabela.clear()
r = cliente.post("/concierge/confirmar/00000000-0000-0000-0000-00000000ffff", data={})
ok(r.status_code == 410 and r.json()["ok"] is False, "confirmar proposta inexistente: 410")

usuario_atual["u"] = LEITOR
r = cliente.get("/concierge/configuracao")
ok(r.status_code == 403, "leitor não acessa a configuração")

print(f"\n{falhas} falha(s)")
sys.exit(1 if falhas else 0)
