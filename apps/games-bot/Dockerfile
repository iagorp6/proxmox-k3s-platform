# Imagem do games-bot (ADR 0006): Python + discord.py, git/ssh para commitar a troca e kubectl para o
# scripts/switch-game.py. Construída e escaneada pelo workflow games-bot-image, publicada no GHCR.
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# kubectl: mesma versão e SHA-256 do platform/defaults.yaml (o render-games.py --check confere)
RUN curl -fsSLo /usr/local/bin/kubectl https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl \
 && echo "c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/kubectl

# O pip sai da imagem depois de instalar: o bot não precisa dele em runtime, e o SBOM que o pip traz
# (pip-*.dist-info/sboms) lista o certifi vendorizado, que o Trivy reprova a cada CVE de CA (GHSA-6q48-6c7w-9gw2).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && python -m pip uninstall -y pip

COPY bot.py /app/bot.py
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bot
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "/app/bot.py"]
