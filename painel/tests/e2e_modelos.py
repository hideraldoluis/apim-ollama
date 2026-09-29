"""Teste do módulo de modelos (Ollama + LiteLLM reais, banco SEM dados de produção).

Baixa dois modelos pequenos (smollm:135m e all-minilm), publica, usa pelo gateway, retira e remove.
Pré-requisitos: os mesmos de e2e.py (admin.teste recém-criado, PAINEL_COOKIE_SEGURO=0), e o gateway
LITELLM_MASTER_KEY em MASTER_KEY.
  python painel/tests/e2e_modelos.py <senha-temporária-do-admin.teste>
"""
import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("PAINEL_URL", "http://127.0.0.1:8080")
LITELLM = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
MASTER = os.environ.get("MASTER_KEY", "")
CHAT, EMB = "smollm:135m", "all-minilm"
falhas = 0


def ok(cond, msg):
    global falhas
    print(("OK    " if cond else "FALHA ") + msg)
    falhas += 0 if cond else 1


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def flash(c, r):
    pag = c.get(r.headers["location"]).text
    m = re.search(r'<p class="aviso (\w+)" role="status">([^<]+)', pag)
    return (m.group(1), m.group(2)) if m else ("", "")


c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=120)
r = c.get("/login")
c.post("/login", data={"login": "admin.teste", "senha": sys.argv[1], "csrf": csrf(r.text)})
t = csrf(c.get("/trocar-senha").text)
c.post("/trocar-senha", data={"csrf": t, "atual": sys.argv[1], "nova": "frase longa do gestor 42", "confirmacao": "frase longa do gestor 42"})
pag = c.get("/modelos")
ok(pag.status_code == 200 and "Versão do Ollama" in pag.text, "página de modelos abre")
ok("Nenhum modelo publicado" in pag.text, "instalação nova começa sem modelos publicados")
ok(httpx.get(f"{LITELLM}/v1/models", headers={"Authorization": f"Bearer {MASTER}"}).json()["data"] == [],
   "gateway começa sem modelos")
ok("Nenhum modelo publicado" in c.get("/chaves/nova").text, "nova chave avisa que não há modelos")
t = csrf(pag.text)

# Downloads
r = c.post("/modelos/baixar", data={"csrf": t, "modelo": "../etc"})
ok(flash(c, r)[0] == "erro", "nome inválido recusado")
for nome in (CHAT, EMB):
    r = c.post("/modelos/baixar", data={"csrf": t, "modelo": nome})
    ok(flash(c, r)[0] == "ok", f"download de {nome} iniciado")
r = c.post("/modelos/baixar", data={"csrf": t, "modelo": CHAT})
ok("já está em andamento" in flash(c, r)[1], "download duplicado do mesmo modelo recusado")
r = c.post("/modelos/baixar", data={"csrf": t, "modelo": "modelo-que-nao-existe-xyz"})
viu_progresso = False
for _ in range(180):
    dados = c.get("/modelos/downloads.json").json()
    viu_progresso |= any(d["status"] == "baixando" and d["percentual"] is not None for d in dados)
    if not any(d["status"] == "baixando" for d in dados):
        break
    time.sleep(1)
estados = {d["id"]: d["status"] for d in dados}
ok(list(estados.values()).count("concluido") >= 2, f"downloads concluídos {estados}")
ok("falhou" in estados.values(), "download de modelo inexistente falha e é registrado")
ok(viu_progresso, "progresso percentual reportado durante o download")

pag = c.get("/modelos").text
ok(f"{CHAT}" in pag and "all-minilm:latest" in pag, "modelos baixados aparecem como instalados")

# Publicação
r = c.post("/modelos/publicar", data={"csrf": t, "modelo": f"{CHAT}", "nome_publico": "mini-chat"})
ok(flash(c, r)[0] == "ok", "modelo de chat publicado")
r = c.post("/modelos/publicar", data={"csrf": t, "modelo": "all-minilm:latest", "nome_publico": "mini-chat"})
ok("Já existe" in flash(c, r)[1], "nome público duplicado recusado")
r = c.post("/modelos/publicar", data={"csrf": t, "modelo": "all-minilm:latest", "nome_publico": "mini-emb"})
ok(flash(c, r)[0] == "ok", "modelo de embedding publicado")
ids = [m["id"] for m in httpx.get(f"{LITELLM}/v1/models", headers={"Authorization": f"Bearer {MASTER}"}).json()["data"]]
ok("mini-chat" in ids and "mini-emb" in ids, "gateway lista os modelos publicados")

# Chave com os modelos e inferência real pelo gateway
pag = c.get("/chaves/nova").text
ok("mini-chat" in pag, "formulário de chave oferece o modelo publicado")
r = c.post("/chaves/nova", data={"csrf": t, "alias": "teste-modelos", "modelos": ["mini-chat", "mini-emb"],
                                 "rpm": "30", "paralelas": "2", "validade": "1d"})
segredo = re.search(r'id="segredo">(sk-[^<]+)<', r.text).group(1)
token = re.search(r'href="/chaves/([0-9a-f]{64})"', r.text).group(1)
h = {"Authorization": f"Bearer {segredo}"}
r = httpx.post(f"{LITELLM}/v1/chat/completions", headers=h, timeout=300,
               json={"model": "mini-chat", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 8})
ok(r.status_code == 200, f"chat pelo gateway com modelo publicado pelo painel ({r.status_code} {r.text[:120] if r.status_code != 200 else ''})")
r = httpx.post(f"{LITELLM}/v1/embeddings", headers=h, timeout=300, json={"model": "mini-emb", "input": "oi"})
ok(r.status_code == 200 and len(r.json()["data"][0]["embedding"]) > 100, f"embedding pelo gateway ({r.status_code})")

