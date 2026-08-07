FROM ghcr.io/astral-sh/uv:0.8.8 AS uv

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/var/lib/xpd-report-agent \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system xpd-agent \
    && useradd --system --gid xpd-agent --home-dir "$HOME" --create-home xpd-agent

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY . /app

RUN uv sync --frozen --no-dev \
    && bash -c '. configs/hermes-runtime.lock; \
      mkdir -p "$HOME/.hermes"; \
      git clone "$HERMES_AGENT_REPOSITORY" "$HOME/.hermes/hermes-agent"; \
      git -C "$HOME/.hermes/hermes-agent" checkout --detach "$HERMES_AGENT_COMMIT"; \
      uv venv "$HOME/.hermes/hermes-agent/venv" --python 3.11; \
      uv pip install --python "$HOME/.hermes/hermes-agent/venv/bin/python" \
        -e "$HOME/.hermes/hermes-agent"; \
      requirements_file="$(mktemp)"; \
      uv export --only-group hermes-plugin --no-hashes --no-header \
        --output-file "$requirements_file"; \
      uv pip install --python "$HOME/.hermes/hermes-agent/venv/bin/python" \
        -r "$requirements_file"; \
      rm -f "$requirements_file"' \
    && chmod +x /app/deploy/docker/entrypoint.sh \
    && mkdir -p /app/data/report-files /app/logs /app/.run \
    && chown -R xpd-agent:xpd-agent "$HOME" /app/data /app/logs /app/.run

USER xpd-agent
EXPOSE 8000
VOLUME ["/var/lib/xpd-report-agent/.hermes", "/app/data/report-files"]

ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
