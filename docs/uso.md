# Uso da API (para consumidores)

Peça ao administrador: a **URL base** (ex.: `https://gn100.empresa.local`), sua **chave** (`sk-...`) e a lista de **modelos** liberados para você.

A API é compatível com OpenAI. Rotas disponíveis:

| Método | Rota | Uso |
|---|---|---|
| GET | `/v1/models` | Lista os modelos que sua chave pode usar |
| POST | `/v1/chat/completions` | Chat (com ou sem streaming) |
| POST | `/v1/completions` | Completion de texto |
| POST | `/v1/embeddings` | Embeddings (se houver modelo de embeddings liberado) |
| GET | `/health/liveliness` | Verificação de disponibilidade (sem chave) |

## curl

```bash
export BASE=https://gn100.empresa.local
export KEY=sk-...

curl $BASE/v1/models -H "Authorization: Bearer $KEY"

curl $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"llama3.2","messages":[{"role":"user","content":"Olá!"}]}'

# Streaming (SSE): use -N para não bufferizar
curl -N $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"llama3.2","stream":true,"messages":[{"role":"user","content":"Conte até 5"}]}'
```

## SDK OpenAI

```python
from openai import OpenAI

client = OpenAI(base_url="https://gn100.empresa.local/v1", api_key="sk-...")
resp = client.chat.completions.create(
    model="llama3.2",
    messages=[{"role": "user", "content": "Olá!"}],
)
print(resp.choices[0].message.content)
```

```js
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "https://gn100.empresa.local/v1", apiKey: "sk-..." });
```

## Erros comuns

| Código | Significado | O que fazer |
|---|---|---|
| 401 | Chave ausente, inválida, expirada ou sem acesso ao modelo | Confira a chave e o nome do modelo (`/v1/models`) |
| 404 | Rota não exposta | Use apenas as rotas da tabela acima |
| 413 | Corpo maior que 5 MB | Reduza o prompt |
| 429 | Limite de requisições (por minuto ou simultâneas) atingido | Aguarde e tente de novo com backoff |
| 5xx / timeout | Modelo carregando ou máquina ocupada | A primeira chamada após inatividade demora mais (carga do modelo); tente novamente |

## Privacidade

Todas as requisições são registradas: o **prompt enviado, a resposta**, o nome do seu usuário/time/grupo (o nome dado na criação da chave), o modelo, o consumo de tokens e o IP de origem. Os administradores da plataforma podem consultar esses registros. Não envie segredos (senhas, tokens, chaves) nem dados que não possam ser vistos pelos administradores.

## Boas práticas

- A primeira requisição a um modelo "frio" pode levar dezenas de segundos. Configure timeout de cliente generoso (≥ 120 s) e prefira streaming.
- Respeite o `429`: use retry com backoff exponencial.
- Sua chave tem data de validade (pergunte ao administrador). Depois de expirar, a API responde `401`; peça a renovação antes do vencimento.
- Não compartilhe sua chave. Se vazar, peça a revogação.
- Os limites (RPM, paralelismo, validade) são definidos por chave. Peça ajuste ao administrador se precisar de mais.
