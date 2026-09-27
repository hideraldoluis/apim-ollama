"""Teste ponta a ponta do painel contra Postgres + LiteLLM reais.

Uso (com a stack no ar e um banco SEM dados de produção; o teste cria chaves e usuários):
  python painel/tests/e2e.py <senha-temporária-do-admin.teste> <diretório-do-compose>

Pré-requisitos: admin.teste criado pela CLI (criar-admin admin.teste "Admin Teste"), nenhum outro
usuário, PAINEL_URL/LITELLM_URL apontando para o painel e o gateway (padrões abaixo) e a master key
em MASTER_KEY (o teste publica o modelo fictício "modelo-teste" direto no gateway; não precisa do Ollama).
Rode o painel com PAINEL_COOKIE_SEGURO=0 quando testar por HTTP: o httpx não envia cookies Secure.
Cada execução soma falhas de login propositais ao limite por IP (20 em 15 min): reinicie o painel
entre execuções seguidas.
"""
import os
import re
import subprocess
import sys
import uuid

import httpx

BASE = os.environ.get("PAINEL_URL", "http://127.0.0.1:8080")
LITELLM = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
MASTER = os.environ.get("MASTER_KEY", "")
MODELO = "modelo-teste"
SENHA_ADMIN = "frase longa do gestor 42"
SENHA_LEITOR = "leitura tranquila de consumo 7"
falhas = 0


def ok(cond, msg):
    global falhas
    print(("OK    " if cond else "FALHA ") + msg)
    if not cond:
        falhas += 1


def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def login(c, usuario, senha):
    r = c.get("/login")
    return c.post("/login", data={"login": usuario, "senha": senha, "csrf": csrf(r.text)})


def psql(sql):
    return subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "litellm", "-d", "litellm",
                           "-tAc", sql], capture_output=True, text=True, cwd=sys.argv[2]).stdout.strip()


temp_admin = sys.argv[1]

# Instalação nova não tem modelos: publica um fictício para as chaves do teste.
_mh = {"Authorization": f"Bearer {MASTER}"}
if MODELO not in [m["id"] for m in httpx.get(f"{LITELLM}/v1/models", headers=_mh).json()["data"]]:
    httpx.post(f"{LITELLM}/model/new", headers=_mh, json={
        "model_name": MODELO, "litellm_params": {"model": f"ollama_chat/{MODELO}", "api_base": "os.environ/OLLAMA_API_BASE"}},
    ).raise_for_status()
admin = httpx.Client(base_url=BASE, follow_redirects=False)

# --- Login e troca obrigatória ---
r = admin.get("/")
ok(r.status_code == 303 and r.headers["location"] == "/login", "sem sessão redireciona para /login")
r = admin.post("/login", data={"login": "admin.teste", "senha": temp_admin, "csrf": "forjado"})
ok(r.status_code == 400, "login com CSRF inválido é recusado")
r = login(admin, "admin.teste", "errada")
ok(r.status_code == 401 and "inválidos" in r.text, "senha errada: mensagem genérica")
r = login(admin, "admin.teste", temp_admin)
ok(r.status_code == 303 and r.headers["location"] == "/trocar-senha", "senha temporária força troca")
r = admin.get("/chaves")
ok(r.status_code == 303 and r.headers["location"] == "/trocar-senha", "sem trocar senha não navega")
pag = admin.get("/trocar-senha").text
r = admin.post("/trocar-senha", data={"csrf": csrf(pag), "atual": temp_admin, "nova": "curta", "confirmacao": "curta"})
ok(r.status_code == 400 and "12 caracteres" in r.text, "política: tamanho mínimo")
r = admin.post("/trocar-senha", data={"csrf": csrf(pag), "atual": temp_admin, "nova": "admin.teste-senha-x", "confirmacao": "admin.teste-senha-x"})
ok(r.status_code == 400 and "login" in r.text, "política: não pode conter o login")
r = admin.post("/trocar-senha", data={"csrf": csrf(pag), "atual": temp_admin, "nova": SENHA_ADMIN, "confirmacao": SENHA_ADMIN})
ok(r.status_code == 303, "troca de senha aceita")
r = admin.get("/")
ok(r.status_code == 200 and "Visão geral" in r.text, "visão geral abre")
ok(r.headers.get("cache-control") == "no-store" and "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""),
   "cabeçalhos de segurança presentes")

