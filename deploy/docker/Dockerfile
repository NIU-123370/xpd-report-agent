FROM public.ecr.aws/docker/library/python:3.11-slim AS python311

FROM ghcr.io/astral-sh/uv:0.8.8 AS uv

FROM public.ecr.aws/docker/library/python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/var/lib/xpd-report-agent \
    HERMES_AGENT_DIR=/opt/hermes-agent \
    HERMES_BIN=/opt/hermes-agent/venv/bin/hermes \
    HERMES_PY=/opt/hermes-agent/venv/bin/python \
    PATH=/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/var/cache/uv \
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

# Keep dependency and Hermes layers independent from normal source changes.
COPY pyproject.toml uv.lock /app/
COPY configs/hermes-runtime.lock /app/configs/hermes-runtime.lock

# Large binary wheels can exceed uv's 30-second default on restricted ECS links.
ENV UV_HTTP_TIMEOUT=300

RUN --mount=type=cache,target=/var/cache/uv \
    --mount=type=bind,source=deploy/docker/hermes-seed,target=/tmp/hermes-seed,ro \
    uv sync --frozen --no-dev --no-install-project \
    && bash -c 'set -euo pipefail; . configs/hermes-runtime.lock; \
      if [ -s /tmp/hermes-seed/hermes-agent.tar ]; then \
        tar -xf /tmp/hermes-seed/hermes-agent.tar -C /opt; \
      else \
        git init "$HERMES_AGENT_DIR"; \
        git -C "$HERMES_AGENT_DIR" remote add origin "$HERMES_AGENT_REPOSITORY"; \
        for attempt in 1 2 3; do \
          git -c http.version=HTTP/1.1 -C "$HERMES_AGENT_DIR" fetch \
            --depth 1 origin "$HERMES_AGENT_COMMIT" && break; \
          test "$attempt" -lt 3; \
          sleep 5; \
        done; \
        git -C "$HERMES_AGENT_DIR" checkout --detach FETCH_HEAD; \
      fi; \
      test "$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD)" = "$HERMES_AGENT_COMMIT"; \
      UV_PROJECT_ENVIRONMENT="$HERMES_AGENT_DIR/venv" uv sync \
        --project "$HERMES_AGENT_DIR" \
        --frozen \
        --no-dev \
        --python /usr/local/bin/python3.11; \
      plugin_requirements="$(mktemp)"; \
      hermes_constraints="$(mktemp)"; \
      trap '\''rm -f "$plugin_requirements" "$hermes_constraints"'\'' EXIT; \
      /usr/local/bin/python3.12 -c '\''import pathlib, sys, tomllib; data = tomllib.loads(pathlib.Path(sys.argv[1]).read_text()); pathlib.Path(sys.argv[2]).write_text("\n".join(data["dependency-groups"]["hermes-plugin"]) + "\n")'\'' /app/pyproject.toml "$plugin_requirements"; \
      uv pip check --python "$HERMES_PY"; \
      uv pip freeze --python "$HERMES_PY" --exclude-editable > "$hermes_constraints"; \
      uv pip install \
        --python "$HERMES_PY" \
        --constraints "$hermes_constraints" \
        -r "$plugin_requirements"; \
      uv pip check --python "$HERMES_PY"; \
      rm -f "$plugin_requirements" "$hermes_constraints"; \
      trap - EXIT; \
      test -f "$HERMES_AGENT_DIR/pyproject.toml"; \
      test -x "$HERMES_BIN"'

COPY . /app

RUN --mount=type=cache,target=/var/cache/uv \
    uv sync --frozen --no-dev \
    && chmod +x /app/deploy/docker/entrypoint.sh \
    && mkdir -p "$HOME/.hermes" /app/data/report-files /app/logs /app/.run \
    && chown -R xpd-agent:xpd-agent "$HOME" /app/data /app/logs /app/.run

USER xpd-agent
EXPOSE 8000
ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
