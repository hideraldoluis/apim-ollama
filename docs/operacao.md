# Operação

Guia do administrador. Comandos executados na GN100, a partir da raiz do repositório. Esta stack não traz automação de operação: tudo abaixo é manual e documentado.

## Subir, parar, atualizar

```bash
docker compose up -d            # sobe tudo (containers têm restart: unless-stopped)
docker compose ps               # estado e healthchecks
docker compose logs -f litellm  # logs de um serviço (ollama, postgres, caddy)
docker compose down             # para e remove containers (volumes permanecem)
```

Atualizar imagens:

```bash
docker compose pull && docker compose up -d
```

A imagem do LiteLLM usa a tag `main-stable`. Para reprodutibilidade, fixe uma versão específica em `docker-compose.yml` e atualize de forma deliberada, lendo as notas de versão. Migrações de banco do LiteLLM rodam na inicialização.

### Iniciar no boot

O Docker inicia no boot (`sudo systemctl enable docker`) e os containers sobem sozinhos pela política `restart: unless-stopped`. Um `docker compose down` explícito impede o reinício até o próximo `up -d`.

## Configuração (`.env`)

| Variável | Função |
|---|---|
| `LITELLM_MASTER_KEY` | Chave administrativa. Guarde em cofre; não a entregue a consumidores. |
| `LITELLM_SALT_KEY` | Criptografa credenciais no banco. **Não altere** depois de definida. |
| `POSTGRES_PASSWORD` | Senha do banco. |
| `SITE_ADDRESS` | Nome DNS da API (vira o CN do certificado). |
| `KEY_DEFAULT_MODELS`, `KEY_DEFAULT_RPM`, `KEY_DEFAULT_PARALLEL`, `KEY_DEFAULT_DURATION` | Padrões das chaves novas. Veja [Padrões das chaves novas](#padrões-das-chaves-novas). |
| `OLLAMA_NUM_PARALLEL` | Requisições simultâneas por modelo no Ollama. |
| `OLLAMA_MAX_LOADED_MODELS` | Quantos modelos ficam carregados na memória ao mesmo tempo. |
| `OLLAMA_KEEP_ALIVE` | Tempo que o modelo permanece carregado sem uso. |

Depois de alterar o `.env`: `docker compose up -d` (recria só o que mudou).

## Gerenciar modelos

```bash
scripts/pull-model.sh <modelo>                # baixa (ex.: llama3.2)
docker compose exec ollama ollama list        # o que está instalado
docker compose exec ollama ollama rm <modelo> # remove
```

Para **publicar** um modelo na API, declare-o em `config/litellm.yaml` (`model_list`) e reinicie o gateway:

```bash
docker compose restart litellm
```

Depois, libere-o nas chaves que devem usá-lo (as chaves têm allowlist de modelos; veja abaixo). Modelos grandes só devem ser adicionados após medir memória e tokens/s; ajuste `OLLAMA_NUM_PARALLEL` e `OLLAMA_MAX_LOADED_MODELS` de acordo, e o `global_max_parallel_requests` em `config/litellm.yaml`.

## Chaves de consumidores

Os scripts falam com `127.0.0.1:4000` e leem `LITELLM_MASTER_KEY` do `.env`.

```bash
# create-key.sh <alias> [modelos] [rpm] [paralelas] [validade]
scripts/create-key.sh time-dados                          # todos os padrões do .env
scripts/create-key.sh time-dados llama3.2,outro 60 4 30d  # tudo explícito
scripts/create-key.sh time-dados "" "" "" 30d             # só a validade; o resto usa o padrão
scripts/create-key.sh servico-x llama3.2 30 2 never       # sem expiração
scripts/list-keys.sh
scripts/update-key.sh sk-... --duration 60d --rpm 120     # altera uma chave existente
scripts/revoke-key.sh sk-...
```

- Uma chave por consumidor (time ou serviço), para atribuir uso e revogar sem afetar os demais.
- A chave só aparece na criação. Entregue por canal seguro.
- **Rotação:** crie a nova chave, entregue ao consumidor, revogue a antiga após a migração.

### Padrões das chaves novas

Os padrões são definidos no `.env` e lidos a cada execução do script (não é preciso reiniciar nada):

| Variável | Parâmetro | Valor embutido |
|---|---|---|
| `KEY_DEFAULT_MODELS` | modelos liberados, separados por vírgula | `llama3.2` |
| `KEY_DEFAULT_RPM` | requisições por minuto | `30` |
| `KEY_DEFAULT_PARALLEL` | requisições simultâneas | `2` |
| `KEY_DEFAULT_DURATION` | validade da chave | `90d` |

Para cada parâmetro, vale a primeira fonte definida: **argumento do `create-key.sh`** (também vale `""`, que o ignora), depois **variável do `.env`**, depois o **valor embutido**. Mudar o `.env` afeta apenas chaves criadas depois; as existentes não mudam (use `update-key.sh`).

### Validade das chaves

Formato: `<número><unidade>` com unidade `s`, `m`, `h`, `d`, `w` ou `mo` (ex.: `12h`, `30d`, `6mo`), ou `never` para não expirar.

`update-key.sh --duration <valor>` redefine a validade de uma chave existente, **contada a partir de agora** (não soma à validade atual); `--duration never` remove a expiração. Chaves expiradas passam a receber `401`. Uma chave sem expiração só deixa de valer se for revogada, então prefira validade finita para consumidores humanos e reserve `never` para serviços com rotação controlada.

## Consultar uso e logs

### Registro de prompts no banco

Toda requisição fica gravada no Postgres, na tabela `LiteLLM_SpendLogs` (habilitado por `store_prompts_in_spend_logs` em `config/litellm.yaml`). Cada registro contém:

| Dado | Onde |
|---|---|
| Prompt enviado (todas as mensagens) | `proxy_server_request->'messages'` |
| Resposta do modelo | `response` |
| Chave (somente o **hash**, nunca o segredo) | `api_key` |
| Nome dado na criação (alias) vigente no momento da requisição | `metadata->>'user_api_key_alias'` |
| Tokens processados por requisição (prompt, resposta e total), com ou sem streaming | `prompt_tokens`, `completion_tokens`, `total_tokens` |
| Modelo, status, duração, IP do cliente | `model`, `status`, `request_duration_ms`, `requester_ip_address` |

Requisições que falham (ex.: `401`, `429`, corpo inválido) também são registradas, com o prompt vazio quando o corpo não chegou a ser lido.

O nome do usuário/time/grupo é o `<alias>` passado ao `create-key.sh`. Ele é gravado de três formas:
1. `key_alias` na tabela `LiteLLM_VerificationToken` (é único: não é possível criar dois com o mesmo nome);
2. `metadata.created_for` e `metadata.created_at` na mesma tabela, uma cópia do nome e da data de criação que não muda se o alias for editado depois;
3. dentro de cada registro de `LiteLLM_SpendLogs`, no momento da requisição. Por isso o histórico mantém o nome mesmo se a chave for revogada. Nesse caso só o `criado_para` deixa de aparecer, pois vem da linha da chave.

O segredo `sk-...` em texto puro **não** é gravado em lugar nenhum: o banco guarda apenas o hash.

Consultar:

```bash
scripts/logs.sh                      # últimos 20 registros, todos os consumidores
scripts/logs.sh time-dados 50        # últimos 50 do consumidor "time-dados"
```

Os tokens são os contados pelo próprio Ollama (mesmos números do campo `usage` da resposta da API). Exemplo de consulta livre (`docker compose exec postgres psql -U litellm -d litellm`), com o consumo por consumidor nos últimos 7 dias:

```sql
select metadata->>'user_api_key_alias' as nome,
       count(*)               as requisicoes,
       sum(prompt_tokens)     as tok_prompt,
       sum(completion_tokens) as tok_resposta,
       sum(total_tokens)      as tok_total
from "LiteLLM_SpendLogs"
where "startTime" > now() - interval '7 days'
group by 1 order by tok_total desc;
```

> **Dado sensível.** O banco passa a conter o conteúdo integral de prompts e respostas. Restrinja o acesso ao host e ao Postgres (a porta 5432 não é publicada), trate os backups do banco com o mesmo cuidado e considere a política de retenção: por padrão nada é apagado e a tabela cresce a cada requisição. Para apagar registros antigos automaticamente, defina em `general_settings` de `config/litellm.yaml` `maximum_spend_logs_retention_period: "30d"` (e opcionalmente `maximum_spend_logs_retention_interval`) e reinicie o gateway.

### Outras consultas

- Uso agregado por chave/modelo: API do LiteLLM em `127.0.0.1:4000` (`/spend/logs`, `/key/info`) com a master key.
- Métricas de sistema/GPU: `nvidia-smi`, `docker stats`.
- Logs de acesso da borda: `docker compose logs caddy`.
- Logs de inferência: `docker compose logs ollama`.

## Backup e restore

O que importa: o Postgres (chaves, uso e o histórico de prompts, que é sensível), o volume do Caddy (CA interna e certificados) e o `.env`. Os modelos podem ser baixados de novo.

```bash
# Backup do Postgres
docker compose exec -T postgres pg_dump -U litellm litellm | gzip > backup-litellm-$(date +%F).sql.gz

# Restore (com a stack parada, exceto o postgres)
docker compose up -d postgres
gunzip -c backup-litellm-AAAA-MM-DD.sql.gz | docker compose exec -T postgres psql -U litellm litellm

# Backup do volume do Caddy
docker run --rm -v apim-ollama_caddy-data:/data -v "$PWD":/backup alpine tar czf /backup/caddy-data-$(date +%F).tgz -C /data .
```

Guarde `.env` e backups fora da máquina e protegidos (contêm segredos). Defina a frequência conforme a criticidade; sugestão: diária para o Postgres.

## Diagnóstico

| Sintoma | Causa provável | Ação |
|---|---|---|
| Primeira chamada muito lenta | Modelo frio sendo carregado | Esperado. Aumente `OLLAMA_KEEP_ALIVE` ou aqueça o modelo com uma chamada. |
| `429` frequente | Limite da chave ou `global_max_parallel_requests` | Ajuste os limites da chave ou o teto global. Confirme que `OLLAMA_NUM_PARALLEL` acompanha. |
| `502/504` no cliente | LiteLLM ou Ollama fora do ar / modelo trocando | `docker compose ps`; `docker compose logs litellm ollama`. |
| Ollama sem GPU (lento, CPU alta) | Runtime NVIDIA não configurado | `docker compose exec ollama nvidia-smi`; revise o NVIDIA Container Toolkit; `docker compose logs ollama` mostra a GPU detectada. |
| Falta de memória ao carregar | Modelo (ou vários) maior que a memória | Reduza `OLLAMA_MAX_LOADED_MODELS`/`NUM_PARALLEL` (contexto por requisição também consome memória) ou use um modelo menor/quantizado. |
| `401` com chave correta | Chave expirada, revogada, ou modelo fora da allowlist | `scripts/list-keys.sh`; confira `models` e `expires` da chave. |
| Erro de certificado no cliente | CA interna não instalada | Veja [rede-e-tls.md](rede-e-tls.md). |
| `litellm` não sobe | `.env` incompleto ou Postgres indisponível | `docker compose logs litellm postgres`. |
| Rota `/ui` ou `/key/*` dá 404 | Comportamento esperado na borda | Use `127.0.0.1:4000` no próprio host. |

## Checklist pós-deploy

1. `docker compose ps` com tudo saudável.
2. `docker compose exec ollama nvidia-smi` mostra a GPU; anote tokens/s de um modelo real.
3. `scripts/smoke-test.sh https://<SITE_ADDRESS> <chave> <modelo>` passa.
4. A porta 11434 não responde de outro host; a 4000 só em loopback; a 443 só do CIDR interno.
5. Backup do Postgres testado (restore em ambiente de teste).