# --- POST sem CSRF / de outra origem ---
r = admin.post("/usuarios/novo", data={"login": "x.y", "nome": "X Y", "papel": "admin"})
ok(r.status_code == 403, "POST autenticado sem CSRF é recusado")
r = admin.post("/logout", data={"csrf": "x"}, headers={"origin": "http://malicioso.example"})
ok(r.status_code == 403, "POST de outra origem é recusado")

# --- Chaves ---
pag = admin.get("/chaves/nova").text
ok(MODELO in pag, "formulário de chave lista modelos publicados no gateway")
tok_csrf = csrf(pag)
r = admin.post("/chaves/nova", data={"csrf": tok_csrf, "alias": "time-dados", "criado_para": "Time de Dados",
                                     "modelos": [MODELO], "rpm": "30", "paralelas": "2", "validade": "30d"})
m = re.search(r'id="segredo">(sk-[^<]+)<', r.text)
ok(r.status_code == 200 and m is not None, "chave criada e segredo exibido uma vez")
segredo = m.group(1)
token = re.search(r'href="/chaves/([0-9a-f]{64})"', r.text).group(1)
r = admin.post("/chaves/nova", data={"csrf": tok_csrf, "alias": "time-dados", "modelos": [MODELO],
                                     "rpm": "30", "paralelas": "2", "validade": "30d"})
ok(r.status_code == 400, "alias duplicado é recusado pelo gateway")
r = admin.post("/chaves/nova", data={"csrf": tok_csrf, "alias": "x", "modelos": ["inexistente"],
                                     "rpm": "30", "paralelas": "2", "validade": "30d"})
ok(r.status_code == 400 and "não publicados" in r.text, "modelo fora do gateway é recusado")
lista = admin.get("/chaves").text
ok("time-dados" in lista and segredo not in lista, "lista mostra a chave sem o segredo")
r = admin.post(f"/chaves/{token}/alterar", data={"csrf": tok_csrf, "modelos": [MODELO], "rpm": "60", "paralelas": "3", "validade": "never"})
ok(r.status_code == 303, "alteração de limites")
det = admin.get(f"/chaves/{token}").text
ok(">60<" in det and "nunca" in det, "detalhe reflete RPM 60 e sem expiração")
r = admin.post(f"/chaves/{token}/bloquear", data={"csrf": tok_csrf, "motivo": "teste de bloqueio"})
ok(r.status_code == 303 and "bloqueada" in admin.get("/chaves").text, "bloqueio")
r = httpx.post(f"{LITELLM}/v1/chat/completions", headers={"Authorization": f"Bearer {segredo}"},
               json={"model": MODELO, "messages": [{"role": "user", "content": "oi"}]})
ok(r.status_code in (401, 403), f"gateway recusa chave bloqueada ({r.status_code})")
admin.post(f"/chaves/{token}/desbloquear", data={"csrf": tok_csrf})
ok("bloqueada" not in admin.get("/chaves").text.split("<tbody>")[1], "desbloqueio")

# --- Logs sintéticos (com texto) ---
rid = str(uuid.uuid4())
psql(f"""insert into "LiteLLM_SpendLogs" (request_id, call_type, api_key, spend, total_tokens, prompt_tokens,
  completion_tokens, "startTime", "endTime", model, metadata, status, proxy_server_request, response, requester_ip_address)
  values ('{rid}', 'acompletion', '{token}', 0, 30, 10, 20, now() at time zone 'utc' - interval '1 minute',
  now() at time zone 'utc', 'llama3.2', '{{"user_api_key_alias": "time-dados"}}', 'success',
  '{{"messages": [{{"role": "user", "content": "SEGREDO-DO-PROMPT"}}]}}',
  '{{"choices": [{{"message": {{"content": "TEXTO-DA-RESPOSTA"}}}}]}}', '10.0.0.9')""")
