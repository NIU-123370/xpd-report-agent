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

    platform_toolsets = data.setdefault("platform_toolsets", {})
    platform_toolsets["api_server"] = ["db_query"]

    known_plugin_toolsets = data.setdefault("known_plugin_toolsets", {})
    api_server_toolsets = known_plugin_toolsets.setdefault("api_server", [])
    if "db_query" not in api_server_toolsets:
        api_server_toolsets.append("db_query")

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
        "model_configured": model_configured,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Hermes for the SQLite demo.")
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
