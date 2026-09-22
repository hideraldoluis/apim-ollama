#!/usr/bin/env bash
# Baixa um modelo no Ollama (container). Uso: scripts/pull-model.sh <modelo>
# Depois, declare-o em config/litellm.yaml e reinicie: docker compose restart litellm
source "$(dirname "$0")/_common.sh"

MODEL="${1:?uso: pull-model.sh <modelo>  (ex.: llama3.2)}"
cd "$ROOT_DIR"
docker compose exec ollama ollama pull "$MODEL"
