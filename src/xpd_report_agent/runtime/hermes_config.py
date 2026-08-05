from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

MODEL_ENV_MAP = {
    "default": "HERMES_LLM_MODEL",
    "provider": "HERMES_LLM_PROVIDER",
    "base_url": "HERMES_LLM_BASE_URL",
    "api_mode": "HERMES_LLM_API_MODE",
    "api_key": "HERMES_LLM_API_KEY",
}

API_SERVER_TOOLSETS = [
    "db_query",
    "report_file",
    "file",
    "session_search",
    "memory",
    "clarify",
]

IDENTITY_MODE_ENV = "XPD_IDENTITY_MODE"
UNSAFE_USER_SESSION_SEARCH_ENV = "XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def session_search_enabled_from_env() -> bool:
    """Return whether Hermes may expose its process-global session search.

    Hermes' native session_search currently has no owner-scope filter. Keep it
    available for the single-user, local session-key workflow, but fail closed
    when the middle-platform user_id identity mode is active. The compatibility
    switch is deliberately named unsafe so enabling it is an explicit decision.
    """

    identity_mode = os.getenv(IDENTITY_MODE_ENV, "session_key").strip().lower()
    if identity_mode == "session_key":
        return True
    if identity_mode == "user_id":
        return _env_bool(UNSAFE_USER_SESSION_SEARCH_ENV, False)
    return False


def api_server_toolsets_from_env() -> list[str]:
    toolsets = list(API_SERVER_TOOLSETS)
    if not session_search_enabled_from_env():
        toolsets.remove("session_search")
    return toolsets


def required_memory_tools_from_env() -> list[str]:
    tools = ["memory"]
    if session_search_enabled_from_env():
        tools.insert(0, "session_search")
    return tools


def memory_config_from_env() -> dict:
    periodic_reflection_enabled = _env_bool("XPD_PERIODIC_REFLECTION_ENABLED", True)
    interval = max(1, _env_int("XPD_REFLECTION_INTERVAL", 3))
    return {
        "memory_enabled": _env_bool("XPD_MEMORY_ENABLED", True),
        "user_profile_enabled": _env_bool("XPD_MEMORY_ENABLED", True),
        "memory_char_limit": max(256, _env_int("XPD_MEMORY_CHAR_LIMIT", 2200)),
        "user_char_limit": max(256, _env_int("XPD_USER_CHAR_LIMIT", 1375)),
        "nudge_interval": interval if periodic_reflection_enabled else 0,
        "flush_min_turns": interval,
    }


def model_config_from_env() -> dict[str, str]:
    return {
        config_key: value
        for config_key, env_name in MODEL_ENV_MAP.items()
        if (value := os.environ.get(env_name))
    }


def _apply_model_config(
    data: dict,
    model_config: dict[str, str] | None,
    *,
    require_model_key: bool,
) -> bool:
    if not model_config:
        model_config = {}

    model = data.setdefault("model", {})
    for key in ("default", "provider", "base_url", "api_mode"):
        value = model_config.get(key)
        if value:
            model[key] = value

    api_key = model_config.get("api_key")
    if api_key:
        model["api_key"] = api_key

    if require_model_key and not model.get("api_key"):
        raise RuntimeError(
            "Hermes model api_key is not configured. Set HERMES_LLM_API_KEY "
            "in configs/local.env or keep an existing model.api_key in "
            "~/.hermes/config.yaml."
        )

    return bool(model)


def configure_config(
    config_path: Path,
    *,
    model_config: dict[str, str] | None = None,
    memory_config: dict | None = None,
    require_model_key: bool = False,
) -> dict:
    config_path = config_path.expanduser().resolve()
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        data = {}

    plugins = data.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    disabled = plugins.setdefault("disabled", [])
    if "db-query" not in enabled:
        enabled.append("db-query")
    if "db-query" in disabled:
        plugins["disabled"] = [item for item in disabled if item != "db-query"]

    api_server_toolsets = api_server_toolsets_from_env()
    platform_toolsets = data.setdefault("platform_toolsets", {})
    platform_toolsets["api_server"] = api_server_toolsets

    known_plugin_toolsets = data.setdefault("known_plugin_toolsets", {})
    known_api_server_toolsets = known_plugin_toolsets.setdefault("api_server", [])
    for plugin_toolset in ("db_query", "report_file"):
        if plugin_toolset not in known_api_server_toolsets:
            known_api_server_toolsets.append(plugin_toolset)

    resolved_memory_config = (
        memory_config if memory_config is not None else memory_config_from_env()
    )
    memory = data.setdefault("memory", {})
    memory.update(resolved_memory_config)

    timezone = os.getenv("HERMES_TIMEZONE", "Asia/Shanghai").strip()
    if timezone:
        data["timezone"] = timezone
    cron = data.setdefault("cron", {})
    cron["max_parallel_jobs"] = max(
        1,
        _env_int("XPD_CRON_MAX_PARALLEL_JOBS", 1),
    )

    model_configured = _apply_model_config(
        data,
        model_config if model_config is not None else model_config_from_env(),
        require_model_key=require_model_key,
    )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    return {
        "config_path": str(config_path),
        "plugins_enabled": plugins["enabled"],
        "api_server_toolsets": platform_toolsets["api_server"],
        "required_memory_tools": required_memory_tools_from_env(),
        "session_search_enabled": "session_search" in api_server_toolsets,
        "memory": memory,
        "timezone": data.get("timezone"),
        "cron": cron,
        "model_configured": model_configured,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Hermes for the MySQL report agent.")
    parser.add_argument(
        "--config",
        default="~/.hermes/config.yaml",
        help="Hermes config path. Defaults to ~/.hermes/config.yaml.",
    )
    parser.add_argument(
        "--require-model-key",
        action="store_true",
        help="Fail if model.api_key is absent after applying HERMES_LLM_* env.",
    )
    args = parser.parse_args()

    result = configure_config(
        Path(args.config),
        require_model_key=args.require_model_key,
    )
    print(f"Configured Hermes: {result['config_path']}")
    print(f"Enabled plugins: {', '.join(result['plugins_enabled'])}")
    print(f"api_server toolsets: {', '.join(result['api_server_toolsets'])}")
    print(f"model configured: {result['model_configured']}")


if __name__ == "__main__":
    main()