logs = admin.get("/logs").text
ok(rid in logs and "SEGREDO-DO-PROMPT" not in logs, "lista de logs sem texto (admin)")
det = admin.get(f"/logs/{rid}").text
ok("SEGREDO-DO-PROMPT" not in det and "Ver conteúdo" in det, "detalhe sem texto até pedir")
r = admin.post(f"/logs/{rid}/conteudo", data={"csrf": tok_csrf})
ok("SEGREDO-DO-PROMPT" in r.text and "TEXTO-DA-RESPOSTA" in r.text, "admin vê o conteúdo via POST")
aud = admin.get("/auditoria?acao=log_conteudo_visto").text
ok(rid in aud, "leitura de conteúdo registrada na auditoria")
cons = admin.get("/consumo").text
ok("time-dados" in cons and ">30<" in cons, "consumo por chave soma tokens")

# --- Usuário leitor ---
r = admin.post("/usuarios/novo", data={"csrf": tok_csrf, "login": "leitor.um", "nome": "Leitor Um", "papel": "leitor"})
temp_leitor = re.search(r'id="temporaria">([^<]+)<', r.text).group(1)
leitor_id = re.search(r'href="/usuarios/(\d+)"', r.text).group(1)
leitor = httpx.Client(base_url=BASE, follow_redirects=False)
login(leitor, "leitor.um", temp_leitor)
pag = leitor.get("/trocar-senha").text
leitor.post("/trocar-senha", data={"csrf": csrf(pag), "atual": temp_leitor, "nova": SENHA_LEITOR, "confirmacao": SENHA_LEITOR})
lcsrf = csrf(leitor.get("/").text)
ok(leitor.get("/consumo").status_code == 200, "leitor vê consumo")
ok(leitor.get("/chaves").status_code == 200, "leitor vê chaves")
ok("Nova chave" not in leitor.get("/chaves").text, "leitor não vê botão de nova chave")
det = leitor.get(f"/logs/{rid}").text
ok("SEGREDO-DO-PROMPT" not in det and "apenas para administradores" in det, "leitor vê detalhe sem texto")
r = leitor.post(f"/logs/{rid}/conteudo", data={"csrf": lcsrf})
ok(r.status_code == 403 and "SEGREDO-DO-PROMPT" not in r.text, "leitor NÃO obtém conteúdo pela rota de admin")
r = leitor.post("/logs/exportar-conteudo", data={"csrf": lcsrf, "justificativa": "tentativa"})
ok(r.status_code == 403, "leitor NÃO exporta conteúdo")
csvl = leitor.get("/logs.csv")
ok(csvl.status_code == 200 and rid in csvl.text and "SEGREDO-DO-PROMPT" not in csvl.text, "CSV do leitor sem texto")
for rota in ("/usuarios", "/auditoria", "/retencao", "/chaves/nova"):
    ok(leitor.get(rota).status_code == 403, f"leitor bloqueado em {rota}")
r = leitor.post(f"/chaves/{token}/revogar", data={"csrf": lcsrf, "motivo": "tentativa", "confirmacao": "time-dados"})
ok(r.status_code == 403, "leitor não revoga chave")

