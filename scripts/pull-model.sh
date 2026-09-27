#!/usr/bin/env bash
# Baixa um modelo no Ollama (container). Uso: scripts/pull-model.sh <modelo>
# Depois, publique-o no gateway pelo painel (tela Modelos) e libere-o nas chaves.
# O painel também baixa modelos, com progresso e auditoria.
source "$(dirname "$0")/_common.sh"

MODEL="${1:?uso: pull-model.sh <modelo>  (ex.: qwen2.5:7b)}"
cd "$ROOT_DIR"
docker compose exec ollama ollama pull "$MODEL"
