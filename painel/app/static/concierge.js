// Concierge (home): conversa em linguagem natural com o painel. Lê o SSE de /concierge/enviar (eventos:
// status, aguardando, delta, ferramenta, link, proposta, erro, metricas, fim). Ações de escrita chegam como
// "proposta" e viram um cartão: nada é executado até o usuário confirmar (POST /concierge/confirmar/{id}).
// Segredos (chave nova, senha temporária) só existem no DOM e somem em "Nova conversa"; nunca vão ao histórico
// enviado ao modelo. Todo texto entra por textContent.
(() => {
  const raiz = document.querySelector("[data-concierge]");
  if (!raiz) return;
  const formulario = raiz.querySelector("[data-form]");
  const historico = raiz.querySelector("[data-historico]");
  const vazio = raiz.querySelector("[data-vazio]");
  const campo = formulario.elements.mensagem;
  const botaoEnviar = raiz.querySelector("[data-enviar]");
  const botaoParar = raiz.querySelector("[data-parar]");
  const csrf = formulario.elements.csrf.value;

  let mensagens = []; // só texto user/assistant: é o que o modelo recebe
  let notas = []; // resultados de ações confirmadas, anexados à próxima mensagem do usuário
  let controle = null;
  let conversa = 0;

  const criar = (tag, classe, texto) => {
    const e = document.createElement(tag);
    if (classe) e.className = classe;
    if (texto !== undefined) e.textContent = texto;
    return e;
  };
  const rolar = () => { historico.scrollTop = historico.scrollHeight; };
  const postar = (url, dados, signal) => fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrf, ...dados }),
    signal,
  });
  // Só links internos (um "/" seguido de algo que não seja "/" ou "\"), nunca URLs externas.
  const linkInterno = (href) => typeof href === "string" && (href === "/" || /^\/[^/\\]/.test(href));

  // Formatação mínima e segura: **negrito**, `código` e listas com "- ".
  const inline = (destino, texto) => {
    texto.split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach((parte) => {
      if (!parte) return;
      if (parte.startsWith("**") && parte.endsWith("**") && parte.length > 4) destino.appendChild(criar("strong", "", parte.slice(2, -2)));
      else if (parte.startsWith("`") && parte.endsWith("`") && parte.length > 2) destino.appendChild(criar("code", "", parte.slice(1, -1)));
      else destino.appendChild(document.createTextNode(parte));
    });
  };
  const formatar = (destino, texto) => {
    destino.replaceChildren();
    let lista = null;
    for (const linha of texto.split("\n")) {
      const item = /^\s*[-*]\s+(.*)$/.exec(linha);
      if (item) {
        if (!lista) { lista = criar("ul"); destino.appendChild(lista); }
        const li = criar("li");
        inline(li, item[1]);
        lista.appendChild(li);
        continue;
      }
      lista = null;
      if (!linha.trim()) continue;
      const p = criar("div", "paragrafo");
      inline(p, linha);
      destino.appendChild(p);
    }
  };

  const bolhaUsuario = (texto) => {
    const b = criar("div", "bolha user");
    b.append(criar("div", "quem", "você"), criar("div", "texto", texto));
    historico.appendChild(b);
  };

  const copiar = (botao, texto) => {
    navigator.clipboard.writeText(texto).then(() => {
      const original = botao.textContent;
      botao.textContent = "Copiado";
      setTimeout(() => (botao.textContent = original), 2000);
    });
  };

  const mostrarSegredos = (destino, segredos) => {
    for (const s of segredos) {
      const caixa = criar("div", "concierge-segredo");
      caixa.appendChild(criar("strong", "", s.rotulo));
      caixa.appendChild(criar("span", "dica", "Copie agora: este valor não será mostrado de novo."));
      const entrada = criar("input");
      entrada.readOnly = true;
      entrada.value = s.valor;
      entrada.addEventListener("focus", () => entrada.select());
      const botao = criar("button", "secundario pequeno", "Copiar");
      botao.type = "button";
      botao.addEventListener("click", () => copiar(botao, s.valor));
      caixa.append(entrada, botao);
      destino.appendChild(caixa);
    }
  };

  // Cartão de confirmação de uma ação proposta.
  const cartao = (p, destino) => {
    const c = criar("div", "concierge-cartao");
    c.appendChild(criar("div", "titulo", "Confirmar ação"));
    c.appendChild(criar("div", "", p.resumo));
    let motivo = null, confirmacao = null;
    if (p.campos.includes("motivo")) {
      const l = criar("label", "", "Motivo (de 5 a 500 caracteres)");
      motivo = criar("input");
      motivo.maxLength = 500;
      motivo.value = p.motivo_sugerido || "";
      l.appendChild(motivo);
      c.appendChild(l);
    }
    if (p.campos.includes("confirmacao")) {
      const l = criar("label", "", `Para confirmar, digite: ${p.confirmar_texto}`);
      confirmacao = criar("input");
      confirmacao.autocomplete = "off";
      l.appendChild(confirmacao);
      c.appendChild(l);
    }
    const acoes = criar("div", "acoes");
    const ok = criar("button", "", "Confirmar");
    const cancelar = criar("button", "secundario", "Cancelar");
    ok.type = cancelar.type = "button";
    const situacao = criar("span", "dica");
    acoes.append(ok, cancelar, situacao);
    c.appendChild(acoes);

    const resolver = (texto, tipo) => {
      c.classList.add("resolvido");
      c.querySelectorAll("input, button").forEach((e) => (e.disabled = true));
      acoes.replaceChildren(criar("span", `selo ${tipo === "ok" ? "ativa" : "neutro"}`, texto));
    };
    const enviarDecisao = async (url, dados) => {
      ok.disabled = cancelar.disabled = true;
      situacao.textContent = "Enviando…";
      try {
        const resp = await postar(url, dados);
        return { resp, dados: await resp.json().catch(() => ({})) };
      } catch {
        situacao.textContent = "Falha de conexão com o painel.";
        ok.disabled = cancelar.disabled = false;
        return null;
      }
    };
    ok.addEventListener("click", async () => {
      const r = await enviarDecisao(`/concierge/confirmar/${p.acao_id}`,
        { motivo: motivo ? motivo.value : "", confirmacao: confirmacao ? confirmacao.value : "" });
      if (!r) return;
      if (r.dados.ok) {
        resolver("Executada", "ok");
        c.appendChild(criar("div", "", r.dados.mensagem));
        if (r.dados.link && linkInterno(r.dados.link.href)) {
          const a = criar("a", "", r.dados.link.rotulo);
          a.href = r.dados.link.href;
          c.appendChild(a);
        }
        if (r.dados.segredos) mostrarSegredos(c, r.dados.segredos);
        notas.push(`ação confirmada e executada: ${r.dados.mensagem}`);
      } else if (r.dados.repetir) {
        situacao.textContent = r.dados.mensagem; // entrada recusada: o usuário corrige e tenta de novo
        ok.disabled = cancelar.disabled = false;
      } else {
        resolver("Não executada", "erro");
        c.appendChild(criar("div", "dica", r.dados.mensagem || `Falha (${r.resp.status}).`));
        notas.push(`ação não executada: ${r.dados.mensagem || "falha"}`);
      }
      rolar();
    });
    cancelar.addEventListener("click", async () => {
      const r = await enviarDecisao(`/concierge/cancelar/${p.acao_id}`, {});
      if (!r) return;
      resolver("Cancelada", "erro");
      notas.push("ação cancelada pelo usuário");
    });
    destino.appendChild(c);
  };

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

  const ocupado = (sim) => {
    campo.disabled = sim;
    botaoEnviar.hidden = sim;
    botaoParar.hidden = !sim;
    if (!sim) campo.focus();
  };

  const enviar = async (texto) => {
    vazio?.remove();
    const minha = conversa;
    const comNotas = notas.length ? `[Painel: ${notas.join("; ")}]\n\n${texto}` : texto;
    notas = [];
    const tentativa = [...mensagens, { role: "user", content: comNotas }].slice(-20);
    bolhaUsuario(texto);

    const bolha = criar("div", "bolha assistant");
    const status = criar("div", "chat-status", "Pensando…");
    const ferramentas = criar("div", "concierge-ferramentas");
    const conteudo = criar("div", "texto");
    const extras = criar("div");
    bolha.append(criar("div", "quem", "concierge"), status, ferramentas, conteudo, extras);
    historico.appendChild(bolha);
    rolar();

    const chips = {};
    let resposta = "", propostas = 0, erro = false, parado = false;
    const fases = { carregando: "Carregando o modelo na memória (primeira chamada)…", aguardando: "Pensando…", pensando: "Pensando…" };
    controle = new AbortController();
    ocupado(true);
    try {
      const resp = await postar("/concierge/enviar", { mensagens: JSON.stringify(tentativa) }, controle.signal);
      if (!resp.ok || !resp.body) {
        const d = await resp.json().catch(() => ({}));
        extras.appendChild(criar("p", "aviso erro", d.erro || `Falha ao contatar o painel (${resp.status}).`));
        erro = true;
      } else {
        await lerEventos(resp, (e) => {
          if (e.tipo === "status") status.textContent = fases[e.fase] || "Pensando…";
          else if (e.tipo === "aguardando") status.textContent = `Aguardando o modelo… ${e.segundos} s`;
          else if (e.tipo === "delta") {
            status.hidden = true;
            resposta += e.texto;
            formatar(conteudo, resposta);
          } else if (e.tipo === "ferramenta") {
            let chip = chips[e.id];
            if (!chip) { chip = criar("span", "selo neutro"); chips[e.id] = chip; ferramentas.appendChild(chip); }
            const nome = e.nome.replaceAll("_", " ");
            chip.className = `selo ${e.estado === "erro" ? "bloqueada" : e.estado === "ok" ? "ativa" : "neutro"}`;
            chip.textContent = e.estado === "executando" ? `${nome}…` : e.estado === "erro" ? `${nome}: falhou` : nome;
            chip.title = e.resumo || "";
          } else if (e.tipo === "link") {
            if (linkInterno(e.href)) {
              const a = criar("a", "", e.rotulo);
              a.href = e.href;
              extras.appendChild(a);
            }
          } else if (e.tipo === "proposta") { propostas += 1; cartao(e, extras); }
          else if (e.tipo === "erro") { erro = true; extras.appendChild(criar("p", "aviso erro", e.mensagem)); }
          rolar();
        });
      }
    } catch (e) {
      if (e.name === "AbortError") parado = true;
      else { extras.appendChild(criar("p", "aviso erro", "Falha de conexão com o painel.")); erro = true; }
    } finally {
      controle = null;
      status.hidden = true;
      ocupado(false);
    }
    if (parado) extras.appendChild(criar("p", "dica", "Interrompido."));
    if (minha !== conversa) return;
    if (!erro && !parado) {
      const final = resposta.trim() || (propostas ? "Registrei a proposta; aguarda a confirmação do usuário." : "");
      if (final) mensagens = [...tentativa, { role: "assistant", content: final }];
    }
    rolar();
  };

  formulario.addEventListener("submit", (e) => {
    e.preventDefault();
    const texto = campo.value.trim();
    if (!texto || controle) return;
    campo.value = "";
    campo.style.height = "auto";
    enviar(texto);
  });
  campo.addEventListener("input", () => {
    campo.style.height = "auto";
    campo.style.height = `${Math.min(campo.scrollHeight, 160)}px`;
  });
  campo.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); formulario.requestSubmit(); }
  });
  raiz.querySelectorAll("[data-sugestao]").forEach((b) => b.addEventListener("click", () => {
    campo.value = b.dataset.sugestao;
    campo.focus();
  }));
  botaoParar.addEventListener("click", () => controle?.abort());
  raiz.querySelector("[data-limpar]").addEventListener("click", () => {
    controle?.abort();
    conversa += 1;
    mensagens = [];
    notas = [];
    historico.replaceChildren(...(vazio ? [vazio] : []));
  });
})();
