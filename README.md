# apim-ollama

API Management para o [Ollama](https://ollama.com): expõe os modelos de uma máquina (alvo: Acer GN100, Linux ARM64 com GPU NVIDIA) como uma API compatível com OpenAI, com autenticação por chave, limites por consumidor e log de uso. Acesso apenas interno.

```
Cliente interno ──HTTPS──► Caddy (:443) ──► LiteLLM (:4000) ──► Ollama (:11434)
                           TLS + rotas        chaves, limites,    container com GPU,
                           públicas apenas    allowlist, uso      sem porta publicada
                                                   └──► Postgres
```

- **Ollama** roda em container e só é alcançável pela rede interna do compose.
- **LiteLLM** é o gateway: chaves virtuais, allowlist de modelos, RPM, requisições paralelas, expiração.
- **Caddy** termina o TLS e expõe somente `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` e `/health/*`. Todo o resto responde `404`.
- A administração (criar/revogar chaves) usa `127.0.0.1:4000` no próprio host, pelos scripts, ou o **painel de governança** em `https://<SITE_ADDRESS>:8443` (restrito à rede de gestão; veja [docs/painel.md](docs/painel.md)).
- **Painel:** login local com papéis admin/leitor; gestão de chaves e de modelos (baixar, publicar no gateway, carregar), chat de teste com os modelos publicados, consumo por chave, logs sem texto para leitores e texto só para admin, com auditoria. Retenção dos logs: 180 dias.
- **Auditoria:** cada requisição grava no Postgres o prompt, a resposta, o hash da chave e o nome dado ao usuário/time/grupo na criação (`scripts/logs.sh`). Veja [docs/operacao.md](docs/operacao.md#registro-de-prompts-no-banco).

## Requisitos (máquina de deploy)

- Docker + Docker Compose v2
- NVIDIA Container Toolkit (`docker run --rm --gpus all ubuntu nvidia-smi` deve funcionar)
- `curl` e `bash` para os scripts

## Início rápido

```bash
cp .env.example .env         # preencha LITELLM_MASTER_KEY, LITELLM_SALT_KEY, POSTGRES_PASSWORD, SITE_ADDRESS e PAINEL_*
chmod +x scripts/*.sh

docker compose up -d --build
docker compose run --rm painel python -m app.cli criar-admin <login> "<Nome>"   # primeiro admin do painel
```

A instalação começa **sem nenhum modelo**: a API não atende nada até um admin, no painel (`https://localhost:8443`, tela **Modelos**), baixar um modelo e publicá-lo no gateway. Depois:

```bash
scripts/create-key.sh time-teste <modelo>   # imprime a chave sk-... (modelo = nome público publicado)
INSECURE=1 scripts/smoke-test.sh https://localhost <chave-sk> <modelo>
```

`INSECURE=1` só é necessário enquanto a CA interna do Caddy não estiver instalada nos clientes (veja [docs/rede-e-tls.md](docs/rede-e-tls.md)).

## Documentação

- [docs/uso.md](docs/uso.md): como os consumidores chamam a API (curl, SDK OpenAI, streaming, erros).
- [docs/rede-e-tls.md](docs/rede-e-tls.md): firewall, TLS e distribuição da CA.
- [docs/operacao.md](docs/operacao.md): operação do dia a dia (atualização, modelos, chaves, backup, diagnóstico).
- [docs/painel.md](docs/painel.md): painel de governança (papéis, usuários, chaves, logs, acesso, recuperação).

## Estrutura

```
docker-compose.yml     ollama + postgres + litellm + caddy + painel (+ painel-init)
Caddyfile              borda: TLS e rotas públicas
config/litellm.yaml    timeouts, limites globais, retenção (sem modelos: publicados pelo painel)
painel/                painel de governança (FastAPI) e schema SQL
scripts/               pull-model, create/update/revoke/list-keys, logs, smoke-test
docs/                  uso, rede/TLS, operação
```
