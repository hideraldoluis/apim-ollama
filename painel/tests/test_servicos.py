"""Teste dos serviços de chaves (app/servicos/chaves.py) com gateway e banco simulados.

Garante que as regras que antes ficavam nas rotas continuam valendo (e valem para o concierge): alterar só o que
mudou, validações, confirmação por nome na revogação, auditoria com origem e rotação.
  python painel/tests/test_servicos.py      (a partir da raiz do repositório; precisa de painel/requirements.txt)
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

for nome, valor in (("PAINEL_DSN_APP", "postgresql://x@localhost/x"), ("PAINEL_DSN_CONTEUDO", "postgresql://x@localhost/x"),
                    ("LITELLM_MASTER_KEY", "sk-teste")):
    os.environ.setdefault(nome, valor)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auditoria, consultas, db, litellm, rotinas, validacao  # noqa: E402
from app.servicos import NaoEncontrado, chaves as s  # noqa: E402

falhas = 0


def ok(cond, msg):
    global falhas
    print(("OK    " if cond else "FALHA ") + msg)
    falhas += 0 if cond else 1


def levanta(excecao, funcao, *a, **kw):
    try:
        funcao(*a, **kw)
    except excecao:
        return True
    return False


ATOR = {"id": 1, "login": "ana", "papel": "admin"}
K = {"token": "a" * 64, "key_alias": "time-a", "key_name": "sk-..", "criado_para": "time-a", "modelos": ["m1"],
     "modelos_reais": ["m1"], "sem_acesso": False, "rpm_limit": 10, "max_parallel_requests": 2, "expira_em": None,
     "bloqueada": False, "criada_em": datetime.now(timezone.utc), "ultimo_uso": None, "status": "ativa"}
consultas.chaves = lambda: [K]
litellm.modelos_publicados = lambda: ["m1", "m2"]

trilha, gateway = [], []
auditoria.registrar = lambda acao, usuario, ip, alvo=None, detalhes=None, conn=None: trilha.append((acao, alvo, detalhes))
litellm.alterar_chave = lambda token, **kw: gateway.append(("alterar", token, kw))
litellm.bloquear = lambda token: gateway.append(("bloquear", token))
litellm.desbloquear = lambda token: gateway.append(("desbloquear", token))
litellm.gerar_chave = lambda *a: (gateway.append(("gerar", a)) or ("sk-novo", "b" * 64))
db.executar = lambda *a, **k: None
rotinas.revogar_com_historico = lambda token, por, ip, motivo: gateway.append(("revogar", token, motivo)) or True

# obter
ok(levanta(NaoEncontrado, s.obter, "z" * 64), "chave inexistente: NaoEncontrado")

# alterar: só o que mudou, None mantém
_, m = s.alterar(ATOR, "ip", K["token"], rpm=10, paralelas=2)
ok(m == {} and not gateway, "nada mudou: não chama o gateway nem audita")
_, m = s.alterar(ATOR, "ip", K["token"], modelos=["m1", "m2"], rpm="20", origem="concierge")
ok(m == {"modelos": ["m1", "m2"], "rpm": 20} and gateway[-1] == ("alterar", K["token"], m), "altera só modelos e RPM")
ok(trilha[-1][0] == "chave_alterada" and trilha[-1][2]["origem"] == "concierge" and trilha[-1][2]["antes"]["rpm"] == 10,
   "auditoria com antes/depois e origem")
s.alterar(ATOR, "ip", K["token"], rpm=20)
ok("origem" not in trilha[-1][2], "sem origem informada (telas), a auditoria não ganha o campo")
ok(levanta(validacao.Invalido, s.alterar, ATOR, "ip", K["token"], modelos=["inexistente"]), "modelo fora do gateway é recusado")
ok(levanta(validacao.Invalido, s.alterar, ATOR, "ip", K["token"], rpm=""), "RPM vazio (formulário) continua sendo erro")
ok(levanta(validacao.Invalido, s.alterar, ATOR, "ip", K["token"], modelos=[]), "lista de modelos vazia é recusada")
ok(levanta(validacao.Invalido, s.alterar, ATOR, "ip", K["token"], validade="amanha"), "validade inválida é recusada")

# bloquear / desbloquear
ok(levanta(validacao.Invalido, s.bloquear, ATOR, "ip", K["token"], "x"), "bloquear exige motivo de 5+ caracteres")
gateway.clear()
s.bloquear(ATOR, "ip", K["token"], "suspeita de vazamento")
ok(gateway == [("bloquear", K["token"])] and trilha[-1][0] == "chave_bloqueada", "bloquear chama o gateway e audita")
s.desbloquear(ATOR, "ip", K["token"])
ok(trilha[-1][0] == "chave_desbloqueada", "desbloquear audita")

# revogar
gateway.clear()
ok(levanta(validacao.Invalido, s.revogar, ATOR, "ip", K["token"], "motivo ok", "outro-nome") and not gateway,
   "revogar exige digitar o nome exato e não revoga sem isso")
s.revogar(ATOR, "ip", K["token"], "motivo ok", " time-a ")
ok(gateway == [("revogar", K["token"], "motivo ok")], "revogar com o nome certo")

# criar
gateway.clear()
segredo, token, alias = s.criar(ATOR, "ip", "novo", "", ["m1"], "30", "2", "30d", "concierge")
ok(segredo == "sk-novo" and alias == "novo" and gateway[0][1][:3] == ("novo", ["m1"], 30), "criar valida e chama o gateway")
ok(trilha[-1][0] == "chave_criada" and trilha[-1][2]["criado_para"] == "novo" and trilha[-1][2]["origem"] == "concierge",
   "criado_para assume o alias; origem auditada")
ok("sk-novo" not in str(trilha), "segredo não é auditado")
ok(levanta(validacao.Invalido, s.criar, ATOR, "ip", "novo", "", ["nao-existe"], "30", "2", "30d"), "criar com modelo inexistente")

# rotacionar
gateway.clear()
r = s.rotacionar(ATOR, "ip", K["token"], 0, "30d")
ordem = [g[0] for g in gateway]
ok(ordem == ["alterar", "gerar", "revogar"], f"rotação sem carência: renomeia, cria e revoga ({ordem})")
ok(gateway[0][2]["alias"].startswith("time-a (substituída") and r["alias"] == "time-a" and r["segredo"] == "sk-novo",
   "a antiga recebe sufixo e a nova assume o nome")
ok(levanta(validacao.Invalido, s.rotacionar, ATOR, "ip", K["token"], 31, "30d"), "carência fora de 0 a 30 é recusada")

print(f"\n{falhas} falha(s)")
sys.exit(1 if falhas else 0)
