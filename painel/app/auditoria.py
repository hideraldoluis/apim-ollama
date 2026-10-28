"""Trilha de auditoria do painel (tabela painel.auditoria, somente inserção)."""
from psycopg.types.json import Jsonb

from . import db


def registrar(acao: str, usuario: str | None, ip: str | None, alvo: str | None = None,
              detalhes: dict | None = None, conn=None) -> None:
    """Grava um evento. Com `conn`, participa da transação do chamador."""
    sql = "insert into painel.auditoria (usuario, ip, acao, alvo, detalhes) values (%s, %s, %s, %s, %s)"
    params = (usuario, ip, acao, alvo, Jsonb(detalhes or {}))
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.executar(sql, params)


# Rótulos exibidos na tela de auditoria.
ACOES = {
    "login": "Login",
    "login_falha": "Falha de login",
    "login_bloqueio": "Bloqueio por tentativas",
    "logout": "Logout",
    "senha_trocada": "Troca de senha",
    "usuario_criado": "Usuário criado",
    "usuario_papel": "Papel alterado",
    "usuario_senha_redefinida": "Senha redefinida",
    "usuario_desbloqueado": "Usuário desbloqueado",
    "usuario_desativado": "Usuário desativado",
    "usuario_reativado": "Usuário reativado",
    "revisao_acesso": "Revisão de acessos",
    "chave_criada": "Chave criada",
    "chave_alterada": "Chave alterada",
    "chave_bloqueada": "Chave bloqueada",
    "chave_desbloqueada": "Chave desbloqueada",
    "chave_revogada": "Chave revogada",
    "chave_rotacionada": "Chave rotacionada",
    "chave_modelos_removidos": "Modelos removidos de chave expirada",
    "modelo_chaves_ajustadas": "Modelo liberado/retirado em várias chaves",
    "chave_revogacao_agendada_falhou": "Falha na revogação agendada",
    "modelo_download_iniciado": "Download de modelo iniciado",
    "modelo_download_concluido": "Download de modelo concluído",
    "modelo_download_falhou": "Download de modelo falhou",
    "modelo_publicado": "Modelo publicado no gateway",
    "modelo_despublicado": "Modelo retirado do gateway",
    "modelo_removido": "Modelo removido do Ollama",
    "modelo_carregado": "Modelo carregado na memória",
    "modelo_descarregado": "Modelo descarregado da memória",
    "log_conteudo_visto": "Conteúdo de log visualizado",
    "exportacao": "Exportação",
    "cli_admin": "Operação via CLI",
}
