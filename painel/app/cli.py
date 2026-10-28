"""Operações de bootstrap e recuperação, executadas no host.

  docker compose run --rm painel python -m app.cli criar-admin <login> "<Nome>"
  docker compose run --rm painel python -m app.cli recuperar <login>
  docker compose run --rm painel python -m app.cli modelo-chaves <liberar|retirar> <modelo> (--todas | --chave ALIAS ...) [--sim]

Ambas geram senha temporária (troca obrigatória no primeiro login) e ficam na auditoria como "cli".
"""
import argparse
import sys

from psycopg.errors import UniqueViolation

from . import auditoria, consultas, db, litellm, modelos_chaves, senhas, validacao
from .config import config


def criar_admin(login: str, nome: str) -> None:
    login, nome = validacao.login(login), validacao.nome(nome)
    temporaria = senhas.gerar_temporaria()
    try:
        with db.transacao() as conn:
            conn.execute(
                "insert into painel.usuarios (login, nome, papel, senha_hash, deve_trocar_senha, "
                "senha_temp_expira_em, criado_por) values (%s, %s, 'admin', %s, true, "
                "now() + make_interval(hours => %s), 'cli')",
                (login, nome, senhas.gerar_hash(temporaria), config.senha_temp_validade_h))
            auditoria.registrar("cli_admin", "cli", None, login, {"operacao": "criar-admin"}, conn)
    except UniqueViolation:
        sys.exit(f"Já existe um usuário '{login}'. Para recuperar o acesso dele use: recuperar {login}")
    _imprimir(login, temporaria)


def recuperar(login: str) -> None:
    """Reativa, desbloqueia e gera senha temporária. Se não houver admin ativo, promove o usuário a admin."""
    login = validacao.login(login)
    temporaria = senhas.gerar_temporaria()
    with db.transacao() as conn:
        u = conn.execute("select id, papel from painel.usuarios where login = %s for update", (login,)).fetchone()
        if u is None:
            sys.exit(f"Usuário '{login}' não existe. Para criar um admin use: criar-admin {login} \"Nome\"")
        tem_admin = conn.execute(
            "select exists(select 1 from painel.usuarios where papel = 'admin' and ativo and id <> %s) as t",
            (u["id"],)).fetchone()["t"]
        papel = u["papel"] if tem_admin else "admin"
        conn.execute(
            "update painel.usuarios set ativo = true, papel = %s, reativado_em = now(), desativado_em = null, "
            "desativado_por = null, motivo_desativacao = null, senha_hash = %s, deve_trocar_senha = true, "
            "senha_temp_expira_em = now() + make_interval(hours => %s), falhas_login = 0, bloqueado_ate = null "
            "where id = %s",
            (papel, senhas.gerar_hash(temporaria), config.senha_temp_validade_h, u["id"]))
        conn.execute("delete from painel.sessoes where usuario_id = %s", (u["id"],))
        auditoria.registrar("cli_admin", "cli", None, login,
                            {"operacao": "recuperar", "papel": papel, "promovido": papel != u["papel"]}, conn)
    _imprimir(login, temporaria)


def modelo_chaves(acao: str, modelo: str, todas: bool, aliases: list[str], confirmar: bool) -> None:
    """Libera ou retira um modelo em várias chaves. Sem --sim só mostra o que seria feito."""
    modelo = validacao.modelo_publico(modelo)
    if modelo not in litellm.modelos_publicados():
        raise validacao.Invalido(f"{modelo} não está publicado no gateway.")
    chaves = consultas.chaves()
    alvos = {k["token"] for k in modelos_chaves.elegiveis(chaves) if todas or k["key_alias"] in aliases}
    desconhecidas = set(aliases) - {k["key_alias"] for k in chaves}
    if desconhecidas:
        raise validacao.Invalido(f"Chaves inexistentes: {', '.join(sorted(desconhecidas))}.")
    # A seleção final parte do estado atual: liberar acrescenta os alvos, retirar os tira.
    atuais = {k["token"] for k in modelos_chaves.elegiveis(chaves) if modelo in k["modelos_reais"]}
    marcadas = (atuais | alvos) if acao == "liberar" else (atuais - alvos)
    plano = modelos_chaves.planejar(modelo, chaves, marcadas)
    for rotulo, itens in (("liberar em", plano.adicionar), ("retirar de", plano.retirar),
                          ("mantida (ficaria sem modelo)", plano.bloqueadas)):
        for i in itens:
            print(f"{rotulo}: {i['chave']['key_alias']}")
    if not plano.alteracoes:
        print("Nada a fazer.")
        return
    if not confirmar:
        print("Simulação. Repita com --sim para aplicar.")
        return
    res = modelos_chaves.executar(plano, "cli", None, "cli")
    print(modelos_chaves.resumo(plano, res))
    if res.falhas:
        sys.exit(1)


def _imprimir(login: str, temporaria: str) -> None:
    print(f"Usuário: {login}")
    print(f"Senha temporária: {temporaria}")
    print(f"Válida por {config.senha_temp_validade_h} h. A troca é obrigatória no primeiro login.")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="comando", required=True)
    c = sub.add_parser("criar-admin", help="cria um administrador")
    c.add_argument("login")
    c.add_argument("nome")
    r = sub.add_parser("recuperar", help="reativa, desbloqueia e gera senha temporária")
    r.add_argument("login")
    m = sub.add_parser("modelo-chaves", help="libera ou retira um modelo em várias chaves")
    m.add_argument("acao", choices=("liberar", "retirar"))
    m.add_argument("modelo")
    m.add_argument("--todas", action="store_true", help="todas as chaves não expiradas com lista de modelos")
    m.add_argument("--chave", action="append", default=[], metavar="ALIAS", help="alias da chave (repetível)")
    m.add_argument("--sim", action="store_true", help="aplica (sem isto, apenas simula)")
    args = p.parse_args()
    if args.comando == "modelo-chaves" and not (args.todas or args.chave):
        p.error("modelo-chaves exige --todas ou ao menos um --chave")

    db.abrir()
    try:
        if args.comando == "criar-admin":
            criar_admin(args.login, args.nome)
        elif args.comando == "modelo-chaves":
            modelo_chaves(args.acao, args.modelo, args.todas, args.chave, args.sim)
        else:
            recuperar(args.login)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        sys.exit(str(e))
    finally:
        db.fechar()


if __name__ == "__main__":
    main()
