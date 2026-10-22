# Imagem do games-bot (ADR 0006): Python + discord.py, git/ssh para commitar a troca e kubectl para o
# scripts/switch-game.py. Construída e escaneada pelo workflow games-bot-image, publicada no GHCR.
FROM python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# kubectl: mesma versão e SHA-256 do platform/defaults.yaml (o render-games.py --check confere)
RUN curl -fsSLo /usr/local/bin/kubectl https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl \
 && echo "6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/kubectl

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY bot.py /app/bot.py
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bot
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "/app/bot.py"]
