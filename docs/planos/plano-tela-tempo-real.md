# Plano: tela "Ao vivo" (chamadas e fila em tempo real)

Plano para implementação posterior (modelo qwen3.8). Escrito a partir do código atual; os pontos marcados **[verificar]** dependem de versões/comportamentos que precisam ser confirmados antes de codar.

## 1. Objetivo

Nova tela `/ao-vivo` no painel mostrando, atualizada em tempo real:

- **Em andamento**: chamadas que o gateway aceitou e ainda não terminaram (chave/alias, modelo, stream sim/não, há quanto tempo, fase).
- **Fila**: chamadas aguardando vaga no Ollama (ainda sem primeiro token), por modelo.
- **Capacidade**: slots usados / `OLLAMA_NUM_PARALLEL` e teto global (`global_max_parallel_requests`), modelos carregados e VRAM (`/api/ps`).
- **Fluxo recente**: últimas N chamadas concluídas (status, latência, tokens, erro/429).
- **Contadores**: req/min, erros/min, 429 (rejeições) na última janela.

Fora de escopo: texto de prompt/resposta (nunca trafega nesta tela; respeita a separação de privilégios do painel).

## 2. Fatos do código que moldam o desenho

- Não existe fila no LiteLLM: acima de `global_max_parallel_requests` (8) ou do limite da chave, a resposta é **429** ([config/litellm.yaml:24](../config/litellm.yaml)). A fila real é **interna do Ollama** (`OLLAMA_NUM_PARALLEL`, padrão 2), e o Ollama **não expõe** profundidade de fila.
- Logo, "fila" = chamadas em andamento no gateway **sem primeiro token** além da capacidade do modelo. Precisa de evento de "primeiro token" para distinguir *aguardando* de *gerando*.
- `LiteLLM_SpendLogs` só recebe a linha **depois** de terminar, e o LiteLLM grava em lote (atraso de dezenas de segundos). Serve para histórico, **não** para tempo real.
- O painel já tem o padrão de SSE via fetch ([painel.js](../painel/app/static/painel.js), [rotas/chat.py](../painel/app/rotas/chat.py)), CSP `script-src 'self'` (sem JS inline), `Cache-Control: no-store`, papéis `admin`/`leitor` ([autenticacao.py](../painel/app/autenticacao.py)) e `ollama.carregados()` para `/api/ps`.

## 3. Arquitetura escolhida

```
Cliente ─► Caddy ─► LiteLLM ──(callback customizado)──POST──► painel /interno/eventos
                                                               │ estado em memória + anel de recentes
Navegador ◄──── SSE /ao-vivo/fluxo ◄───────────────────────────┘
                 (+ poll de /api/ps pelo próprio painel a cada 3 s)
```

Por quê: o callback do LiteLLM é o único ponto que vê início, primeiro token, fim e falha de cada chamada no instante em que ocorrem. O painel (FastAPI, já com SSE) guarda o estado e distribui. Alternativas descartadas: Prometheus `/metrics` (gauge de in-flight depende de versão/licença e não dá detalhe por chamada **[verificar]**), polling de SpendLogs (atraso), tabela Postgres (carga de escrita por evento sem necessidade).

**Premissa a confirmar:** painel roda com **1 worker** uvicorn (estado em memória). Se houver mais de um, mover o estado para uma tabela `UNLOGGED` no schema `painel`. **[verificar no Dockerfile do painel]**

## 4. Contrato de eventos (LiteLLM → painel)

`POST http://painel:8000/interno/eventos`, header `X-Eventos-Token: $PAINEL_EVENTOS_TOKEN`, corpo JSON, timeout curto (≤1 s), **fire-and-forget**: falha do painel nunca pode afetar a API.

```json
{"tipo": "inicio|primeiro_token|fim|falha",
 "id": "<litellm_call_id>", "ts": 1730000000.123,
 "modelo": "qwen3:8b", "alias": "time-x", "chave_hash": "ab12…(8 chars)",
 "stream": true,
 "status": "ok|erro|429", "http": 200, "erro_tipo": "RateLimitError",
 "tok_prompt": 123, "tok_resposta": 456}
```

