# Rede e TLS

## Portas

| Porta | Serviço | Exposição |
|---|---|---|
| 443 / 80 | Caddy | LAN/VPN interna (a 80 só redireciona para HTTPS) |
| 4000 | LiteLLM | **Somente `127.0.0.1` do host** (administração) |
| 11434 | Ollama | Não publicada; só na rede interna do compose |
| 5432 | Postgres | Não publicada |

## Firewall (ufw)

Libere a 443 (e a 80, para o redirecionamento) somente para a rede interna. Ajuste o CIDR:

```bash
sudo ufw default deny incoming
sudo ufw allow from 10.0.0.0/8 to any port 22 proto tcp     # SSH de administração
sudo ufw allow from 10.0.0.0/8 to any port 443 proto tcp
sudo ufw allow from 10.0.0.0/8 to any port 80 proto tcp
sudo ufw enable
```

> O Docker manipula o iptables diretamente e pode **ignorar regras do ufw** para portas publicadas. Valide de fora da máquina (`nmap`/`curl` de outro host) e, se necessário, aplique as regras na cadeia `DOCKER-USER`. Como só 80/443 estão publicados externamente, o risco já é pequeno.

## DNS

Aponte um nome interno (ex.: `gn100.empresa.local`) para o IP da GN100 e defina `SITE_ADDRESS` com esse nome no `.env`. O certificado do Caddy é emitido para esse nome.

## TLS

### Padrão: CA interna do Caddy (`tls internal`)

O Caddy gera uma CA própria e emite o certificado. Os clientes precisam **confiar nessa CA**, senão verão erro de certificado.

1. Exporte o certificado raiz:
   ```bash
   docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
   ```
2. Distribua o `caddy-root.crt` aos clientes e instale-o como raiz confiável:
   - **Debian/Ubuntu:** copie para `/usr/local/share/ca-certificates/caddy-root.crt` e rode `sudo update-ca-certificates`.
   - **Windows:** `certutil -addstore -f Root caddy-root.crt` (administrador).
   - **Python/requests, Node:** aponte `REQUESTS_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS` para o arquivo.
3. Não versione o `caddy-root.crt` nem a chave da CA (o volume `caddy-data` deve ter backup, veja [operacao.md](operacao.md)).

### Quando houver certificado corporativo

Ainda não se sabe se existe uma CA corporativa; verifique com o time de infraestrutura/segurança. Se houver:

1. Obtenha um certificado e chave para o nome de `SITE_ADDRESS`.
2. Monte-os no serviço `caddy` (novo volume em `docker-compose.yml`, ex.: `./certs:/certs:ro`).
3. No `Caddyfile`, troque `tls internal` por `tls /certs/gn100.crt /certs/gn100.key`.
4. `docker compose up -d caddy`. Os clientes que já confiam na CA corporativa não precisam de nada.

Se o Caddy alcançar uma ACME interna, também é possível usar `tls { issuer acme { dir <url> } }`.
