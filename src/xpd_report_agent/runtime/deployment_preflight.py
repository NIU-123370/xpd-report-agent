from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence

MIN_SECRET_LENGTH = 32

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MODEL_CONFIG_NAMES = (
    "HERMES_LLM_PROVIDER",
    "HERMES_LLM_MODEL",
    "HERMES_LLM_BASE_URL",
    "HERMES_LLM_API_MODE",
    "HERMES_LLM_API_KEY",
)
_SECRET_NAMES = (
    "HERMES_GATEWAY_API_KEY",
    "XPD_SERVICE_API_KEY",
    "XPD_SESSION_SIGNING_SECRET",
)
_DATABASE_FIELDS = (
    ("MYSQL_HOST/XPD_DB_HOST", ("MYSQL_HOST", "XPD_DB_HOST")),
    ("MYSQL_PORT/XPD_DB_PORT", ("MYSQL_PORT", "XPD_DB_PORT")),
    ("MYSQL_DATABASE/XPD_DB_NAME", ("MYSQL_DATABASE", "XPD_DB_NAME")),
    ("MYSQL_USER/XPD_DB_USERNAME", ("MYSQL_USER", "XPD_DB_USERNAME")),
    ("MYSQL_PASSWORD/XPD_DB_PASSWORD", ("MYSQL_PASSWORD", "XPD_DB_PASSWORD")),
)
_OSS_FIELDS = (
    ("XPD_REPORT_OSS_ENDPOINT", ("XPD_REPORT_OSS_ENDPOINT", "XPD_OSS_ENDPOINT")),
    ("XPD_REPORT_OSS_REGION", ("XPD_REPORT_OSS_REGION",)),
    ("XPD_REPORT_OSS_BUCKET", ("XPD_REPORT_OSS_BUCKET",)),
    ("XPD_REPORT_OSS_PREFIX", ("XPD_REPORT_OSS_PREFIX",)),
    (
        "XPD_REPORT_OSS_ACCESS_KEY_ID",
        ("XPD_REPORT_OSS_ACCESS_KEY_ID", "XPD_OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID"),
    ),
    (
        "XPD_REPORT_OSS_ACCESS_KEY_SECRET",
        (
            "XPD_REPORT_OSS_ACCESS_KEY_SECRET",
            "XPD_OSS_ACCESS_KEY_SECRET",
            "OSS_ACCESS_KEY_SECRET",
        ),
    ),
)


class DeploymentPreflightError(RuntimeError):
    """Raised when production deployment configuration is unsafe or incomplete."""

    def __init__(self, issues: Sequence[str]):
        self.issues = tuple(issues)
        super().__init__("Deployment preflight failed:\n" + "\n".join(f"- {item}" for item in issues))


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "")).strip()


def _first_value(env: Mapping[str, str], names: Sequence[str]) -> str:
    for name in names:
        value = _value(env, name)
        if value:
            return value
    return ""


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized.startswith("replace_with") or normalized == "dev-secret"


def _required_non_placeholder(
    env: Mapping[str, str],
    *,
    label: str,
    names: Sequence[str],
    issues: list[str],
) -> str:
    value = _first_value(env, names)
    if not value:
        issues.append(f"{label} is required.")
    elif _is_placeholder(value):
        issues.append(f"{label} still contains a deployment placeholder.")
    return value


def _configured_bool(
    env: Mapping[str, str],
    name: str,
    *,
    default: bool | None,
    issues: list[str],
) -> bool | None:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    issues.append(f"{name} must be a boolean value.")
    return None


def deployment_preflight_issues(
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return secret-free validation errors for one production deployment environment."""

    environment = os.environ if env is None else env
    issues: list[str] = []

    auth_enabled = _configured_bool(
        environment,
        "XPD_SERVICE_AUTH_ENABLED",
        default=None,
        issues=issues,
    )
    if auth_enabled is not True:
        issues.append("XPD_SERVICE_AUTH_ENABLED must be explicitly set to true.")

    if _value(environment, "XPD_IDENTITY_MODE").casefold() != "user_id":
        issues.append("XPD_IDENTITY_MODE must be set to user_id for middle-platform deployment.")

    unsafe_session_search = _configured_bool(
        environment,
        "XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED",
        default=False,
        issues=issues,
    )
    if unsafe_session_search:
        issues.append(
            "XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED must be false for multi-user deployment."
        )

    reload_enabled = _configured_bool(
        environment,
        "FASTAPI_RELOAD",
        default=False,
        issues=issues,
    )
    if reload_enabled:
        issues.append("FASTAPI_RELOAD must be false for production deployment.")

    for name in _MODEL_CONFIG_NAMES:
        _required_non_placeholder(
            environment,
            label=name,
            names=(name,),
            issues=issues,
        )

    secret_values: dict[str, str] = {}
    for name in _SECRET_NAMES:
        raw_value = str(environment.get(name, ""))
        value = _required_non_placeholder(
            environment,
            label=name,
            names=(name,),
            issues=issues,
        )
        secret_values[name] = value
        if value and not _is_placeholder(value) and len(value) < MIN_SECRET_LENGTH:
            issues.append(f"{name} must contain at least {MIN_SECRET_LENGTH} characters.")
        if raw_value and any(character.isspace() for character in raw_value):
            issues.append(f"{name} must not contain whitespace.")

    populated_secrets = {
        name: value for name, value in secret_values.items() if value and not _is_placeholder(value)
    }
    if len(populated_secrets) == len(_SECRET_NAMES) and len(set(populated_secrets.values())) != len(
        populated_secrets
    ):
        issues.append(
            "HERMES_GATEWAY_API_KEY, XPD_SERVICE_API_KEY and "
            "XPD_SESSION_SIGNING_SECRET must be different secrets."
        )

    database_values: dict[str, str] = {}
    for label, names in _DATABASE_FIELDS:
        database_values[label] = _required_non_placeholder(
            environment,
            label=label,
            names=names,
            issues=issues,
        )
    database_port = database_values["MYSQL_PORT/XPD_DB_PORT"]
    if database_port and not _is_placeholder(database_port):
        try:
            port = int(database_port)
        except ValueError:
            issues.append("MYSQL_PORT/XPD_DB_PORT must be an integer.")
        else:
            if not 1 <= port <= 65535:
                issues.append("MYSQL_PORT/XPD_DB_PORT must be between 1 and 65535.")

    oss_enabled = _configured_bool(
        environment,
        "XPD_REPORT_OSS_ENABLED",
        default=False,
        issues=issues,
    )
    if oss_enabled:
        for label, names in _OSS_FIELDS:
            _required_non_placeholder(
                environment,
                label=label,
                names=names,
                issues=issues,
            )

    return tuple(issues)


def validate_deployment_environment(env: Mapping[str, str] | None = None) -> None:
    """Fail closed when production deployment configuration is unsafe or incomplete."""

    issues = deployment_preflight_issues(env)
    if issues:
        raise DeploymentPreflightError(issues)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Validate xpd-report-agent production deployment configuration."
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print the success message.")
    args = parser.parse_args(argv)

    try:
        validate_deployment_environment(env)
    except DeploymentPreflightError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not args.quiet:
        print("Deployment preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