Sem prompt, sem resposta, sem a chave inteira. Campos de tokens/status só em `fim`/`falha`.

## 5. Passos de implementação (ordem)

1. **Callback do LiteLLM** — `config/callbacks/eventos_painel.py` com `CustomLogger`:
   - `async_pre_call_hook` → `inicio`; `async_post_call_streaming_iterator_hook` (ou `async_log_stream_event`) → `primeiro_token` na 1ª chunk; `async_log_success_event` → `fim`; `async_log_failure_event` → `falha` (inclui 429 por limite de chave/global; verificar se o hook de pre-call dispara antes da recusa para registrar rejeições **[verificar]**).
   - Envio via `httpx.AsyncClient` global, `asyncio.create_task`, engolindo exceções.
   - Registrar em `config/litellm.yaml`: `litellm_settings.callbacks: eventos_painel.proxy_handler_instance`; montar o arquivo no serviço `litellm` em `docker-compose.yml` (volume `:ro`, já que `config/litellm.yaml` é montado assim) e adicionar `PAINEL_EVENTOS_URL` / `PAINEL_EVENTOS_TOKEN` ao ambiente. Incluir o token em `.env.example`.
2. **Estado no painel** — `painel/app/aovivo.py`:
   - `ativas: dict[id, Chamada]` (início, fase `aguardando|gerando`, modelo, alias, stream); `recentes: deque(maxlen=200)`; contadores por minuto (balde de 60 s, janela de 15 min).
   - Expurgo de chamadas "órfãs" (sem `fim` após `request_timeout`=600 s + folga) para não vazar memória se um evento se perder.
   - Fila por modelo = `aguardando` ordenadas por início; posição = índice; capacidade vinda de `config` (`OLLAMA_NUM_PARALLEL`, `global_max_parallel_requests` passados como env para o painel).
   - Pub/sub simples: `set` de `asyncio.Queue` de assinantes; publicar snapshot-diff a cada evento, com coalescência (máx. ~4 envios/s).
3. **Rotas** — `painel/app/rotas/aovivo.py`, registrada em [main.py](../painel/app/main.py):
   - `POST /interno/eventos`: valida token com `hmac.compare_digest`; **sem** sessão/CSRF; restrito à rede do compose (não publicado no Caddy — o Caddy já só expõe rotas explícitas, confirmar o bloco :8443 **[verificar]**). Adicionar à exceção do middleware de Origin só se necessário (não há header `Origin` em chamadas servidor-a-servidor).
   - `GET /ao-vivo`: página (qualquer usuário logado, `leitor` incluso, pois não há texto).
   - `GET /ao-vivo/fluxo`: SSE (`text/event-stream`); primeiro evento `snapshot` completo, depois `delta`; heartbeat a cada 15 s; `GET` com cookie de sessão (EventSource funciona, sem CSRF por ser leitura).
   - `GET /ao-vivo/estado.json`: snapshot (fallback por polling e para testes).
   - Capacidade/modelos carregados: tarefa do painel que chama `ollama.carregados()` a cada 3 s (reuso, tolerando `ErroOllama`) e injeta no snapshot.
4. **Template e JS** — `templates/aovivo.html` (extends `base.html`; usa classes de `painel.css`: `cartoes`, `painel`, `tabela`, `selo`); bloco em `painel.js` guardado por `[data-aovivo]`, como o do chat/downloads: `EventSource`, render **somente por `textContent`**, cronômetro local por linha (tick de 1 s, sem pedir ao servidor), reconexão com backoff e indicador "desconectado". Link "Ao vivo" em [base.html](../painel/app/templates/base.html). Respeitar `prefers-reduced-motion` (sem animações obrigatórias).
5. **Proxy** — garantir `flush_interval -1` no `reverse_proxy` do painel no `Caddyfile` (bloco :8443) para o SSE não ser bufferizado. **[verificar]**
6. **Histórico ao reiniciar** — após restart do painel, `recentes` começa vazio; opcional: pré-carregar últimas 50 de `LiteLLM_SpendLogs` via função SQL de metadados (sem texto), no padrão das funções `plpgsql` de [schema.sql](../painel/sql/schema.sql). Deixar para uma 2ª fase.
7. **Docs** — seção em [docs/painel.md](painel.md) (o que a tela mostra, definição de "fila", limites) e nota em [docs/operacao.md](operacao.md) (novo token, callback).

