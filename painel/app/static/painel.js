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

// Chat: envia a mensagem para /chat/enviar e mostra a resposta conforme chega (SSE via fetch,
// não EventSource, porque a requisição é POST com csrf no corpo).
(() => {
  const painel = document.querySelector("[data-chat]");
  if (!painel) return;
  const formulario = painel.querySelector("form");
  const historico = formulario.querySelector("[data-historico]");
  const campo = formulario.querySelector("[name=mensagem]");
  const selecaoModelo = formulario.querySelector("[name=modelo]");
  const botaoEnviar = formulario.querySelector("button[type=submit]");
  const botaoLimpar = painel.querySelector("[data-limpar]");
  const csrf = formulario.querySelector("[name=csrf]").value;

  let mensagens = [];

  const rolar = () => (historico.scrollTop = historico.scrollHeight);

  const bolha = (papel, texto) => {
    const div = document.createElement("div");
    div.className = `bolha ${papel}`;
    const quem = document.createElement("div");
    quem.className = "quem";
    quem.textContent = papel === "user" ? "você" : "modelo";
    const corpo = document.createElement("div");
    corpo.className = "texto";
    corpo.textContent = texto;
    div.append(quem, corpo);
    historico.appendChild(div);
    rolar();
    return corpo;
  };

  const erro = (texto) => {
    const p = document.createElement("p");
    p.className = "aviso erro";
    p.textContent = texto;
    historico.appendChild(p);
    rolar();
  };

  botaoLimpar.addEventListener("click", () => {
    mensagens = [];
    historico.innerHTML = "";
  });

  campo.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      formulario.requestSubmit();
    }
  });

  formulario.addEventListener("submit", async (e) => {
    e.preventDefault();
    const texto = campo.value.trim();
    if (!texto) return;
    mensagens.push({ role: "user", content: texto });
    bolha("user", texto);
    campo.value = "";
    campo.disabled = true;
    botaoEnviar.disabled = true;

    const alvo = bolha("assistant", "");
    let acumulado = "";

    try {
      const resp = await fetch("/chat/enviar", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ csrf, modelo: selecaoModelo.value, mensagens: JSON.stringify(mensagens) }),
      });
      if (!resp.body) throw new Error("sem corpo");
      if (!resp.ok) {
        let detalhe;
        try { detalhe = (await resp.json()).erro; } catch { /* ignora */ }
        erro(detalhe || `Falha ao contatar o gateway (${resp.status}).`);
        alvo.closest(".bolha").remove();
        mensagens.pop();
        return;
      }
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
          const payload = linha.slice(5).trim();
          if (!payload) continue;
          let evento;
          try { evento = JSON.parse(payload); } catch { continue; }
          if (evento.erro) {
            erro(evento.erro);
            continue;
          }
          acumulado += evento.choices?.[0]?.delta?.content || "";
          alvo.textContent = acumulado;
          rolar();
        }
      }
      if (acumulado) {
        mensagens.push({ role: "assistant", content: acumulado });
      } else {
        alvo.closest(".bolha").remove();
        mensagens.pop();
      }
    } catch {
      erro("Falha de conexão com o painel.");
      alvo.closest(".bolha").remove();
      mensagens.pop();
    } finally {
      campo.disabled = false;
      botaoEnviar.disabled = false;
      campo.focus();
    }
  });
})();
