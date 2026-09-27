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
