FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Sem privilégios de root no container.
RUN useradd --system --uid 10001 painel
USER painel

EXPOSE 8080
# Um único processo: as rotinas periódicas rodam em uma thread dele.
# --forwarded-allow-ips: o painel só é alcançável pelo Caddy (rede do compose) e pelo loopback do host.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
