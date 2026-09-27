"""Configuração do painel, lida do ambiente (definido no docker-compose.yml a partir do .env)."""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _obrigatorio(nome: str) -> str:
    valor = os.environ.get(nome, "")
    if not valor:
        raise RuntimeError(f"Variável de ambiente {nome} não definida.")
    return valor


def _int(nome: str, padrao: int) -> int:
    return int(os.environ.get(nome) or padrao)


@dataclass(frozen=True)
class Config:
    dsn_app: str
    dsn_conteudo: str
    litellm_url: str
    litellm_master_key: str
    ollama_url: str
    fuso: str
    cookie_seguro: bool
    # Padrões das chaves novas (mesmas variáveis do scripts/create-key.sh).
    chave_modelos: str
    chave_rpm: int
    chave_paralelas: int
    chave_validade: str
    # Governança de usuários.
    sessao_inatividade_min: int = 30
    sessao_maxima_h: int = 8
    senha_temp_validade_h: int = 24
    senha_minimo: int = 12
    senha_historico: int = 5
    login_max_falhas: int = 5
    login_bloqueio_min: int = 15
    inatividade_desativa_dias: int = 90
    rotacao_carencia_dias: int = 7

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.fuso)


def carregar() -> Config:
    return Config(
        dsn_app=_obrigatorio("PAINEL_DSN_APP"),
        dsn_conteudo=_obrigatorio("PAINEL_DSN_CONTEUDO"),
        litellm_url=os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/"),
        litellm_master_key=_obrigatorio("LITELLM_MASTER_KEY"),
        ollama_url=os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/"),
        fuso=os.environ.get("PAINEL_FUSO") or "America/Sao_Paulo",
        cookie_seguro=os.environ.get("PAINEL_COOKIE_SEGURO", "1") != "0",
        chave_modelos=os.environ.get("KEY_DEFAULT_MODELS") or "",
        chave_rpm=_int("KEY_DEFAULT_RPM", 30),
        chave_paralelas=_int("KEY_DEFAULT_PARALLEL", 2),
        chave_validade=os.environ.get("KEY_DEFAULT_DURATION") or "90d",
        inatividade_desativa_dias=_int("PAINEL_INATIVIDADE_DIAS", 90),
        rotacao_carencia_dias=_int("PAINEL_ROTACAO_CARENCIA_DIAS", 7),
    )


config = carregar()
