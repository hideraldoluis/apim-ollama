-- Schema do painel de governança. Idempotente: roda a cada subida (serviço painel-init), como superusuário.
-- Variáveis do psql: app_senha, conteudo_senha.
--
-- Separação de privilégios:
--   painel_app       conexão principal do painel. Lê SÓ metadados do LiteLLM (nunca prompt/resposta).
--   painel_conteudo  conexão usada apenas pela rota de conteúdo (admin). Só executa as funções de texto.
--   painel_leitor_litellm (NOLOGIN) dono das funções SECURITY DEFINER; tem SELECT nas tabelas do LiteLLM.
--
-- As leituras do LiteLLM são funções plpgsql (e não views): o corpo não cria dependência de coluna,
-- então as migrações do LiteLLM continuam livres para alterar as tabelas.

\set ON_ERROR_STOP 1
set client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
select 'create role painel_app login'
 where not exists (select 1 from pg_roles where rolname = 'painel_app') \gexec
select 'create role painel_conteudo login'
 where not exists (select 1 from pg_roles where rolname = 'painel_conteudo') \gexec
select 'create role painel_leitor_litellm nologin'
 where not exists (select 1 from pg_roles where rolname = 'painel_leitor_litellm') \gexec

select format('alter role painel_app password %L', :'app_senha') \gexec
select format('alter role painel_conteudo password %L', :'conteudo_senha') \gexec

-- Os roles do painel não enxergam nada do schema public (onde ficam as tabelas do LiteLLM).
revoke all on schema public from painel_app, painel_conteudo;
revoke all on all tables in schema public from painel_app, painel_conteudo;

-- O dono das funções lê somente as duas tabelas necessárias. Regrava a cada subida porque
-- uma migração do LiteLLM que recrie a tabela perde os grants.
grant usage on schema public to painel_leitor_litellm;
grant select on "LiteLLM_SpendLogs", "LiteLLM_VerificationToken" to painel_leitor_litellm;

create schema if not exists painel;
revoke all on schema painel from public;
grant usage on schema painel to painel_app, painel_conteudo;
grant create, usage on schema painel to painel_leitor_litellm;

-- ---------------------------------------------------------------------------
-- Índices de apoio na tabela de logs do LiteLLM (nomes próprios; não conflitam com as migrações)
-- ---------------------------------------------------------------------------
create index if not exists painel_spendlogs_inicio_idx on "LiteLLM_SpendLogs" ("startTime");
create index if not exists painel_spendlogs_chave_inicio_idx on "LiteLLM_SpendLogs" (api_key, "startTime");
create index if not exists painel_spendlogs_alias_inicio_idx
    on "LiteLLM_SpendLogs" ((metadata->>'user_api_key_alias'), "startTime");

-- ---------------------------------------------------------------------------
-- Tabelas do painel
-- ---------------------------------------------------------------------------
create table if not exists painel.usuarios (
    id                   bigserial primary key,
    login                text not null unique check (login ~ '^[a-z0-9._-]{3,64}$'),
    nome                 text not null,
    papel                text not null check (papel in ('admin', 'leitor')),
    ativo                boolean not null default true,
    senha_hash           text not null,
    deve_trocar_senha    boolean not null default true,
    senha_temp_expira_em timestamptz,
    senha_alterada_em    timestamptz not null default now(),
    falhas_login         int not null default 0,
    bloqueado_ate        timestamptz,
    ultimo_login         timestamptz,
    criado_em            timestamptz not null default now(),
    criado_por           text not null,
    desativado_em        timestamptz,
    desativado_por       text,
    motivo_desativacao   text,
    reativado_em         timestamptz
);

create table if not exists painel.senhas_historico (
    id         bigserial primary key,
    usuario_id bigint not null references painel.usuarios(id),
    senha_hash text not null,
    criado_em  timestamptz not null default now()
);
create index if not exists senhas_historico_usuario_idx on painel.senhas_historico (usuario_id, criado_em desc);

-- A sessão guarda só o hash do token do cookie.
create table if not exists painel.sessoes (
    token_hash text primary key,
    usuario_id bigint not null references painel.usuarios(id),
    csrf       text not null,
    criada_em  timestamptz not null default now(),
    ultimo_uso timestamptz not null default now(),
    ip         text
);
create index if not exists sessoes_usuario_idx on painel.sessoes (usuario_id);

-- Trilha de auditoria: só inserção (sem UPDATE/DELETE para o painel_app).
create table if not exists painel.auditoria (
    id        bigserial primary key,
    quando    timestamptz not null default now(),
    usuario   text,
    ip        text,
    acao      text not null,
    alvo      text,
    detalhes  jsonb not null default '{}'
);
create index if not exists auditoria_quando_idx on painel.auditoria (quando desc);
create index if not exists auditoria_acao_idx on painel.auditoria (acao, quando desc);

