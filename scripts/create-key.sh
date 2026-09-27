#!/usr/bin/env bash
# Cria uma chave virtual para um consumidor.
# Uso: scripts/create-key.sh <alias> [modelos] [rpm] [paralelas] [validade]
#   alias      nome do consumidor (ex.: time-dados)
#   modelos    lista separada por vírgula; precisam estar publicados no gateway (painel → Modelos)
#   rpm        requisições por minuto
#   paralelas  requisições simultâneas
#   validade   30d, 12h, 6mo... ou "never" para não expirar
#
# Cada parâmetro omitido (ou passado como "") usa o padrão do .env; se também ausente, o valor embutido:
#   modelos   KEY_DEFAULT_MODELS    (sem padrão: obrigatório)
#   rpm       KEY_DEFAULT_RPM       (30)
#   paralelas KEY_DEFAULT_PARALLEL  (2)
#   validade  KEY_DEFAULT_DURATION  (90d)
source "$(dirname "$0")/_common.sh"

ALIAS="${1:?uso: create-key.sh <alias> [modelos] [rpm] [paralelas] [validade]}"
MODELS="${2:-$(env_value KEY_DEFAULT_MODELS)}"
RPM="${3:-$(env_value KEY_DEFAULT_RPM)}";           RPM="${RPM:-30}"
PARALLEL="${4:-$(env_value KEY_DEFAULT_PARALLEL)}"; PARALLEL="${PARALLEL:-2}"
DURATION="${5:-$(env_value KEY_DEFAULT_DURATION)}"; DURATION="${DURATION:-90d}"

if [[ "$ALIAS" == *[\"\\]* || "$ALIAS" =~ [[:cntrl:]] ]]; then
  echo "Alias inválido: não use aspas duplas, barra invertida nem caracteres de controle." >&2
  exit 1
fi
validate_int rpm "$RPM"
validate_int paralelas "$PARALLEL"
validate_duration "$DURATION"
load_master_key

# Lista vazia no LiteLLM libera TODOS os modelos: aqui ela é proibida.
if [[ -z "$MODELS" ]]; then
  echo "Informe os modelos da chave (2º argumento) ou defina KEY_DEFAULT_MODELS no .env." >&2
  exit 1
fi
# Só aceita modelos publicados no gateway.
PUBLICADOS="$(admin_get /v1/models | { grep -oE '"id": ?"[^"]+"' || true; } | sed -E 's/.*"([^"]+)"$/\1/')"
IFS=',' read -ra LISTA <<< "$MODELS"
for m in "${LISTA[@]}"; do
  if ! grep -qxF "$m" <<< "$PUBLICADOS"; then
    echo "Modelo '$m' não está publicado no gateway. Publique-o pelo painel (tela Modelos)." >&2
    echo "Publicados: $(tr '\n' ',' <<< "$PUBLICADOS" | sed 's/,*$//; s/^$/nenhum/')" >&2
    exit 1
  fi
done

# Converte "a,b" em ["a","b"]
MODELS_JSON="[\"${MODELS//,/\",\"}\"]"

# "never" = sem expiração: omite o campo duration.
DURATION_JSON=""
[[ "$DURATION" != "never" ]] && DURATION_JSON=", \"duration\": \"$DURATION\""

# metadata guarda uma cópia do nome dado na criação (o key_alias pode ser editado depois).
# O metadata da chave também acompanha cada registro de uso no banco.
admin_post /key/generate "{
  \"key_alias\": \"$ALIAS\",
  \"models\": $MODELS_JSON,
  \"rpm_limit\": $RPM,
  \"max_parallel_requests\": $PARALLEL,
  \"metadata\": {\"created_for\": \"$ALIAS\", \"created_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
  $DURATION_JSON
}"
echo "Guarde o valor de \"key\" agora: ele é o segredo entregue ao consumidor." >&2
