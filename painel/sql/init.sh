#!/bin/sh
# Serviço painel-init: espera o LiteLLM terminar as migrações e aplica painel/sql/schema.sql (idempotente).
set -eu

: "${PAINEL_DB_APP_PASSWORD:?defina PAINEL_DB_APP_PASSWORD no .env}"
: "${PAINEL_DB_CONTEUDO_PASSWORD:?defina PAINEL_DB_CONTEUDO_PASSWORD no .env}"

export PGHOST=postgres PGUSER=litellm PGDATABASE=litellm
LITELLM_HEALTH="${LITELLM_HEALTH:-http://litellm:4000/health/liveliness}"

# O LiteLLM só responde depois de aplicar as migrações do banco; alterar as tabelas antes disso
# concorre com a migração ("tuple concurrently updated").
tentativas=0
until wget -q -T 5 -O /dev/null "$LITELLM_HEALTH" 2>/dev/null; do
  tentativas=$((tentativas + 1))
  if [ "$tentativas" -gt 60 ]; then
    echo "O LiteLLM não respondeu após 5 minutos. Veja: docker compose logs litellm" >&2
    exit 1
  fi
  echo "Aguardando o LiteLLM concluir a inicialização..."
  sleep 5
done

for tentativa in 1 2 3; do
  if psql -q -v ON_ERROR_STOP=1 \
       -v "app_senha=$PAINEL_DB_APP_PASSWORD" \
       -v "conteudo_senha=$PAINEL_DB_CONTEUDO_PASSWORD" \
       -f /sql/schema.sql; then
    echo "Schema do painel aplicado."
    exit 0
  fi
  echo "Falha ao aplicar o schema (tentativa $tentativa); repetindo em 10 s..." >&2
  sleep 10
done
exit 1
