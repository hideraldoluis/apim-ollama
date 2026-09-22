#!/usr/bin/env bash
# Teste de aceitação da borda. Uso: scripts/smoke-test.sh <base_url> <api_key> [modelo]
#   ex.: scripts/smoke-test.sh https://localhost sk-abc llama3.2
# Usa -k (ignora validação do certificado) apenas se INSECURE=1 (CA interna ainda não instalada).
set -uo pipefail

BASE="${1:?uso: smoke-test.sh <base_url> <api_key> [modelo]}"
KEY="${2:?informe a api_key}"
MODEL="${3:-llama3.2}"
CURL_OPTS=(-sS -o /dev/null -w '%{http_code}' --max-time 300)
[[ "${INSECURE:-0}" == "1" ]] && CURL_OPTS+=(-k)

FAILS=0
check() {
  # check <descrição> <esperado(s) separados por |> <obtido>
  if [[ "|$2|" == *"|$3|"* ]]; then
    echo "OK    $1 ($3)"
  else
    echo "FALHA $1 (esperado $2, obtido $3)"
    FAILS=$((FAILS + 1))
  fi
}

BODY="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Responda apenas: ok\"}],\"max_tokens\":16}"
STREAM_BODY="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Responda apenas: ok\"}],\"max_tokens\":16,\"stream\":true}"

check "health"                    200 "$(curl "${CURL_OPTS[@]}" "$BASE/health/liveliness")"
check "GET /v1/models"            200 "$(curl "${CURL_OPTS[@]}" "$BASE/v1/models" -H "Authorization: Bearer $KEY")"
check "chat sem streaming"        200 "$(curl "${CURL_OPTS[@]}" "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$BODY")"
check "chat com streaming"        200 "$(curl "${CURL_OPTS[@]}" "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$STREAM_BODY")"
check "sem chave"                 401 "$(curl "${CURL_OPTS[@]}" "$BASE/v1/chat/completions" -H 'Content-Type: application/json' -d "$BODY")"
check "chave inválida"            401 "$(curl "${CURL_OPTS[@]}" "$BASE/v1/models" -H 'Authorization: Bearer sk-invalida')"
check "modelo fora da allowlist"  "401|403" "$(curl "${CURL_OPTS[@]}" "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"model":"modelo-inexistente","messages":[{"role":"user","content":"x"}]}')"
check "admin não exposto"         404 "$(curl "${CURL_OPTS[@]}" "$BASE/key/generate" -X POST -H "Authorization: Bearer $KEY")"
check "UI não exposta"            404 "$(curl "${CURL_OPTS[@]}" "$BASE/ui")"

echo
if [[ $FAILS -eq 0 ]]; then echo "Todos os testes passaram."; else echo "$FAILS teste(s) falharam."; exit 1; fi
