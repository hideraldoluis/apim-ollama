"""Concierge: chat em linguagem natural na home que executa as operações do painel.

  configuracao  modelo e limites, guardados no banco (tela de admin)
  ferramentas   catálogo de operações (leitura executa na hora; escrita só vira proposta)
  acoes         propostas pendentes de confirmação humana (painel.concierge_acoes)
  agente        laço modelo -> ferramentas -> modelo, emitindo eventos SSE
  limites       freio por usuário e global (o concierge usa a master key, fora dos limites de chave)
"""
