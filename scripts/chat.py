#!/usr/bin/env python3
"""Envia uma mensagem para a API e imprime a resposta da IA (só biblioteca padrão).

Uso:
  python scripts/chat.py --base https://localhost --key sk-... "Olá!"
  python scripts/chat.py "Olá!"                  # usa BASE, KEY e MODEL do ambiente
  python scripts/chat.py --stream "Conte até 5"  # resposta em streaming (SSE)
  python scripts/chat.py                         # modo interativo (mantém o histórico)

Use --insecure (ou INSECURE=1) enquanto a CA interna do Caddy não estiver instalada.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request


def post_chat(args, messages):
    body = {"model": args.model, "messages": messages, "stream": args.stream}
    req = urllib.request.Request(
        f"{args.base.rstrip('/')}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl._create_unverified_context() if args.insecure else None
    return urllib.request.urlopen(req, timeout=args.timeout, context=ctx)


def read_response(resp, stream):
    """Imprime a resposta conforme chega e devolve (texto, uso de tokens)."""
    if not stream:
        data = json.load(resp)
        text = data["choices"][0]["message"]["content"]
        print(text, flush=True)
        return text, data.get("usage")

    parts, usage = [], None
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        chunk = json.loads(payload)
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices", []):
            piece = (choice.get("delta") or {}).get("content") or ""
            parts.append(piece)
            print(piece, end="", flush=True)
    print(flush=True)
    return "".join(parts), usage


def ask(args, messages):
    start = time.monotonic()
    try:
        with post_chat(args, messages) as resp:
            text, usage = read_response(resp, args.stream)
    except urllib.error.HTTPError as e:
        print(f"ERRO HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERRO de conexão: {getattr(e, 'reason', e)}", file=sys.stderr)
        return None

    info = f"[{time.monotonic() - start:.1f}s"
    if usage:
        info += f", tokens: {usage.get('prompt_tokens')} entrada / {usage.get('completion_tokens')} saída"
    print(info + "]", file=sys.stderr)
    return text


def main():
    p = argparse.ArgumentParser(description="Testa a API enviando uma mensagem para a IA.")
    p.add_argument("message", nargs="?", help="mensagem; se omitida, abre o modo interativo")
    p.add_argument("--base", default=os.environ.get("BASE", "https://localhost"), help="URL base (env BASE)")
    p.add_argument("--key", default=os.environ.get("KEY"), help="chave sk-... (env KEY)")
    p.add_argument("--model", default=os.environ.get("MODEL"), help="nome público do modelo (env MODEL)")
    p.add_argument("--system", help="prompt de sistema opcional")
    p.add_argument("--stream", action="store_true", help="recebe a resposta em streaming")
    p.add_argument("--timeout", type=float, default=300, help="timeout em segundos (padrão 300)")
    p.add_argument("--insecure", action="store_true", default=os.environ.get("INSECURE") == "1",
                   help="não valida o certificado TLS (env INSECURE=1)")
    args = p.parse_args()

    for s in (sys.stdout, sys.stderr, sys.stdin):
        s.reconfigure(encoding="utf-8")  # acentos corretos no console do Windows

    if not args.key:
        p.error("informe a chave com --key ou pela variável de ambiente KEY")
    if not args.model:
        p.error("informe o modelo com --model ou pela variável de ambiente MODEL (GET /v1/models lista os liberados)")

    messages = [{"role": "system", "content": args.system}] if args.system else []

    if args.message:
        messages.append({"role": "user", "content": args.message})
        sys.exit(0 if ask(args, messages) is not None else 1)

    print(f"Conversando com {args.model} em {args.base}. Ctrl+C ou 'sair' para encerrar.", file=sys.stderr)
    while True:
        try:
            user = input("\nvocê> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user.lower() in ("sair", "exit", "quit"):
            break
        if not user:
            continue
        messages.append({"role": "user", "content": user})
        print("ia> ", end="", flush=True)
        reply = ask(args, messages)
        if reply is None:
            messages.pop()  # descarta a pergunta que falhou para não poluir o histórico
        else:
            messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