-- Retrato da chave antes da revogação (o LiteLLM apaga a linha da chave).
create table if not exists painel.chaves_historico (
    id           bigserial primary key,
    token        text not null,
    key_alias    text,
    criado_para  text,
    dados        jsonb not null,
    revogada_em  timestamptz not null default now(),
    revogada_por text not null,
    motivo       text not null
);
create index if not exists chaves_historico_token_idx on painel.chaves_historico (token);

-- Revogações agendadas (rotação): executadas pela rotina periódica do painel.
create table if not exists painel.revogacoes_agendadas (
    token       text primary key,
    key_alias   text,
    executar_em timestamptz not null,
    agendada_por text not null,
    motivo      text not null,
    executada_em timestamptz
);

-- Consumo consolidado por dia (sem texto). Sobrevive à retenção de 180 dias dos logs.
create table if not exists painel.consumo_diario (
    dia          date not null,
    chave        text not null,
    alias        text not null,
    modelo       text not null,
    requisicoes  bigint not null,
    erros        bigint not null,
    tok_prompt   bigint not null,
    tok_resposta bigint not null,
    tok_total    bigint not null,
    primary key (dia, chave, alias, modelo)
);

-- Último dia já consolidado em consumo_diario (linha única).
create table if not exists painel.consolidacao (
    id  int primary key default 1 check (id = 1),
    ate date not null
);

create table if not exists painel.revisoes_acesso (
    id          bigserial primary key,
    quando      timestamptz not null default now(),
    revisor     text not null,
    observacao  text not null,
    usuarios    jsonb not null
);

-- Downloads de modelos no Ollama iniciados pelo painel.
create table if not exists painel.downloads (
    id            bigserial primary key,
    modelo        text not null,
    status        text not null check (status in ('baixando', 'concluido', 'falhou', 'interrompido')),
    total         bigint,
    baixado       bigint,
    mensagem      text,
    iniciado_por  text not null,
    iniciado_em   timestamptz not null default now(),
    finalizado_em timestamptz
);
create index if not exists downloads_iniciado_idx on painel.downloads (iniciado_em desc);

-- ---------------------------------------------------------------------------
-- Funções de leitura do LiteLLM
-- Modelo: nome público (model_group). A coluna model guarda o nome interno (ex.: ollama_chat/llama3.2)
-- e só é usada quando model_group está vazio (requisições recusadas antes do roteamento). (SECURITY DEFINER, dono painel_leitor_litellm)
-- "startTime" é timestamp sem fuso, em UTC.
-- ---------------------------------------------------------------------------

-- Chaves existentes, sem o segredo (token é o hash).
create or replace function painel.chaves()
returns table (
    token text, key_alias text, key_name text, criado_para text, modelos text[],
    rpm_limit bigint, max_parallel_requests bigint, expira_em timestamptz, bloqueada boolean,
    criada_em timestamptz, ultimo_uso timestamptz
)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
begin
    return query
    select k.token::text,
           k.key_alias::text,
           k.key_name::text,
           (k.metadata->>'created_for')::text,
           k.models::text[],
           k.rpm_limit::bigint,
           k.max_parallel_requests::bigint,
           (k.expires at time zone 'UTC')::timestamptz,
           coalesce(k.blocked, false),
           coalesce(((to_jsonb(k)->>'created_at')::timestamp at time zone 'UTC'),
                    (k.metadata->>'created_at')::timestamptz),
           (select max(s."startTime") at time zone 'UTC'
              from "LiteLLM_SpendLogs" s where s.api_key = k.token)
      from "LiteLLM_VerificationToken" k
     order by k.key_alias nulls last;
end $$;

-- Linha completa da chave (sem segredo) para o retrato antes de revogar.
create or replace function painel.chave_retrato(p_token text)
returns jsonb
language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare r jsonb;
begin
    select to_jsonb(k) into r from "LiteLLM_VerificationToken" k where k.token = p_token;
    return r;
end $$;

-- Logs SEM prompt e SEM resposta. Paginação por cursor (quando, id) decrescente.
create or replace function painel.logs(
    p_desde timestamptz, p_ate timestamptz, p_alias text, p_chave text, p_modelo text, p_status text,
    p_cursor_quando timestamptz, p_cursor_id text, p_limite int
)
returns table (
    id text, quando timestamptz, alias text, chave text, modelo text, status text, codigo_erro text,
    tok_prompt bigint, tok_resposta bigint, tok_total bigint, duracao_ms bigint, ip text
)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare
    v_desde timestamp := p_desde at time zone 'UTC';
    v_ate   timestamp := p_ate at time zone 'UTC';
    v_cq    timestamp := p_cursor_quando at time zone 'UTC';
