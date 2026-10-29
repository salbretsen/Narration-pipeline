# Audiobook Studio container image (mock / CPU path only; see docs/12-container.md).
# Upgrades are by image tag: pull or build a new tag and recreate the container. There is no in-container self-update.
# Build from the repository root:  docker build -t audiobook-studio:<version> .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    AUDIOBOOK_STUDIO_HOME=/data \
    STUDIO_ENGINE=mock \
    HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 studio \
 && mkdir /data && chown studio:studio /data

WORKDIR /opt/studio

# Dependencies first (cached layer). requirements.txt lists only the portable packages; requirements-mlx.txt
# (mlx-audio, mlx-whisper: Apple Silicon only) is deliberately NOT installed, so requirements.lock is not used here.
COPY src/app/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY src/app/studio ./studio
COPY docker/entrypoint.sh /usr/local/bin/studio-entrypoint
RUN chmod 0755 /usr/local/bin/studio-entrypoint

USER studio
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/auth/status', timeout=4)" || exit 1
ENTRYPOINT ["studio-entrypoint"]
