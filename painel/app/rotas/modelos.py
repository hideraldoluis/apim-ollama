"""Modelos: o que está instalado no Ollama, o que está publicado no gateway e o uso de cada um.

Todos consultam; só admin baixa, publica, retira, remove, carrega e descarrega.
Regras de proteção:
- não se retira do gateway um modelo liberado em alguma chave não expirada (use "Chaves do modelo" para
  retirá-lo em massa, ou "Retirar de todas e despublicar");
- não se remove do Ollama um modelo publicado no gateway ou com download em andamento.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auditoria, autenticacao as auth, consultas, downloads, litellm, modelos_chaves, ollama, validacao
from ..web import redirecionar, render

router = APIRouter(prefix="/modelos")


def _estado() -> dict:
    """Junta Ollama, gateway, chaves e consumo. Falhas de um serviço não impedem mostrar o resto."""
    erros = []
    try:
        instalados = ollama.instalados()
        carregados = ollama.carregados()
        versao = ollama.versao()
    except ollama.ErroOllama as e:
        instalados, carregados, versao = [], {}, None
        erros.append(str(e))
    try:
        publicados = litellm.modelos_gateway()
    except litellm.ErroLiteLLM as e:
        publicados = []
        erros.append(str(e))

    inicio, fim = consultas.periodo(None, None, 30)
    uso = {r["modelo"]: r for r in consultas.consumo(inicio, fim, "modelo")}
    chaves = [k for k in consultas.chaves() if k["status"] != "expirada"]
    nomes_instalados = {m["nome"] for m in instalados}

    for p in publicados:
        alvo = ollama.nome_completo(p["ollama"]) if p["ollama"] else None
        p["instalado"] = alvo in nomes_instalados if alvo else None
        p["carregado"] = alvo in carregados if alvo else False
        p["chaves"] = [k for k in chaves if p["nome"] in (k["modelos"] or [])]
        p["uso"] = uso.get(p["nome"])
    for m in instalados:
        m["publicado_como"] = [p for p in publicados if p["ollama"] and ollama.nome_completo(p["ollama"]) == m["nome"]]
        m["carregado"] = carregados.get(m["nome"])
        m["embedding"] = ollama.e_embedding(m["capacidades"])
        m["sugestao_publica"] = m["nome"].removesuffix(":latest").rsplit("/", 1)[-1]
    return {"instalados": instalados, "publicados": publicados, "versao": versao, "erros": erros,
            "inicio": inicio, "fim": fim, "memoria": sum((c.get("tamanho") or 0) for c in carregados.values())}


@router.get("")
def pagina(request: Request, u: dict = Depends(auth.usuario_logado)):
    estado = _estado()
    return render(request, "modelos.html", {**estado, "downloads": downloads.recentes()})


@router.get("/downloads.json")
def progresso(request: Request, u: dict = Depends(auth.usuario_logado)):
    return JSONResponse([
        {"id": d["id"], "status": d["status"], "percentual": d["percentual"], "mensagem": d.get("mensagem"),
         "baixado": d.get("baixado"), "total": d.get("total")}
        for d in downloads.recentes()
    ])


@router.post("/baixar")
def baixar(request: Request, u: dict = Depends(auth.exigir_admin), modelo: str = Form("")):
    try:
        modelo = validacao.modelo_ollama(modelo)
        downloads.iniciar(modelo, u["login"], auth.ip_cliente(request))
    except (validacao.Invalido, ValueError) as e:
        return redirecionar("/modelos", str(e), "erro")
    return redirecionar("/modelos", f"Download de {modelo} iniciado. O progresso aparece abaixo.")


@router.post("/publicar")
def publicar(request: Request, u: dict = Depends(auth.exigir_admin),
             modelo: str = Form(""), nome_publico: str = Form("")):
    try:
        nome_publico = validacao.modelo_publico(nome_publico)
        instalado = next((m for m in ollama.instalados() if m["nome"] == modelo), None)
        if instalado is None:
            raise validacao.Invalido(f"{modelo} não está instalado no Ollama.")
        if any(p["nome"] == nome_publico for p in litellm.modelos_gateway()):
            raise validacao.Invalido(f"Já existe um modelo publicado como {nome_publico}.")
        embedding = ollama.e_embedding(instalado["capacidades"])
        litellm.publicar_modelo(nome_publico, modelo, embedding)
    except (validacao.Invalido, ollama.ErroOllama, litellm.ErroLiteLLM) as e:
        return redirecionar("/modelos", str(e), "erro")
    auditoria.registrar("modelo_publicado", u["login"], auth.ip_cliente(request), nome_publico,
                        {"ollama": modelo, "tipo": "embedding" if embedding else "chat"})
    return redirecionar("/modelos", f"{modelo} publicado como {nome_publico}. Libere-o nas chaves que devem usá-lo.")


@router.post("/despublicar")
def despublicar(request: Request, u: dict = Depends(auth.exigir_admin),
                model_id: str = Form(""), confirmacao: str = Form("")):
    try:
        alvo = next((p for p in litellm.modelos_gateway() if p["id"] == model_id), None)
        if alvo is None:
            raise validacao.Invalido("Modelo não encontrado no gateway.")
        em_uso = [k["key_alias"] or k["token"][:12] for k in consultas.chaves()
                  if k["status"] != "expirada" and alvo["nome"] in (k["modelos"] or [])]
        if em_uso:
            raise validacao.Invalido(f"{alvo['nome']} está liberado nas chaves: {', '.join(em_uso)}. "
                                     "Retire-o dessas chaves (Chaves do modelo) ou use \"Retirar de todas e despublicar\".")
        if confirmacao.strip() != alvo["nome"]:
            raise validacao.Invalido("Para retirar, digite o nome público do modelo exatamente como aparece.")
        litellm.despublicar_modelo(model_id)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar("/modelos", str(e), "erro")
    auditoria.registrar("modelo_despublicado", u["login"], auth.ip_cliente(request), alvo["nome"],
                        {"ollama": alvo["ollama"], "id": model_id})
    return redirecionar("/modelos", f"{alvo['nome']} retirado do gateway. Continua instalado no Ollama.")


@router.post("/despublicar-total")
def despublicar_total(request: Request, u: dict = Depends(auth.exigir_admin),
                      model_id: str = Form(""), confirmacao: str = Form("")):
    """Retira o modelo de todas as chaves e depois do gateway. Chave que ficaria sem modelo passa a SEM_ACESSO."""
    ip = auth.ip_cliente(request)
    try:
        alvo = next((p for p in litellm.modelos_gateway() if p["id"] == model_id), None)
        if alvo is None:
            raise validacao.Invalido("Modelo não encontrado no gateway.")
        if confirmacao.strip() != alvo["nome"]:
            raise validacao.Invalido("Para retirar, digite o nome público do modelo exatamente como aparece.")
        plano = modelos_chaves.planejar(alvo["nome"], consultas.chaves(), set(), ultimo_vira_sem_acesso=True)
        res = modelos_chaves.executar(plano, u["login"], ip, "despublicar-total")
        if res.falhas:
            raise validacao.Invalido("O modelo não foi retirado do gateway porque algumas chaves não foram alteradas. "
                                     + modelos_chaves.resumo(plano, res))
        litellm.despublicar_modelo(model_id)
    except (validacao.Invalido, litellm.ErroLiteLLM) as e:
        return redirecionar("/modelos", str(e), "erro")
    auditoria.registrar("modelo_despublicado", u["login"], ip, alvo["nome"],
                        {"ollama": alvo["ollama"], "id": model_id, "chaves_alteradas": res.alteradas})
    return redirecionar("/modelos", f"{alvo['nome']} retirado de {res.alteradas} chave(s) e do gateway. "
                                    "Continua instalado no Ollama.")


def _publicado_ou_404(nome: str) -> dict:
    try:
        alvo = next((p for p in litellm.modelos_gateway() if p["nome"] == nome), None)
    except litellm.ErroLiteLLM as e:
        raise HTTPException(502, str(e))
    if alvo is None:
        raise HTTPException(404)
    return alvo


def _estado_chaves(nome: str) -> dict:
    chaves = consultas.chaves()
    elegiveis = modelos_chaves.elegiveis(chaves)
    return {
        "modelo": nome,
        "chaves": elegiveis,
        "todos": [k for k in chaves if k["status"] != "expirada" and not (k["modelos"] or [])],
        "expiradas": sum(1 for k in chaves if k["status"] == "expirada"),
    }


@router.get("/{nome}/chaves")
def chaves_do_modelo(request: Request, nome: str, u: dict = Depends(auth.exigir_admin)):
    _publicado_ou_404(nome)
    return render(request, "modelo_chaves.html", _estado_chaves(nome))


@router.post("/{nome}/chaves/revisar")
def chaves_revisar(request: Request, nome: str, u: dict = Depends(auth.exigir_admin), chave: list[str] = Form([])):
    _publicado_ou_404(nome)
    marcadas = set(chave)
    plano = modelos_chaves.planejar(nome, consultas.chaves(), marcadas)
    if not plano.alteracoes and not plano.bloqueadas:
        return redirecionar(f"/modelos/{nome}/chaves", "Nada foi alterado.", "aviso")
    return render(request, "modelo_chaves_revisao.html", {"modelo": nome, "plano": plano, "marcadas": sorted(marcadas)})


@router.post("/{nome}/chaves/aplicar")
def chaves_aplicar(request: Request, nome: str, u: dict = Depends(auth.exigir_admin), chave: list[str] = Form([])):
    _publicado_ou_404(nome)
    plano, res = modelos_chaves.aplicar(nome, set(chave), u["login"], auth.ip_cliente(request))
    return redirecionar(f"/modelos/{nome}/chaves", modelos_chaves.resumo(plano, res), "erro" if res.falhas else "ok")


@router.post("/remover")
def remover(request: Request, u: dict = Depends(auth.exigir_admin),
            modelo: str = Form(""), confirmacao: str = Form("")):
    try:
        if modelo not in {m["nome"] for m in ollama.instalados()}:
            raise validacao.Invalido(f"{modelo} não está instalado no Ollama.")
        publicados = [p["nome"] for p in litellm.modelos_gateway()
                      if p["ollama"] and ollama.nome_completo(p["ollama"]) == modelo]
        if publicados:
            raise validacao.Invalido(f"{modelo} está publicado no gateway como {', '.join(publicados)}. "
                                     "Retire-o do gateway antes.")
        if downloads.em_andamento(modelo):
            raise validacao.Invalido(f"Há um download de {modelo} em andamento.")
        if confirmacao.strip() != modelo:
            raise validacao.Invalido("Para remover, digite o nome do modelo exatamente como aparece.")
        ollama.remover(modelo)
    except (validacao.Invalido, ollama.ErroOllama, litellm.ErroLiteLLM) as e:
        return redirecionar("/modelos", str(e), "erro")
    auditoria.registrar("modelo_removido", u["login"], auth.ip_cliente(request), modelo)
    return redirecionar("/modelos", f"{modelo} removido do Ollama.")


@router.post("/carregar")
def carregar(request: Request, u: dict = Depends(auth.exigir_admin), modelo: str = Form("")):
    return _memoria(request, u, modelo, carregar=True)


@router.post("/descarregar")
def descarregar(request: Request, u: dict = Depends(auth.exigir_admin), modelo: str = Form("")):
    return _memoria(request, u, modelo, carregar=False)


def _memoria(request: Request, u: dict, modelo: str, carregar: bool):
    try:
        instalado = next((m for m in ollama.instalados() if m["nome"] == modelo), None)
        if instalado is None:
            raise validacao.Invalido(f"{modelo} não está instalado no Ollama.")
        embedding = ollama.e_embedding(instalado["capacidades"])
        (ollama.carregar if carregar else ollama.descarregar)(modelo, embedding)
    except (validacao.Invalido, ollama.ErroOllama) as e:
        return redirecionar("/modelos", str(e), "erro")
    auditoria.registrar("modelo_carregado" if carregar else "modelo_descarregado", u["login"],
                        auth.ip_cliente(request), modelo)
    return redirecionar("/modelos", f"{modelo} {'carregado na' if carregar else 'descarregado da'} memória.")