# Memória
pag = c.get("/modelos").text
ok("na memória" in pag, "modelo usado aparece carregado na memória")
r = c.post("/modelos/descarregar", data={"csrf": t, "modelo": f"{CHAT}"})
ok(flash(c, r)[0] == "ok", "descarregar")
r = c.post("/modelos/carregar", data={"csrf": t, "modelo": f"{CHAT}"})
ok(flash(c, r)[0] == "ok", "carregar")

# Chat do painel (master key; o stream do gateway é traduzido em eventos próprios)
def chat_eventos(cliente, token_csrf, **extra):
    campos = {"csrf": token_csrf, "modelo": "mini-chat", "max_tokens": "8",
              "mensagens": json.dumps([{"role": "user", "content": "Say hi"}]), **extra}
    r = cliente.post("/chat/enviar", data=campos, timeout=300)
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.status_code, r.json()
    return r.status_code, [json.loads(b[5:]) for b in r.text.split("\n\n") if b.startswith("data:")]


st, ev = chat_eventos(c, t)
tipos = [e["tipo"] for e in ev]
ok(st == 200 and "delta" in tipos and "erro" not in tipos and tipos[-1] == "fim", f"chat do painel responde em streaming ({tipos[:6]})")
met = next((e for e in ev if e["tipo"] == "metricas"), {})
ok((met.get("tokens_saida") or 0) > 0 and "ttft_ms" in met and met["frio"] is False, f"chat mede tokens e TTFT ({met})")
st, ev = chat_eventos(c, t, temperatura="5")
ok(st == 400 and "Temperatura" in ev.get("erro", ""), "chat recusa parâmetro inválido")
st, ev = chat_eventos(c, t, modelo="mini-emb")
ok(st == 400, "chat não oferece modelo de embedding")
ok(c.get("/chat/estado.json").json().get("mini-chat") is True, "estado do chat mostra o modelo carregado")
r = c.post("/chat/aquecer", data={"csrf": t, "modelo": "mini-chat"}, timeout=600)
ok(r.status_code == 200 and r.json().get("ok"), "aquecer pelo chat")

# Proteções
publicados = {m["model_name"]: m["model_info"]["id"] for m in httpx.get(
    f"{LITELLM}/model/info", headers={"Authorization": f"Bearer {MASTER}"}).json()["data"]}
r = c.post("/modelos/remover", data={"csrf": t, "modelo": f"{CHAT}", "confirmacao": f"{CHAT}"})
ok("publicado no gateway" in flash(c, r)[1], "não remove do Ollama modelo publicado")
r = c.post("/modelos/despublicar", data={"csrf": t, "model_id": publicados["mini-chat"], "confirmacao": "mini-chat"})
ok("liberado nas chaves" in flash(c, r)[1], "não retira do gateway modelo liberado em chave")
c.post(f"/chaves/{token}/alterar", data={"csrf": t, "modelos": ["mini-emb"], "rpm": "30", "paralelas": "2"})
r = c.post("/modelos/despublicar", data={"csrf": t, "model_id": publicados["mini-chat"], "confirmacao": "errado"})
ok("digite o nome" in flash(c, r)[1], "retirar exige confirmar o nome")
r = c.post("/modelos/despublicar", data={"csrf": t, "model_id": publicados["mini-chat"], "confirmacao": "mini-chat"})
ok(flash(c, r)[0] == "ok", "retirado do gateway após sair das chaves")
r = c.post("/modelos/remover", data={"csrf": t, "modelo": f"{CHAT}", "confirmacao": f"{CHAT}"})
ok(flash(c, r)[0] == "ok", "removido do Ollama")
ok(f"{CHAT}" not in c.get("/modelos").text.split("Instalados no Ollama")[1].split("Downloads")[0], "some da lista de instalados")

# Consumo por modelo usa o nome público
cons = c.get(f"/chaves/{token}").text
ok("mini-chat" in cons and "ollama_chat/" not in cons, "consumo da chave usa o nome público do modelo")

# Leitor
r = c.post("/usuarios/novo", data={"csrf": t, "login": "leitor.modelos", "nome": "Leitor Modelos", "papel": "leitor"})
temp = re.search(r'id="temporaria">([^<]+)<', r.text).group(1)
l = httpx.Client(base_url=BASE, follow_redirects=False)
l.post("/login", data={"login": "leitor.modelos", "senha": temp, "csrf": csrf(l.get("/login").text)})
lt = csrf(l.get("/trocar-senha").text)
l.post("/trocar-senha", data={"csrf": lt, "atual": temp, "nova": "consulta tranquila 2026", "confirmacao": "consulta tranquila 2026"})
pag = l.get("/modelos")
ok(pag.status_code == 200 and "Publicar" not in pag.text and "Baixar" not in pag.text, "leitor vê modelos sem ações")
lt = csrf(l.get("/").text)
for rota, dados in [("/modelos/baixar", {"modelo": "x"}), ("/modelos/publicar", {"modelo": "x", "nome_publico": "x"}),
                    ("/modelos/remover", {"modelo": "x"}), ("/modelos/carregar", {"modelo": "x"}),
                    ("/chat/aquecer", {"modelo": "x"})]:
    ok(l.post(rota, data={"csrf": lt, **dados}).status_code == 403, f"leitor bloqueado em {rota}")

aud = c.get("/auditoria").text
for acao in ("Download de modelo iniciado", "Modelo publicado no gateway", "Modelo retirado do gateway", "Modelo removido do Ollama"):
    ok(acao in aud, f"auditoria: {acao}")
print(f"\n{falhas} falha(s)")
sys.exit(1 if falhas else 0)
