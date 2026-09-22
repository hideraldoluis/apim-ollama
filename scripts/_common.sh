#!/usr/bin/env bash
# Funções compartilhadas pelos scripts de administração. Use com: source "$(dirname "$0")/_common.sh"
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Endpoint administrativo: loopback do host (não exposto na borda).
ADMIN_URL="${ADMIN_URL:-http://127.0.0.1:4000}"

load_master_key() {
  if [[ -z "${LITELLM_MASTER_KEY:-}" && -f "$ROOT_DIR/.env" ]]; then
    LITELLM_MASTER_KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$ROOT_DIR/.env" | head -n1 | cut -d= -f2-)"
  fi
  if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
    echo "LITELLM_MASTER_KEY não definido (variável de ambiente ou .env)." >&2
    exit 1
  fi
}

# Lê uma variável do .env (ou do ambiente) sem exigir que ela exista.
env_value() {
  local name="$1" value="${!1:-}"
  if [[ -z "$value" && -f "$ROOT_DIR/.env" ]]; then
    value="$(grep -E "^${name}=" "$ROOT_DIR/.env" | head -n1 | cut -d= -f2- || true)"
  fi
  echo "$value"
}

# Valida uma duração no formato do LiteLLM: <número><s|m|h|d|w|mo> ou "never".
validate_duration() {
  if [[ ! "$1" =~ ^([0-9]+(s|m|h|d|w|mo)|never)$ ]]; then
    echo "Duração inválida: '$1'. Use, por exemplo, 30d, 12h, 6mo ou never (sem expiração)." >&2
    exit 1
  fi
}

# Valida um inteiro positivo. Uso: validate_int <nome> <valor>
validate_int() {
  if [[ ! "$2" =~ ^[1-9][0-9]*$ ]]; then
    echo "Valor inválido para $1: '$2'. Use um inteiro positivo." >&2
    exit 1
  fi
}

admin_post() {
  # admin_post <caminho> <json>
  curl -sS --fail-with-body -X POST "$ADMIN_URL$1" \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$2"
  echo
}

admin_get() {
  curl -sS --fail-with-body "$ADMIN_URL$1" -H "Authorization: Bearer $LITELLM_MASTER_KEY"
  echo
}
