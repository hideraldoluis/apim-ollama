#!/usr/bin/env bash
# Consulta os prompts gravados no banco.
# Uso: scripts/logs.sh [alias] [limite]
#   alias   filtra pelo nome dado na criação da chave (padrão: todos)
#   limite  quantidade de registros, do mais recente ao mais antigo (padrão: 20)
source "$(dirname "$0")/_common.sh"

ALIAS="${1:-}"
LIMIT="${2:-20}"
validate_int limite "$LIMIT"

cd "$ROOT_DIR"
# O alias vai como variável do psql (:'alias'), que faz o escape do valor.
docker compose exec -T postgres psql -U litellm -d litellm -v "alias=$ALIAS" -v "lim=$LIMIT" <<'SQL'
select s."startTime"                                 as quando,
       s.metadata->>'user_api_key_alias'             as nome,
       k.metadata->>'created_for'                    as criado_para,
       s.model,
       s.status,
       s.prompt_tokens                               as tok_prompt,
       s.completion_tokens                           as tok_resposta,
       s.total_tokens                                as tok_total,
       s.proxy_server_request->'messages'            as prompt,
       regexp_replace(s.response->'choices'->0->'message'->>'content', '\s+', ' ', 'g') as resposta
from "LiteLLM_SpendLogs" s
left join "LiteLLM_VerificationToken" k on k.token = s.api_key
where (:'alias' = '' or s.metadata->>'user_api_key_alias' = :'alias')
order by s."startTime" desc
limit :lim;
SQL