# --- Regras de governança ---
admin_id = re.search(r'href="/usuarios/(\d+)">admin.teste', admin.get("/usuarios").text).group(1)
r = admin.post(f"/usuarios/{admin_id}/papel", data={"csrf": tok_csrf, "papel": "leitor"})
ok("próprio usuário" in admin.get(r.headers["location"]).text, "admin não altera o próprio papel")
r = admin.post(f"/usuarios/{leitor_id}/papel", data={"csrf": tok_csrf, "papel": "admin"})
ok(r.status_code == 303, "promoção de leitor a admin")
ok(leitor.get("/").status_code == 303, "mudança de papel encerra sessões do usuário")
login(leitor, "leitor.um", SENHA_LEITOR)
lcsrf = csrf(leitor.get("/").text)
r = leitor.post(f"/usuarios/{admin_id}/desativar", data={"csrf": lcsrf, "motivo": "teste de desativação"})
ok(r.status_code == 303, "segundo admin desativa o primeiro")
r = leitor.post(f"/usuarios/{admin_id}/reativar", data={"csrf": lcsrf})
temp2 = re.search(r'id="temporaria">([^<]+)<', r.text).group(1)
ok(bool(temp2), "reativação gera nova senha temporária")
r = leitor.post(f"/usuarios/{leitor_id}/desativar", data={"csrf": lcsrf, "motivo": "auto desativação"})
ok("próprio usuário" in leitor.get(r.headers["location"]).text, "ninguém desativa a si mesmo")
# Último admin: com admin.teste rebaixado, leitor.um é o único admin e não pode ser rebaixado por ninguém.
leitor.post(f"/usuarios/{admin_id}/papel", data={"csrf": lcsrf, "papel": "leitor"})
ok(psql("select count(*) from painel.usuarios where papel='admin' and ativo") == "1", "restou um admin ativo")

# --- Bloqueio por tentativas ---
x = httpx.Client(base_url=BASE, follow_redirects=False)
for _ in range(5):
    login(x, "admin.teste", "errada-errada")
r = login(x, "admin.teste", temp2)
ok(r.status_code == 401, "conta bloqueada após 5 falhas (mesmo com a senha certa)")
ok("login_bloqueio" in psql("select string_agg(acao, ',') from painel.auditoria where alvo='admin.teste'"), "bloqueio auditado")
leitor.post(f"/usuarios/{admin_id}/desbloquear", data={"csrf": lcsrf})
r = login(x, "admin.teste", temp2)
ok(r.status_code == 303, "desbloqueio pelo admin libera o login")

# --- Rotação e revogação ---
r = leitor.post(f"/chaves/{token}/rotacionar", data={"csrf": lcsrf, "carencia": "0", "validade": "30d"})
novo = re.search(r'id="segredo">(sk-[^<]+)<', r.text)
ok(novo is not None and "Já foi revogada" in r.text, "rotação com carência 0 revoga a antiga")
ok(psql(f"""select count(*) from "LiteLLM_VerificationToken" where token='{token}'""") == "0", "chave antiga removida do gateway")
ok(psql(f"select count(*) from painel.chaves_historico where token='{token}'") == "1", "retrato da chave antiga no histórico")
ok(admin.get(f"/chaves/{token}").status_code in (200, 303), "detalhe de chave revogada não quebra")
novo_token = re.search(r'href="/chaves/([0-9a-f]{64})"', r.text).group(1)
r = leitor.post(f"/chaves/{novo_token}/revogar", data={"csrf": lcsrf, "motivo": "fim do teste", "confirmacao": "errado"})
ok("digite o nome" in leitor.get(r.headers["location"]).text, "revogação exige confirmar o nome")
r = leitor.post(f"/chaves/{novo_token}/revogar", data={"csrf": lcsrf, "motivo": "fim do teste", "confirmacao": "time-dados"})
ok(r.status_code == 303 and "time-dados" in leitor.get("/chaves/revogadas").text, "revogação registrada no histórico")
cons = leitor.get("/consumo").text
ok("revogada" in cons, "consumo preserva chave revogada")
ok(leitor.get("/retencao").status_code == 200, "tela de retenção abre")

print(f"\n{falhas} falha(s)")
sys.exit(1 if falhas else 0)