begin
    return query
    select s.request_id::text,
           s."startTime" at time zone 'UTC',
           (s.metadata->>'user_api_key_alias')::text,
           s.api_key::text,
           coalesce(nullif(s.model_group, ''), s.model)::text,
           s.status::text,
           (s.metadata->'error_information'->>'error_code')::text,
           s.prompt_tokens::bigint,
           s.completion_tokens::bigint,
           s.total_tokens::bigint,
           (extract(epoch from (s."endTime" - s."startTime")) * 1000)::bigint,
           s.requester_ip_address::text
      from "LiteLLM_SpendLogs" s
     where s."startTime" >= v_desde and s."startTime" < v_ate
       and (p_alias  is null or s.metadata->>'user_api_key_alias' = p_alias)
       and (p_chave  is null or s.api_key = p_chave)
       and (p_modelo is null or coalesce(nullif(s.model_group, ''), s.model) = p_modelo)
       and (p_status is null or s.status = p_status)
       and (v_cq is null or (s."startTime", s.request_id) < (v_cq, p_cursor_id))
     order by s."startTime" desc, s.request_id desc
     limit least(greatest(p_limite, 1), 1000);
end $$;

-- Um registro, sem texto.
create or replace function painel.log(p_id text)
returns table (
    id text, quando timestamptz, alias text, chave text, modelo text, status text, codigo_erro text,
    tok_prompt bigint, tok_resposta bigint, tok_total bigint, duracao_ms bigint, ip text,
    criado_para text, mensagem_erro text
)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
begin
    return query
    select s.request_id::text,
           s."startTime" at time zone 'UTC',
           (s.metadata->>'user_api_key_alias')::text,
           s.api_key::text,
           coalesce(nullif(s.model_group, ''), s.model)::text,
           s.status::text,
           (s.metadata->'error_information'->>'error_code')::text,
           s.prompt_tokens::bigint,
           s.completion_tokens::bigint,
           s.total_tokens::bigint,
           (extract(epoch from (s."endTime" - s."startTime")) * 1000)::bigint,
           s.requester_ip_address::text,
           (k.metadata->>'created_for')::text,
           -- Classe do erro (ex.: RateLimitError), sem a mensagem, que pode ecoar o prompt.
           (s.metadata->'error_information'->>'error_class')::text
      from "LiteLLM_SpendLogs" s
      left join "LiteLLM_VerificationToken" k on k.token = s.api_key
     where s.request_id = p_id;
end $$;

-- Agregado por dia/chave/modelo, no fuso informado. Base da consolidação e do consumo do dia corrente.
create or replace function painel.agregado_diario(p_de date, p_ate date, p_fuso text)
returns table (
    dia date, chave text, alias text, modelo text, requisicoes bigint, erros bigint,
    tok_prompt bigint, tok_resposta bigint, tok_total bigint
)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare
    v_de  timestamp := (p_de::timestamp at time zone p_fuso) at time zone 'UTC';
    v_ate timestamp := ((p_ate + 1)::timestamp at time zone p_fuso) at time zone 'UTC';
begin
    return query
    select ((s."startTime" at time zone 'UTC') at time zone p_fuso)::date,
           coalesce(s.api_key, '')::text,
           coalesce(s.metadata->>'user_api_key_alias', '')::text,
           coalesce(coalesce(nullif(s.model_group, ''), s.model), '')::text,
           count(*)::bigint,
           count(*) filter (where s.status is distinct from 'success')::bigint,
           coalesce(sum(s.prompt_tokens), 0)::bigint,
           coalesce(sum(s.completion_tokens), 0)::bigint,
           coalesce(sum(s.total_tokens), 0)::bigint
      from "LiteLLM_SpendLogs" s
     where s."startTime" >= v_de and s."startTime" < v_ate
     group by 1, 2, 3, 4;
end $$;

-- Latência e erros por modelo no período.
create or replace function painel.resumo_modelos(p_desde timestamptz, p_ate timestamptz)
returns table (modelo text, requisicoes bigint, erros bigint, p50_ms bigint, p95_ms bigint)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare
    v_desde timestamp := p_desde at time zone 'UTC';
    v_ate   timestamp := p_ate at time zone 'UTC';
