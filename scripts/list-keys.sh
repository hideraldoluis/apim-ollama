#!/usr/bin/env bash
# Lista as chaves existentes (aliases, limites, gasto). Uso: scripts/list-keys.sh
source "$(dirname "$0")/_common.sh"

load_master_key
admin_get "/key/list?return_full_object=true"
