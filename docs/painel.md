# Painel de governança

Interface web para governar as chaves de consumidores e os logs de uso. Endereço: `https://<SITE_ADDRESS>:8443`.

```
Rede de gestão (PAINEL_CIDR) ──HTTPS :8443──► Caddy ──► painel:8080 ──► LiteLLM (mutações de chave, master key)
Emergência (no host)        ──túnel SSH────► 127.0.0.1:8080            └► Postgres (leitura por funções, schema "painel")
```

## Papéis

| Capacidade | Leitor | Admin |
|---|:-:|:-:|
| Visão geral (requisições, erros, latência por modelo) | ✅ | ✅ |
| Consumo de tokens por chave (período, CSV) | ✅ | ✅ |
| Lista e detalhe de chaves (limites, expiração, último uso) | ✅ | ✅ |
| Logs sem texto (filtros, CSV de metadados) | ✅ | ✅ |
| Modelos: instalados, publicados, memória, uso e chaves que os liberam | ✅ | ✅ |
| Baixar, publicar, retirar, remover, carregar e descarregar modelos | ❌ | ✅ |
| Texto de prompts e respostas (por requisição ou exportação com justificativa) | ❌ | ✅ auditado |
| Criar, alterar, bloquear, rotacionar e revogar chaves | ❌ | ✅ |
| Usuários, revisão de acessos, auditoria, retenção | ❌ | ✅ |

**Texto dos logs.** A regra vale no próprio banco, não só na tela. O painel usa dois roles:
- `painel_app`, o role de todas as telas, que não tem permissão para ler prompt e resposta;
- `painel_conteudo`, usado apenas pelas duas rotas de conteúdo, que exigem admin e gravam a leitura na auditoria **antes** de devolver o texto.

## Primeiro acesso

1. Preencha no `.env` as variáveis `PAINEL_CIDR`, `PAINEL_DB_APP_PASSWORD` e `PAINEL_DB_CONTEUDO_PASSWORD` (veja o [.env.example](../.env.example)).
2. Suba a stack:
   ```bash
   docker compose up -d --build
   ```
   O serviço `painel-init` cria ou atualiza o schema e termina.
3. Crie o primeiro administrador. O comando imprime uma senha temporária, válida por 24 h:
   ```bash
   docker compose run --rm painel python -m app.cli criar-admin <login> "<Nome completo>"
   ```
4. Acesse `https://<SITE_ADDRESS>:8443`, entre com a senha temporária e defina a sua.

Os demais usuários são criados pela tela **Usuários**.

## Acesso de rede

- A porta **8443** é atendida pelo Caddy, que só encaminha clientes em `PAINEL_CIDR` (uma ou mais redes separadas por espaço). Os demais recebem `403`. Com `PAINEL_CIDR` vazio, ninguém entra pela rede.
- Restrinja a 8443 também no firewall do host, só para o mesmo CIDR (veja [rede-e-tls.md](rede-e-tls.md)).
- O certificado é o mesmo da API (CA interna do Caddy ou corporativa). Não é preciso DNS novo.
- **Emergência** (Caddy fora do ar): o painel escuta em `127.0.0.1:8080` no host.
  ```bash
  ssh -L 8080:127.0.0.1:8080 usuario@gn100
  ```
  Depois abra `http://localhost:8080` no seu navegador.
- A API pública (`:443`) continua sem nenhuma rota administrativa.

## Governança de usuários

| Regra | Valor |
|---|---|
| Cadastro | Só um admin cria usuários. Não há autocadastro. |
| Senha inicial | Temporária, gerada pelo sistema, válida por 24 h, com troca obrigatória no primeiro login |
| Política de senha | Mínimo de 12 caracteres, sem senhas comuns, sem conter login ou nome, sem repetir as 5 últimas; hash argon2id |
| Tentativas de login | 5 falhas seguidas bloqueiam a conta por 15 min (o admin pode desbloquear antes); 20 falhas por IP em 15 min bloqueiam o IP |
| Sessão | Expira após 30 min sem uso e após 8 h no total |
| Inatividade | Conta sem login há `PAINEL_INATIVIDADE_DIAS` (padrão 90) é desativada automaticamente |
| Exclusão | Não existe. Usuários são **desativados** e o login não pode ser reutilizado. |
| Reativação | Sempre gera nova senha temporária |
| Último admin | O sistema recusa rebaixar ou desativar o último admin ativo, inclusive pela rotina de inatividade |
| Auto-alteração | Ninguém muda o próprio papel, se desativa ou redefine a própria senha pela tela de usuários |
| Encerramento de sessões | Mudança de papel, redefinição de senha e desativação derrubam as sessões do usuário |
| Revisão de acessos | Tela **Usuários → Revisão de acessos**. Faça uma vez por trimestre e registre o resultado (fica na auditoria). |

### Recuperação

Use quando alguém perdeu a senha, está bloqueado ou foi desativado, inclusive se ninguém mais consegue entrar:

```bash
docker compose run --rm painel python -m app.cli recuperar <login>
```

O comando reativa e desbloqueia o usuário e imprime uma nova senha temporária. Se não existir outro admin ativo, o usuário é promovido a admin. A operação fica na auditoria como `cli`.

