#!/usr/bin/env bash
# Revoga uma chave virtual. Uso: scripts/revoke-key.sh <chave-sk-...>
source "$(dirname "$0")/_common.sh"

KEY="${1:?uso: revoke-key.sh <chave>}"
load_master_key
admin_post /key/delete "{\"keys\": [\"$KEY\"]}"