begin
    return query
    select coalesce(coalesce(nullif(s.model_group, ''), s.model), '')::text,
           count(*)::bigint,
           count(*) filter (where s.status is distinct from 'success')::bigint,
           (percentile_cont(0.5) within group (order by extract(epoch from (s."endTime" - s."startTime")) * 1000))::bigint,
           (percentile_cont(0.95) within group (order by extract(epoch from (s."endTime" - s."startTime")) * 1000))::bigint
      from "LiteLLM_SpendLogs" s
     where s."startTime" >= v_desde and s."startTime" < v_ate
     group by 1
     order by 2 desc;
end $$;

-- Situação da retenção.
create or replace function painel.retencao()
returns table (mais_antigo timestamptz, mais_recente timestamptz, registros_estimados bigint, tamanho text)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
begin
    return query
    select (select min(s."startTime") from "LiteLLM_SpendLogs" s) at time zone 'UTC',
           (select max(s."startTime") from "LiteLLM_SpendLogs" s) at time zone 'UTC',
           (select c.reltuples::bigint from pg_class c where c.oid = '"LiteLLM_SpendLogs"'::regclass),
           pg_size_pretty(pg_total_relation_size('"LiteLLM_SpendLogs"'::regclass));
end $$;

-- ---------------------------------------------------------------------------
-- Funções de CONTEÚDO (prompt e resposta): executáveis só por painel_conteudo
-- ---------------------------------------------------------------------------
create or replace function painel.log_conteudo(p_id text)
returns table (prompt jsonb, resposta jsonb)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
begin
    return query
    select s.proxy_server_request->'messages', s.response::jsonb
      from "LiteLLM_SpendLogs" s
     where s.request_id = p_id;
end $$;

create or replace function painel.logs_conteudo(
    p_desde timestamptz, p_ate timestamptz, p_alias text, p_chave text, p_modelo text, p_status text, p_limite int
)
returns table (id text, prompt jsonb, resposta jsonb)
language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare
    v_desde timestamp := p_desde at time zone 'UTC';
    v_ate   timestamp := p_ate at time zone 'UTC';
begin
    return query
    select s.request_id::text, s.proxy_server_request->'messages', s.response::jsonb
      from "LiteLLM_SpendLogs" s
     where s."startTime" >= v_desde and s."startTime" < v_ate
       and (p_alias  is null or s.metadata->>'user_api_key_alias' = p_alias)
       and (p_chave  is null or s.api_key = p_chave)
       and (p_modelo is null or coalesce(nullif(s.model_group, ''), s.model) = p_modelo)
       and (p_status is null or s.status = p_status)
     order by s."startTime" desc, s.request_id desc
     limit least(greatest(p_limite, 1), 50000);
end $$;

-- ---------------------------------------------------------------------------
-- Donos e permissões
-- ---------------------------------------------------------------------------
alter function painel.chaves()                                                          owner to painel_leitor_litellm;
alter function painel.chave_retrato(text)                                               owner to painel_leitor_litellm;
alter function painel.logs(timestamptz, timestamptz, text, text, text, text, timestamptz, text, int) owner to painel_leitor_litellm;
alter function painel.log(text)                                                         owner to painel_leitor_litellm;
alter function painel.agregado_diario(date, date, text)                                 owner to painel_leitor_litellm;
alter function painel.resumo_modelos(timestamptz, timestamptz)                          owner to painel_leitor_litellm;
alter function painel.retencao()                                                        owner to painel_leitor_litellm;
alter function painel.log_conteudo(text)                                                owner to painel_leitor_litellm;
alter function painel.logs_conteudo(timestamptz, timestamptz, text, text, text, text, int) owner to painel_leitor_litellm;

revoke all on all functions in schema painel from public;

grant execute on function
    painel.chaves(),
    painel.chave_retrato(text),
    painel.logs(timestamptz, timestamptz, text, text, text, text, timestamptz, text, int),
    painel.log(text),
    painel.agregado_diario(date, date, text),
    painel.resumo_modelos(timestamptz, timestamptz),
    painel.retencao()
  to painel_app;

grant execute on function
    painel.log_conteudo(text),
    painel.logs_conteudo(timestamptz, timestamptz, text, text, text, text, int)
  to painel_conteudo;

-- Tabelas próprias: o painel_app administra usuários e sessões; auditoria e histórico só crescem.
revoke all on all tables in schema painel from painel_app, painel_conteudo;
grant select, insert, update on painel.usuarios, painel.revogacoes_agendadas, painel.consumo_diario,
                                painel.consolidacao, painel.downloads to painel_app;
grant select, insert, delete on painel.sessoes, painel.senhas_historico to painel_app;
grant update on painel.sessoes to painel_app;
grant select, insert on painel.auditoria, painel.chaves_historico, painel.revisoes_acesso to painel_app;
grant usage on all sequences in schema painel to painel_app;
