"""Liberar e retirar um modelo em várias chaves de uma vez (a partir do modelo).

Só mexe em chaves vivas e não expiradas que têm lista de modelos. Chaves sem lista ("todos") ficam de
fora: adicionar um modelo a elas as restringiria. Chaves expiradas perdem os modelos pela rotina.
"""
from dataclasses import dataclass, field

from . import auditoria, consultas, litellm
from .validacao import SEM_ACESSO


@dataclass
class Plano:
    modelo: str
    adicionar: list[dict] = field(default_factory=list)   # {"chave": k, "novos": [...]}
    retirar: list[dict] = field(default_factory=list)
    bloqueadas: list[dict] = field(default_factory=list)  # retirar deixaria a chave sem nenhum modelo

    @property
    def alteracoes(self) -> list[dict]:
        return self.adicionar + self.retirar


@dataclass
class Resultado:
    alteradas: int = 0
    falhas: list[tuple[str, str]] = field(default_factory=list)


def elegiveis(chaves: list[dict]) -> list[dict]:
    """Chaves que a tela em massa pode alterar."""
    return [k for k in chaves if k["status"] != "expirada" and (k["modelos"] or [])]


def planejar(modelo: str, chaves: list[dict], marcadas: set[str], ultimo_vira_sem_acesso: bool = False) -> Plano:
    """Diferença entre o estado atual das chaves e a seleção. `marcadas` são tokens que devem ter o modelo."""
    plano = Plano(modelo)
    for k in elegiveis(chaves):
        reais = k["modelos_reais"]
        tem, quer = modelo in reais, k["token"] in marcadas
        if quer and not tem:
            plano.adicionar.append({"chave": k, "novos": reais + [modelo]})
        elif tem and not quer:
            restantes = [m for m in reais if m != modelo]
            if restantes:
                plano.retirar.append({"chave": k, "novos": restantes})
            elif ultimo_vira_sem_acesso:
                plano.retirar.append({"chave": k, "novos": [SEM_ACESSO]})
            else:
                plano.bloqueadas.append({"chave": k, "novos": []})
    return plano


def executar(plano: Plano, usuario: str, ip: str | None, origem: str = "massa") -> Resultado:
    """Aplica o plano chave a chave. Uma falha não interrompe as demais; repetir é seguro (idempotente)."""
    res = Resultado()
    for item in plano.alteracoes:
        k = item["chave"]
        alias = k["key_alias"] or k["token"][:12]
        try:
            litellm.alterar_chave(k["token"], modelos=item["novos"])
        except litellm.ErroLiteLLM as e:
            res.falhas.append((alias, str(e)))
            continue
        res.alteradas += 1
        auditoria.registrar("chave_alterada", usuario, ip, k["key_alias"], {
            "token": k["token"], "origem": origem, "modelo": plano.modelo,
            "antes": {"modelos": k["modelos"]}, "depois": {"modelos": item["novos"]}})
    auditoria.registrar("modelo_chaves_ajustadas", usuario, ip, plano.modelo, {
        "origem": origem, "adicionadas": len(plano.adicionar), "retiradas": len(plano.retirar),
        "bloqueadas": len(plano.bloqueadas), "alteradas": res.alteradas, "falhas": len(res.falhas)})
    return res


def aplicar(modelo: str, marcadas: set[str], usuario: str, ip: str | None, origem: str = "massa") -> tuple[Plano, Resultado]:
    """Relê as chaves e aplica: o que mudou desde que a tela foi aberta é recalculado."""
    plano = planejar(modelo, consultas.chaves(), marcadas)
    return plano, executar(plano, usuario, ip, origem)


def resumo(plano: Plano, res: Resultado) -> str:
    texto = f"{res.alteradas} chave(s) alterada(s): {len(plano.adicionar)} liberada(s), {len(plano.retirar)} retirada(s)."
    if plano.bloqueadas:
        texto += f" {len(plano.bloqueadas)} mantida(s) por ficarem sem nenhum modelo."
    if res.falhas:
        texto += f" Falharam {len(res.falhas)}: " + "; ".join(f"{a} ({e})" for a, e in res.falhas[:3])
        texto += "…" if len(res.falhas) > 3 else ""
    return texto