## Chaves

- **Criar.** Os padrões vêm de `KEY_DEFAULT_*`. O segredo `sk-...` aparece **uma única vez** e não é gravado. O painel identifica a chave pelo hash e mostra só o final dela.
- **Alterar.** Modelos, RPM, requisições simultâneas e validade. A nova validade conta a partir de agora; `never` remove a expiração.
- **Bloquear.** Suspensão reversível, com motivo.
- **Rotacionar.** Cria uma chave nova com o mesmo nome e os mesmos limites. A antiga recebe o sufixo `(substituída …)` e é revogada depois da carência (padrão `PAINEL_ROTACAO_CARENCIA_DIAS`; 0 revoga na hora).
- **Revogar.** Pede motivo e a digitação do nome da chave. Antes de apagar no gateway, o painel guarda um retrato da chave em **Chaves revogadas**.

Os scripts de `scripts/` continuam funcionando como alternativa. O que eles fizerem não aparece na auditoria do painel, e uma chave revogada por script não entra no histórico.

## Modelos

A tela **Modelos** junta três visões de cada modelo:
- o que está **instalado no Ollama**: tamanho, parâmetros, quantização, contexto e capacidades;
- o que está **publicado no gateway**: o nome público que os consumidores usam em `"model"`;
- o que está **carregado na memória**: quanto ocupa e que parte está na GPU.

A mesma tela mostra também o uso de cada modelo nos últimos 30 dias e quantas chaves o liberam.

Para um consumidor usar um modelo, são necessárias três etapas:

1. **Baixar.** Informe o nome do catálogo (`qwen2.5:7b`, `nomic-embed-text`...). O download roda em segundo plano, e o progresso aparece na tela e é atualizado sozinho. Baixar de novo um modelo já instalado o atualiza. Se o painel reiniciar no meio, o download fica como "interrompido"; ao pedir de novo, o Ollama retoma de onde parou.
2. **Publicar.** Escolha o nome público. O tipo (chat ou embedding) é detectado pelas capacidades do modelo. A publicação é gravada no banco do gateway e vale na hora, sem reinício.
3. **Liberar nas chaves.** Na página de cada chave, em **Alterar limites e validade**.

**Carregar/Descarregar** aquece o modelo, para evitar a lentidão da primeira chamada, ou libera memória. O tempo que o modelo fica carregado sem uso segue `OLLAMA_KEEP_ALIVE`.

Proteções:
- não se retira do gateway um modelo liberado em alguma chave (retire-o das chaves antes);
- não se remove do Ollama um modelo publicado ou com download em andamento;
- retirar ou remover exige digitar o nome;
- não há modelos fixos: o `config/litellm.yaml` não declara nenhum, e uma instalação nova começa sem modelos publicados. Enquanto nada for publicado, não é possível criar chaves.

Antes de publicar um modelo grande, meça memória e tokens/s e ajuste `OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS` e `global_max_parallel_requests` ([operacao.md](operacao.md#gerenciar-modelos)).

## Logs, consumo e retenção

- **Consumo.** Uma rotina a cada 15 minutos consolida os totais diários (chave × modelo) em `painel.consumo_diario`. O dia corrente é somado na hora. Os totais **não são apagados**, então o consumo histórico sobrevive à retenção dos logs.
- **Retenção.** O LiteLLM apaga logs com mais de **180 dias** (`maximum_spend_logs_retention_period` em [config/litellm.yaml](../config/litellm.yaml)). A tela **Retenção** mostra o registro mais antigo e avisa se a limpeza não estiver ocorrendo.
- **Backups.** Os dumps do Postgres também contêm texto de prompts. Para que a retenção valha de fato, não guarde dumps por mais de 180 dias.
- **Auditoria.** A tabela `painel.auditoria` só aceita inserção pelo painel. Ela registra:
  - login, falha de login, bloqueio e logout;
  - troca e redefinição de senha;
  - gestão de usuários e de chaves;
  - cada leitura de conteúdo;
  - exportações;
  - revisões de acesso.

## Operação

| Tarefa | Comando |
|---|---|
| Logs do painel | `docker compose logs -f painel` |
| Reaplicar o schema | `docker compose up painel-init` (idempotente) |
| Atualizar o painel após mudar o código | `docker compose up -d --build painel` |
| Teste ponta a ponta (ambiente de teste) | `python painel/tests/e2e.py` e `python painel/tests/e2e_modelos.py` (instruções nos arquivos) |

O schema do painel fica no mesmo banco do LiteLLM e já entra no `pg_dump` descrito em [operacao.md](operacao.md#backup-e-restore).

**Compatibilidade com versões do LiteLLM.** O painel lê as tabelas do LiteLLM por funções `plpgsql`, e não por views. Assim, as migrações do LiteLLM continuam livres para alterar as tabelas. Se uma atualização renomear colunas usadas pelas funções (por exemplo `proxy_server_request`, `requester_ip_address` ou `metadata`), as telas do painel falham com erro de SQL e a API continua funcionando. Nesse caso, ajuste [painel/sql/schema.sql](../painel/sql/schema.sql) e reaplique com `docker compose up painel-init`.
