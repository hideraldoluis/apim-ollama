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
| Chat: conversar com um modelo publicado (via master key, fora dos limites de uma chave) | ✅ | ✅ |
| Baixar, publicar, retirar, remover, carregar e descarregar modelos (inclui **Aquecer** no chat) | ❌ | ✅ |
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
3. **Liberar nas chaves.** Uma chave por vez, na página dela (**Alterar limites e validade**), ou várias de uma vez pela tela **Chaves do modelo** (abaixo).

### Liberar ou retirar um modelo em várias chaves

Na tela **Modelos**, o link **gerenciar** (coluna Chaves do modelo publicado) abre a lista de chaves com caixas de seleção. As que já têm o modelo vêm marcadas. Marque ou desmarque, use o filtro e os atalhos (marcar as visíveis, liberar em todas, retirar de todas, desfazer) e acompanhe o contador `+liberadas / −retiradas`. **Revisar alterações** mostra o antes e o depois de cada chave; só **Aplicar** grava.

- O estado das chaves é lido de novo ao aplicar, então o resultado é seguro mesmo que alguém tenha mexido nas chaves nesse meio tempo, e repetir a operação não causa dano.
- Uma chave que ficaria **sem nenhum modelo** não é alterada (aparece como "mantida" na revisão).
- Chaves **sem lista de modelos** (acesso a todos) e **expiradas** não aparecem na seleção.
- Se o gateway recusar algumas chaves, as demais são aplicadas e o resultado lista as que falharam.
- Cada chave alterada gera um evento `chave_alterada` (com `origem: massa`) na própria trilha, mais um evento-resumo.

**Retirar de todas as chaves e despublicar** (dentro de **Retirar**, quando o modelo está liberado em chaves) faz tudo num passo, com confirmação digitando o nome. Aqui, a chave cujo único modelo era esse passa a ficar **sem acesso** (ver abaixo), e, se alguma chave falhar, o modelo não é despublicado.

Pela linha de comando, no host (a simulação é o padrão; `--sim` aplica):

```bash
docker compose run --rm painel python -m app.cli modelo-chaves liberar qwen2.5 --chave time-a --chave time-b
docker compose run --rm painel python -m app.cli modelo-chaves retirar qwen2.5 --todas --sim
```

### Chaves expiradas

Uma rotina (a cada 15 minutos, mais o botão **Limpar chaves expiradas** na tela Chaves) tira os modelos das chaves **expiradas**. No LiteLLM, uma lista vazia significa "todos os modelos", então a chave não fica vazia: recebe o marcador `__sem-acesso__`, que não existe no gateway e recusa tudo. A tela mostra "sem modelos". Para reativar uma chave, estenda a validade **e escolha ao menos um modelo** (salvar sem modelo é recusado). Os modelos retirados ficam no evento `chave_modelos_removidos` da auditoria. Chaves **revogadas** não entram: elas já saíram do gateway, e o retrato delas em **Chaves revogadas** guarda os modelos que tinham.

**Carregar/Descarregar** aquece o modelo, para evitar a lentidão da primeira chamada, ou libera memória. O tempo que o modelo fica carregado sem uso segue `OLLAMA_KEEP_ALIVE`.

Proteções:
- não se retira do gateway um modelo liberado em alguma chave não expirada (retire-o antes, em massa, ou use "Retirar de todas as chaves e despublicar");
- não se remove do Ollama um modelo publicado ou com download em andamento;
- retirar ou remover exige digitar o nome;
- não há modelos fixos: o `config/litellm.yaml` não declara nenhum, e uma instalação nova começa sem modelos publicados. Enquanto nada for publicado, não é possível criar chaves.

Antes de publicar um modelo grande, meça memória e tokens/s e ajuste `OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS` e `global_max_parallel_requests` ([operacao.md](operacao.md#gerenciar-modelos)).

## Chat

A tela **Chat** deixa conversar com qualquer modelo de chat publicado, direto do navegador. Serve para
testar respostas e desempenho sem precisar de uma chave `sk-...`.

- A requisição sai com a **master key**, então não passa pelo RPM, paralelismo nem allowlist de nenhuma
  chave — vale para admin e leitor.
- A conversa não é guardada pelo painel: some ao recarregar a página ou trocar de tela.
- Cada mensagem, no entanto, é uma chamada real ao gateway: aparece no consumo do modelo e, para admins,
  no texto dos logs, como qualquer outra requisição.

O que a tela mostra:
- **Estado do modelo:** `carregado` ou `fora da memória`, além de parâmetros, quantização e contexto. Fora da
  memória, a primeira resposta inclui a carga do modelo, que leva minutos em modelos grandes (ex.: llama3.3 70B).
  Durante a espera, a bolha mostra a fase e um cronômetro. O botão **Aquecer** (só admin, auditado como
  "Modelo carregado na memória") carrega o modelo antes do teste.
- **Métricas por resposta:** tempo até o 1º token, tokens/s, tokens gerados, tempo total e se a resposta
  incluiu a carga do modelo ou foi cortada pelo limite de tokens.
- **Parâmetros:** prompt de sistema, temperatura (0–2), máximo de tokens e seed. Vazio usa o padrão do modelo.
- **Erros sempre visíveis:** recusa do gateway, erro no meio da resposta, resposta vazia e timeout (600 s,
  o mesmo do LiteLLM) aparecem na bolha, com **Tentar de novo**. **Parar** interrompe a geração no Ollama.
- **Copiar conversa:** copia a conversa em Markdown, com parâmetros e métricas, para anexar a uma avaliação.

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
| Teste da liberação em massa (sem serviços externos) | `python painel/tests/test_modelos_chaves.py` |
| Teste ponta a ponta (ambiente de teste) | `python painel/tests/e2e.py` e `python painel/tests/e2e_modelos.py` (instruções nos arquivos) |

O schema do painel fica no mesmo banco do LiteLLM e já entra no `pg_dump` descrito em [operacao.md](operacao.md#backup-e-restore).

**Compatibilidade com versões do LiteLLM.** O painel lê as tabelas do LiteLLM por funções `plpgsql`, e não por views. Assim, as migrações do LiteLLM continuam livres para alterar as tabelas. Se uma atualização renomear colunas usadas pelas funções (por exemplo `proxy_server_request`, `requester_ip_address` ou `metadata`), as telas do painel falham com erro de SQL e a API continua funcionando. Nesse caso, ajuste [painel/sql/schema.sql](../painel/sql/schema.sql) e reaplique com `docker compose up painel-init`.