## 6. Layout da tela

1. Faixa de cartões: *Em andamento*, *Na fila*, *Slots (usados/capacidade)*, *req/min*, *Rejeitadas (429)/min*, estado da conexão.
2. Tabela **Em andamento** (colunas: início/tempo decorrido, chave/alias, modelo, tipo stream/normal, fase com `selo`, posição na fila quando `aguardando`).
3. Por modelo: carregado? VRAM, expira em, ocupação de slots.
4. Tabela **Recentes** (hora, alias, modelo, status, latência, tok in/out), filtro por modelo/alias no cliente.
5. Mini-gráfico req/min dos últimos 15 min (SVG simples, sem biblioteca externa por causa da CSP).

## 7. Segurança e privacidade

- Nenhum texto de conversa no evento nem na tela; `chave_hash` truncado + alias apenas.
- Endpoint `/interno/eventos` autenticado por token compartilhado, comparação em tempo constante, não exposto na borda; rate-limit/limite de tamanho do corpo (ex.: 4 KB).
- Falha do painel ou do callback **não** pode degradar a API (timeouts curtos, exceções engolidas, tarefa assíncrona).
- Cabeçalhos e CSP existentes continuam valendo (sem JS/CSS inline).

## 8. Testes

- `painel/tests/test_aovivo.py` (pytest, padrão de `test_chat.py`): sequência de eventos produz estado correto (inicio→primeiro_token→fim; falha 429; órfã expurgada; fila e posições por modelo; token inválido → 403; payload sem campos → 422).
- Teste do callback isolado: monta eventos a partir de objetos simulados e confirma que nenhum campo de texto é enviado e que exceção de rede é engolida.
- E2E manual/`tests/e2e.py`: subir stack, disparar N+3 chamadas concorrentes em stream a um modelo com `NUM_PARALLEL=2`, ver 2 "gerando" e demais "aguardando"; disparar acima do teto global e ver contador de 429; derrubar o painel e confirmar que a API segue respondendo.

## 9. Critérios de aceite

- Chamada nova aparece na tela em ≤1 s; some de "em andamento" em ≤1 s após terminar.
- Fila e posições coerentes com o cenário de teste acima.
- Leitor vê a tela; nenhum dado de prompt/resposta em HTML, SSE ou logs.
- Painel reiniciado/desconectado: tela mostra "desconectado" e reconecta sozinha; sem vazamento de memória (chamadas órfãs expiram).
- Sem alterações de comportamento na API pública.

## 10. Riscos e decisões em aberto

| Item | Risco | Mitigação |
|---|---|---|
| Hooks do `CustomLogger` variam por versão do LiteLLM (`main-stable`) | Eventos faltando (ex.: primeiro token, 429) | Spike de 1 h no passo 1 imprimindo os hooks em log antes de construir o resto |
| "Fila" é estimada (Ollama não expõe) | Número pode divergir do real em modelos multi-instância | Rotular como "aguardando 1º token" na UI |
| Estado em memória | Perde dados no restart; quebra com >1 worker | Fase 2 (item 6) ou tabela `UNLOGGED` |
| Eventos perdidos | Chamada "presa" na tela | Expurgo por timeout + reconciliação ao receber `fim` desconhecido |

## 11. Divisão sugerida para o qwen3.8

1. Spike dos hooks + callback (passo 1) → validar com `curl` e logs.
2. `aovivo.py` + rota `/interno/eventos` + testes (passos 2–3, 8).
3. SSE + página + JS (passos 3–4) → depois Caddy e docs (5, 7).
Cada etapa em commit próprio; rodar `painel/tests/test_*.py` ao fim de cada uma.
