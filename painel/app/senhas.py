"""Hash (argon2id) e política de senha do painel."""
import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import db
from .config import config

_hasher = PasswordHasher()

# Hash de referência para gastar o mesmo tempo quando o login não existe.
_HASH_FALSO = _hasher.hash(secrets.token_urlsafe(16))

# Senhas comuns recusadas (comparação sem diferenciar maiúsculas). Complementa o tamanho mínimo.
_COMUNS = {
    "123456789012", "1234567890123", "12345678901234", "qwertyuiopas", "qwertyuiop123",
    "password1234", "password12345", "passw0rd1234", "senha1234567", "senha12345678",
    "senhasenha12", "mudar1234567", "mudar@123456", "trocar123456", "admin1234567",
    "administrador", "administrator", "iloveyou1234", "welcome12345", "bemvindo1234",
    "abcdefghijkl", "abc123456789", "aaaaaaaaaaaa", "000000000000", "111111111111",
    "letmein12345", "football1234", "brasil123456", "brasil@12345", "gn100gn100gn",
    "ollamaollama", "litellm12345", "painel123456", "changeme1234", "q1w2e3r4t5y6",
}


def gerar_hash(senha: str) -> str:
    return _hasher.hash(senha)


def conferir(hash_: str | None, senha: str) -> bool:
    try:
        return _hasher.verify(hash_ or _HASH_FALSO, senha) and hash_ is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def gerar_temporaria() -> str:
    """Senha temporária de 16 caracteres sem símbolos ambíguos."""
    alfabeto = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    return "".join(secrets.choice(alfabeto) for _ in range(16))


def problemas(senha: str, login: str, nome: str, usuario_id: int | None = None) -> list[str]:
    """Lista de violações da política (vazia = senha aceita)."""
    erros = []
    if len(senha) < config.senha_minimo:
        erros.append(f"A senha deve ter pelo menos {config.senha_minimo} caracteres.")
    if len(senha) > 256:
        erros.append("A senha deve ter no máximo 256 caracteres.")
    baixa = senha.lower()
    if baixa in _COMUNS or len(set(baixa)) < 4:
        erros.append("Senha muito comum ou repetitiva.")
    partes = [login.lower()] + [p.lower() for p in nome.split() if len(p) >= 3]
    if any(p and p in baixa for p in partes):
        erros.append("A senha não pode conter o login nem partes do nome.")
    if usuario_id is not None:
        anteriores = db.consultar(
            "select senha_hash from painel.senhas_historico where usuario_id = %s "
            "order by criado_em desc limit %s",
            (usuario_id, config.senha_historico),
        )
        if any(conferir(r["senha_hash"], senha) for r in anteriores):
            erros.append(f"A senha não pode repetir nenhuma das {config.senha_historico} últimas.")
    return erros
