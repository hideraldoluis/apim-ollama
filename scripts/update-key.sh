#!/usr/bin/env bash
# Altera uma chave existente. Informe só o que quer mudar.
# Uso: scripts/update-key.sh <chave-sk-...> [--duration 30d|never] [--rpm N] [--parallel N] [--models a,b]
#   --duration  nova validade contada a partir de agora ("never" = sem expiração)
source "$(dirname "$0")/_common.sh"

KEY="${1:?uso: update-key.sh <chave> [--duration D] [--rpm N] [--parallel N] [--models a,b]}"
shift

FIELDS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      validate_duration "${2:?--duration requer valor}"
      if [[ "$2" == "never" ]]; then FIELDS+=("\"duration\": null, \"expires\": null"); else FIELDS+=("\"duration\": \"$2\""); fi
      shift 2 ;;
    --rpm)      FIELDS+=("\"rpm_limit\": ${2:?--rpm requer valor}"); shift 2 ;;
    --parallel) FIELDS+=("\"max_parallel_requests\": ${2:?--parallel requer valor}"); shift 2 ;;
    --models)   m="${2:?--models requer valor}"; FIELDS+=("\"models\": [\"${m//,/\",\"}\"]"); shift 2 ;;
    *) echo "Opção desconhecida: $1" >&2; exit 1 ;;
  esac
done

if [[ ${#FIELDS[@]} -eq 0 ]]; then
  echo "Nada para alterar. Informe ao menos uma opção." >&2
  exit 1
fi

load_master_key
BODY="{\"key\": \"$KEY\""
for f in "${FIELDS[@]}"; do BODY+=", $f"; done
BODY+="}"
admin_post /key/update "$BODY"
