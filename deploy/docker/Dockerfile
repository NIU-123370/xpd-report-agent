FROM public.ecr.aws/docker/library/python:3.11-slim AS python311

FROM ghcr.io/astral-sh/uv:0.8.8 AS uv

FROM public.ecr.aws/docker/library/python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/var/lib/xpd-report-agent \
    PATH=/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/

RUN sed -i "s@deb.debian.org@mirrors.aliyun.com@g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git fonts-wqy-zenhei passwd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system xpd-agent \
    && useradd --system --gid xpd-agent --home-dir "$HOME" --create-home xpd-agent

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=python311 /usr/local/bin/python3.11 /usr/local/bin/python3.11
COPY --from=python311 /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=python311 /usr/local/lib/libpython3.11.so.1.0 /usr/local/lib/libpython3.11.so.1.0
COPY --from=python311 /usr/local/include/python3.11 /usr/local/include/python3.11
RUN ldconfig
WORKDIR /app
COPY . /app

RUN uv lock --refresh \
    && uv sync --frozen --no-dev \
    && bash -c 'set -euo pipefail; . configs/hermes-runtime.lock; \
      mkdir -p "$HOME/.hermes"; \
      git clone "$HERMES_AGENT_REPOSITORY" "$HOME/.hermes/hermes-agent"; \
      git -C "$HOME/.hermes/hermes-agent" checkout --detach "$HERMES_AGENT_COMMIT"; \
      uv venv "$HOME/.hermes/hermes-agent/venv" \
        --python /usr/local/bin/python3.11; \
      uv pip install --python "$HOME/.hermes/hermes-agent/venv/bin/python" \
        -e "$HOME/.hermes/hermes-agent"; \
      requirements_file="$(mktemp)"; \
      uv export --only-group hermes-plugin --no-hashes --no-header \
        --output-file "$requirements_file"; \
      uv pip install --python "$HOME/.hermes/hermes-agent/venv/bin/python" \
        -r "$requirements_file"; \
      rm -f "$requirements_file"; \
      test -f "$HOME/.hermes/hermes-agent/pyproject.toml"; \
      test -x "$HOME/.hermes/hermes-agent/venv/bin/hermes"' \
    && chmod +x /app/deploy/docker/entrypoint.sh \
    && mkdir -p /app/data/report-files /app/logs /app/.run \
    && chown -R xpd-agent:xpd-agent "$HOME" /app/data /app/logs /app/.run

USER xpd-agent
EXPOSE 8000
ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
