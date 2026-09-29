// Comportamentos mínimos (sem scripts inline, por causa da CSP).
document.addEventListener("click", (e) => {
  const botao = e.target.closest("[data-copiar]");
  if (!botao) return;
  const alvo = document.getElementById(botao.dataset.copiar);
  navigator.clipboard.writeText(alvo.textContent.trim()).then(() => {
    const original = botao.textContent;
    botao.textContent = "Copiado";
    setTimeout(() => (botao.textContent = original), 2000);
  });
});

document.addEventListener("submit", (e) => {
  const mensagem = e.target.dataset.confirmar;
  if (mensagem && !window.confirm(mensagem)) e.preventDefault();
});

// Progresso dos downloads de modelos: consulta a cada 3 s enquanto houver download em andamento
// e recarrega a página quando algum termina (para atualizar a lista de instalados).
(() => {
  const tabela = document.querySelector("[data-downloads]");
  if (!tabela || !tabela.querySelector('[data-status="baixando"]')) return;
  const tamanho = (b) => (b ? (b / 1024 ** 3).toFixed(2).replace(".", ",") + " GB" : "—");
  const atualizar = async () => {
    let dados;
    try {
      dados = await (await fetch(tabela.dataset.downloads, { credentials: "same-origin" })).json();
    } catch {
      return setTimeout(atualizar, 5000);
    }
    for (const d of dados) {
      const linha = tabela.querySelector(`[data-download="${d.id}"]`);
      if (!linha) continue;
      if (linha.dataset.status === "baixando" && d.status !== "baixando") return location.reload();
      const celula = linha.querySelector("[data-progresso]");
      const barra = celula.querySelector("progress");
      if (barra && d.percentual !== null) barra.value = d.percentual;
      const texto = celula.querySelector("span");
      if (texto) {
        const pct = d.percentual !== null ? `${d.percentual}% ` : "";
        texto.textContent = `${pct}${tamanho(d.baixado)} / ${tamanho(d.total)} · ${d.mensagem || ""}`;
      }
    }
    setTimeout(atualizar, 3000);
  };
  setTimeout(atualizar, 3000);
})();


// Chat: envia a conversa para /chat/enviar e mostra a resposta conforme chega (SSE lido via fetch,
// não EventSource, porque a requisição é POST com csrf no corpo). O servidor emite eventos próprios:
// status, aguardando, raciocinio, delta, erro, metricas e fim. Nada é inserido como HTML: todo texto
// do modelo entra por textContent.
(() => {
  const painel = document.querySelector("[data-chat]");
  if (!painel) return;
  const formulario = painel.querySelector("form");
  const $ = (seletor) => painel.querySelector(seletor);
  const historico = $("[data-historico]");
  const vazio = $("[data-vazio]");
  const campo = formulario.elements.mensagem;
  const selecaoModelo = formulario.elements.modelo;
  const botaoEnviar = $("[data-enviar]");
  const botaoParar = $("[data-parar]");
  const botaoAquecer = $("[data-aquecer]");
  const seloEstado = $("[data-estado]");
  const detalhesModelo = $("[data-detalhes]");
  const resumoParametros = $("[data-resumo-parametros]");
  const csrf = formulario.elements.csrf.value;

  let mensagens = []; // histórico enviado ao modelo: só trocas concluídas
  let registro = []; // o mesmo histórico, com modelo e métricas, para "Copiar conversa"
  let controle = null; // AbortController da resposta em andamento
  let seguir = true; // rolagem automática, desligada quando o usuário sobe no histórico
  let conversa = 0; // muda em "Limpar": respostas de uma conversa anterior são descartadas

  const criar = (tag, classe, texto) => {
    const e = document.createElement(tag);
    if (classe) e.className = classe;
    if (texto !== undefined) e.textContent = texto;
    return e;
  };
  const rolar = () => { if (seguir) historico.scrollTop = historico.scrollHeight; };
  historico.addEventListener("scroll", () => {
    seguir = historico.scrollHeight - historico.scrollTop - historico.clientHeight < 80;
  });

  const segundos = (ms) => `${(ms / 1000).toFixed(1).replace(".", ",")} s`;
  const numero = (n) => Number(n).toLocaleString("pt-BR");

  const copiar = (botao, texto) => {
    navigator.clipboard.writeText(texto).then(() => {
      const original = botao.textContent;
      botao.textContent = "Copiado";
      setTimeout(() => (botao.textContent = original), 2000);
    });
  };

  // Formatação mínima e segura: blocos ``` viram <pre> com botão Copiar; o resto fica como texto.
  const formatar = (destino, texto) => {
    destino.replaceChildren();
    texto.split("```").forEach((parte, i) => {
      if (i % 2 === 0) {
        if (parte.trim()) destino.appendChild(criar("div", "paragrafo", parte.replace(/^\n+|\n+$/g, "")));
        return;
      }
      const quebra = parte.indexOf("\n");
      const linguagem = quebra >= 0 ? parte.slice(0, quebra).trim() : "";
      const codigo = quebra >= 0 ? parte.slice(quebra + 1).replace(/\n$/, "") : parte;
      const bloco = criar("div", "codigo");
      const topo = criar("div", "codigo-topo", linguagem || "código");
      const botao = criar("button", "link", "Copiar");
      botao.type = "button";
      botao.addEventListener("click", () => copiar(botao, codigo));
      topo.appendChild(botao);
      const pre = criar("pre");
      pre.appendChild(criar("code", "", codigo));
      bloco.append(topo, pre);
      destino.appendChild(bloco);
    });
  };

  // Modelo: selo carregado/frio e detalhes do Ollama.
  const atualizarInfo = () => {
    const opcao = selecaoModelo.selectedOptions[0];
    const carregado = !!opcao.dataset.carregado;
    seloEstado.textContent = carregado ? "carregado" : "fora da memória";
    seloEstado.className = `selo ${carregado ? "ativo" : "neutro"}`;
    seloEstado.title = carregado
      ? "Na memória: a resposta começa rápido."
      : "A primeira resposta inclui a carga do modelo na memória, que pode levar minutos em modelos grandes.";
    const { parametros, quantizacao, contexto } = opcao.dataset;
    detalhesModelo.textContent = [parametros, quantizacao, contexto && `contexto ${numero(contexto)}`]
      .filter(Boolean).join(" · ");
  };
  const atualizarEstado = async () => {
    try {
      const dados = await (await fetch("/chat/estado.json", { credentials: "same-origin" })).json();
      for (const opcao of selecaoModelo.options) {
        if (opcao.value in dados) opcao.dataset.carregado = dados[opcao.value] ? "1" : "";
      }
      atualizarInfo();
    } catch { /* mantém o estado anterior */ }
  };
  selecaoModelo.addEventListener("change", atualizarInfo);
  atualizarInfo();

  const nota = (texto, tipo = "") => {
    vazio.remove();
    historico.appendChild(criar("p", `chat-nota ${tipo}`, texto));
    rolar();
  };

  const postar = (url, dados, signal) => fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrf, ...dados }),
    signal,
  });

  botaoAquecer?.addEventListener("click", async () => {
    const modelo = selecaoModelo.value;
    botaoAquecer.disabled = true;
    botaoAquecer.textContent = "Carregando…";
    nota(`Carregando ${modelo} na memória. Em modelos grandes isso leva alguns minutos.`);
    try {
      const resp = await postar("/chat/aquecer", { modelo });
      const dados = await resp.json().catch(() => ({}));
      if (resp.ok) nota(`${modelo} carregado em ${segundos(dados.segundos * 1000)}.`, "ok");
      else nota(dados.erro || `Falha ao carregar ${modelo} (${resp.status}).`, "erro");
    } catch {
      nota("Falha de conexão com o painel.", "erro");
    } finally {
      botaoAquecer.disabled = false;
      botaoAquecer.textContent = "Aquecer";
      atualizarEstado();
    }
  });

  // Parâmetros: resumo no título do bloco recolhido.
  const parametros = () => ({
    temperatura: formulario.elements.temperatura.value.trim(),
    max_tokens: formulario.elements.max_tokens.value.trim(),
    seed: formulario.elements.seed.value.trim(),
  });
  const atualizarResumo = () => {
    const p = parametros();
    const partes = [];
    if (formulario.elements.sistema.value.trim()) partes.push("prompt de sistema");
    if (p.temperatura) partes.push(`temperatura ${p.temperatura}`);
    if (p.max_tokens) partes.push(`máx. ${p.max_tokens} tokens`);
    if (p.seed) partes.push(`seed ${p.seed}`);
    resumoParametros.textContent = partes.length ? `(${partes.join(", ")})` : "(padrão do modelo)";
  };
  $(".chat-parametros").addEventListener("input", atualizarResumo);
  atualizarResumo();

  // Caixa de texto que cresce com o conteúdo; Enter envia, Shift+Enter quebra linha.
  const ajustarAltura = () => {
    campo.style.height = "auto";
    campo.style.height = `${Math.min(campo.scrollHeight, 220)}px`;
  };
  campo.addEventListener("input", ajustarAltura);
  campo.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      formulario.requestSubmit();
    }
  });
  painel.querySelectorAll("[data-sugestao]").forEach((b) => b.addEventListener("click", () => {
    campo.value = b.dataset.sugestao;
    ajustarAltura();
    campo.focus();
  }));

  const ocupado = (sim) => {
    campo.disabled = sim;
    botaoEnviar.hidden = sim;
    botaoParar.hidden = !sim;
    selecaoModelo.disabled = sim;
    if (!sim) campo.focus();
  };

  const bolhaUsuario = (texto) => {
    const bolha = criar("div", "bolha user");
    bolha.append(criar("div", "quem", "você"), criar("div", "texto", texto));
    historico.appendChild(bolha);
    return bolha;
  };

  // Bolha da resposta: status com cronômetro, raciocínio recolhível, texto, erro e rodapé.
  const bolhaResposta = (modelo) => {
    const bolha = criar("div", "bolha assistant");
    const status = criar("div", "chat-status");
    const raciocinio = criar("details", "chat-raciocinio");
    const raciocinioTexto = criar("div", "texto");
    raciocinio.append(criar("summary", "", "Raciocínio"), raciocinioTexto);
    raciocinio.hidden = true;
    const texto = criar("div", "texto");
    const rodape = criar("div", "chat-rodape");
    const metricas = criar("span", "dica");
    const acoes = criar("span", "chat-acoes");
    rodape.append(metricas, acoes);
    bolha.append(criar("div", "quem", modelo), status, raciocinio, texto, rodape);
    historico.appendChild(bolha);

    const inicio = performance.now();
    const fases = {
      carregando: "Carregando o modelo na memória (primeira chamada)",
      aguardando: "Aguardando o modelo",
      pensando: "Pensando",
    };
    let fase = "aguardando";
    const tique = () => {
      if (!fases[fase]) return;
      status.textContent = `${fases[fase]}… ${Math.round((performance.now() - inicio) / 1000)} s`;
    };
    const relogio = setInterval(tique, 1000);
    tique();

    let conteudo = "", pensamento = "", agendado = false;
    const desenhar = () => {
      agendado = false;
      formatar(texto, conteudo);
      raciocinioTexto.textContent = pensamento;
      rolar();
    };
    const agendar = () => {
      if (!agendado) { agendado = true; requestAnimationFrame(desenhar); }
    };

    return {
      bolha,
      get conteudo() { return conteudo; },
      fase(nova) {
        fase = nova;
        status.hidden = !fases[fase];
        tique();
      },
      delta(pedaco) { conteudo += pedaco; agendar(); },
      raciocinio(pedaco) { pensamento += pedaco; raciocinio.hidden = false; agendar(); },
      erro(mensagem) {
        bolha.insertBefore(criar("p", "aviso erro", mensagem), rodape);
        rolar();
      },
      metricas(m) {
        const partes = [];
        if (m.ttft_ms !== undefined) partes.push(`1º token em ${segundos(m.ttft_ms)}`);
        if (m.tokens_s) partes.push(`${numero(m.tokens_s)} tok/s`);
        if (m.tokens_saida) partes.push(`${numero(m.tokens_saida)} tokens`);
        partes.push(`total ${segundos(m.total_ms)}`);
        if (m.frio) partes.push("incluiu a carga do modelo");
        if (m.fim === "length") partes.push("cortada pelo limite de tokens");
        metricas.textContent = partes.join(" · ");
        return metricas.textContent;
      },
      acao(rotulo, funcao) {
        const botao = criar("button", "link", rotulo);
        botao.type = "button";
        botao.addEventListener("click", () => funcao(botao));
        acoes.appendChild(botao);
        return botao;
      },
      encerrar() {
        clearInterval(relogio);
        status.hidden = true;
        desenhar();
      },
    };
  };

  // Lê o corpo SSE e entrega cada evento a `tratar`.
  const lerEventos = async (resp, tratar) => {
    const leitor = resp.body.getReader();
    const decodificador = new TextDecoder();
    let restante = "";
    for (;;) {
      const { done, value } = await leitor.read();
      if (done) break;
      restante += decodificador.decode(value, { stream: true });
      const partes = restante.split("\n\n");
      restante = partes.pop();
      for (const parte of partes) {
        const linha = parte.trim();
        if (!linha.startsWith("data:")) continue;
        try { tratar(JSON.parse(linha.slice(5))); } catch { /* evento malformado */ }
      }
    }
  };

  const enviar = async (texto, anterior) => {
    vazio.remove();
    historico.querySelectorAll("[data-retentar]").forEach((b) => b.remove());
    seguir = true;
    const minhaConversa = conversa;
    const modelo = selecaoModelo.value;
    const sistema = formulario.elements.sistema.value.trim();
    const tentativa = [...mensagens, { role: "user", content: texto }];
    const enviadas = sistema ? [{ role: "system", content: sistema }, ...tentativa] : tentativa;

    const usuario = anterior?.usuario || bolhaUsuario(texto);
    usuario.classList.remove("falhou");
    anterior?.resposta.remove();
    const resposta = bolhaResposta(modelo);
    rolar();

    controle = new AbortController();
    ocupado(true);
    let falhou = false, parado = false, resumo = "";
    try {
      const resp = await postar("/chat/enviar",
        { modelo, mensagens: JSON.stringify(enviadas), ...parametros() }, controle.signal);
      if (!resp.ok || !resp.body) {
        const dados = await resp.json().catch(() => ({}));
        resposta.erro(dados.erro || `Falha ao contatar o painel (${resp.status}).`);
        falhou = true;
      } else {
        await lerEventos(resp, (evento) => {
          if (evento.tipo === "status") resposta.fase(evento.fase);
          else if (evento.tipo === "delta") resposta.delta(evento.texto);
          else if (evento.tipo === "raciocinio") resposta.raciocinio(evento.texto);
          else if (evento.tipo === "erro") { resposta.erro(evento.mensagem); falhou = true; }
          else if (evento.tipo === "metricas") resumo = resposta.metricas(evento);
        });
      }
    } catch (e) {
      if (e.name === "AbortError") parado = true;
      else { resposta.erro("Falha de conexão com o painel."); falhou = true; }
    } finally {
      controle = null;
      resposta.encerrar();
      ocupado(false);
    }

    if (minhaConversa !== conversa) return;
    const conteudo = resposta.conteudo;
    if (parado) resposta.erro(conteudo ? "Resposta interrompida." : "Interrompida antes de o modelo responder.");
    if (conteudo && !falhou) {
      mensagens = [...tentativa, { role: "assistant", content: conteudo }];
      registro.push({ papel: "você", texto }, { papel: modelo, texto: conteudo, metricas: resumo });
      resposta.acao("Copiar", (b) => copiar(b, conteudo));
    } else {
      usuario.classList.add("falhou");
      const botao = resposta.acao("Tentar de novo", () => enviar(texto, { usuario, resposta: resposta.bolha }));
      botao.dataset.retentar = "";
    }
    atualizarEstado();
  };

  formulario.addEventListener("submit", (e) => {
    e.preventDefault();
    const texto = campo.value.trim();
    if (!texto || controle) return;
    campo.value = "";
    ajustarAltura();
    enviar(texto);
  });

  botaoParar.addEventListener("click", () => controle?.abort());

  $("[data-limpar]").addEventListener("click", () => {
    controle?.abort();
    conversa += 1;
    mensagens = [];
    registro = [];
    historico.replaceChildren(vazio);
  });

  $("[data-exportar]").addEventListener("click", (e) => {
    if (!registro.length) return;
    const sistema = formulario.elements.sistema.value.trim();
    const linhas = [`# Chat · ${new Date().toLocaleString("pt-BR")}`, "", `Parâmetros: ${resumoParametros.textContent}`];
    if (sistema) linhas.push("", "## Prompt de sistema", "", sistema);
    for (const r of registro) {
      linhas.push("", `## ${r.papel}${r.metricas ? ` (${r.metricas})` : ""}`, "", r.texto);
    }
    copiar(e.currentTarget, linhas.join("\n"));
  });
})();
