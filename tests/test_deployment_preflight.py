from __future__ import annotations

import pytest

from xpd_report_agent.runtime.deployment_preflight import (
    DeploymentPreflightError,
    deployment_preflight_issues,
    main,
    validate_deployment_environment,
)


def _valid_environment() -> dict[str, str]:
    return {
        "XPD_SERVICE_AUTH_ENABLED": "true",
        "XPD_IDENTITY_MODE": "user_id",
        "XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED": "false",
        "FASTAPI_RELOAD": "false",
        "HERMES_LLM_PROVIDER": "custom",
        "HERMES_LLM_MODEL": "qwen3.7-max",
        "HERMES_LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "HERMES_LLM_API_MODE": "chat_completions",
        "HERMES_LLM_API_KEY": "model-api-key-for-production",
        "HERMES_GATEWAY_API_KEY": "gateway-" + "g" * 40,
        "XPD_SERVICE_API_KEY": "service-" + "s" * 40,
        "XPD_SESSION_SIGNING_SECRET": "session-" + "x" * 40,
        "XPD_DB_HOST": "mysql.internal",
        "XPD_DB_PORT": "3306",
        "XPD_DB_NAME": "main_biz_dev",
        "XPD_DB_USERNAME": "report_reader",
        "XPD_DB_PASSWORD": "database-password",
        "XPD_REPORT_OSS_ENABLED": "true",
        "XPD_REPORT_OSS_ENDPOINT": "https://oss-cn-beijing.aliyuncs.com",
        "XPD_REPORT_OSS_REGION": "cn-beijing",
        "XPD_REPORT_OSS_BUCKET": "report-bucket",
        "XPD_REPORT_OSS_PREFIX": "reports/dev",
        "XPD_REPORT_OSS_ACCESS_KEY_ID": "oss-access-key-id",
        "XPD_REPORT_OSS_ACCESS_KEY_SECRET": "oss-access-key-secret",
    }


def test_valid_production_environment_passes():
    env = _valid_environment()

    assert deployment_preflight_issues(env) == ()
    validate_deployment_environment(env)


@pytest.mark.parametrize("configured", [None, "false", "sometimes"])
def test_service_auth_must_be_explicitly_enabled(configured):
    env = _valid_environment()
    if configured is None:
        env.pop("XPD_SERVICE_AUTH_ENABLED")
    else:
        env["XPD_SERVICE_AUTH_ENABLED"] = configured

    issues = deployment_preflight_issues(env)

    assert "XPD_SERVICE_AUTH_ENABLED must be explicitly set to true." in issues


def test_middle_platform_identity_mode_is_required():
    env = _valid_environment()
    env["XPD_IDENTITY_MODE"] = "session_key"

    issues = deployment_preflight_issues(env)

    assert "XPD_IDENTITY_MODE must be set to user_id for middle-platform deployment." in issues


def test_unsafe_cross_user_session_search_is_rejected():
    env = _valid_environment()
    env["XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED"] = "true"

    issues = deployment_preflight_issues(env)

    assert (
        "XPD_UNSAFE_USER_SESSION_SEARCH_ENABLED must be false for multi-user deployment."
        in issues
    )


def test_fastapi_reload_is_rejected_in_production():
    env = _valid_environment()
    env["FASTAPI_RELOAD"] = "true"

    issues = deployment_preflight_issues(env)

    assert "FASTAPI_RELOAD must be false for production deployment." in issues


def test_hermes_node_requires_an_isolated_instance_home():
    env = _valid_environment()
    env.update(
        {
            "XPD_CONTAINER_ROLE": "hermes",
            "XPD_HERMES_NODE_ID": "hermes-2",
            "HERMES_HOME": "/data/hermes",
            "XPD_HERMES_SHARED_HOME": "/data/hermes",
            "XPD_MEMORY_ROOT": "/data/hermes/memories",
        }
    )

    issues = deployment_preflight_issues(env)

    assert (
        "Each Hermes node must use an isolated HERMES_HOME; "
        "it must not equal XPD_HERMES_SHARED_HOME."
    ) in issues


def test_hermes_node_accepts_isolated_home_with_shared_memory_root():
    env = _valid_environment()
    env.update(
        {
            "XPD_CONTAINER_ROLE": "hermes",
            "XPD_HERMES_NODE_ID": "hermes-2",
            "HERMES_HOME": "/data/hermes/instances/hermes-2",
            "XPD_HERMES_SHARED_HOME": "/data/hermes",
            "XPD_MEMORY_ROOT": "/data/hermes/memories",
        }
    )

    assert deployment_preflight_issues(env) == ()


@pytest.mark.parametrize(
    "name",
    [
        "HERMES_LLM_PROVIDER",
        "HERMES_LLM_MODEL",
        "HERMES_LLM_BASE_URL",
        "HERMES_LLM_API_MODE",
        "HERMES_LLM_API_KEY",
    ],
)
def test_complete_model_configuration_is_required(name):
    env = _valid_environment()
    env.pop(name)

    issues = deployment_preflight_issues(env)

    assert f"{name} is required." in issues


@pytest.mark.parametrize(
    "name",
    [
        "HERMES_LLM_PROVIDER",
        "HERMES_LLM_MODEL",
        "HERMES_LLM_BASE_URL",
        "HERMES_LLM_API_MODE",
        "HERMES_LLM_API_KEY",
    ],
)
def test_model_configuration_placeholders_are_rejected(name):
    env = _valid_environment()
    env[name] = "REPLACE_WITH_MODEL_VALUE"

    issues = deployment_preflight_issues(env)

    assert f"{name} still contains a deployment placeholder." in issues


@pytest.mark.parametrize(
    "name",
    [
        "HERMES_GATEWAY_API_KEY",
        "XPD_SERVICE_API_KEY",
        "XPD_SESSION_SIGNING_SECRET",
    ],
)
@pytest.mark.parametrize("value", ["REPLACE_WITH_SECRET", "dev-secret"])
def test_deployment_secret_placeholders_are_rejected(name, value):
    env = _valid_environment()
    env[name] = value

    issues = deployment_preflight_issues(env)

    assert f"{name} still contains a deployment placeholder." in issues


@pytest.mark.parametrize(
    "name",
    [
        "HERMES_GATEWAY_API_KEY",
        "XPD_SERVICE_API_KEY",
        "XPD_SESSION_SIGNING_SECRET",
    ],
)
def test_deployment_secrets_require_reasonable_length(name):
    env = _valid_environment()
    env[name] = "s" * 31

    issues = deployment_preflight_issues(env)

    assert f"{name} must contain at least 32 characters." in issues


def test_deployment_secrets_reject_leading_or_trailing_whitespace():
    env = _valid_environment()
    env["XPD_SERVICE_API_KEY"] = f" {env['XPD_SERVICE_API_KEY']} "

    issues = deployment_preflight_issues(env)

    assert "XPD_SERVICE_API_KEY must not contain whitespace." in issues


def test_deployment_secrets_must_be_distinct():
    env = _valid_environment()
    env["XPD_SERVICE_API_KEY"] = env["HERMES_GATEWAY_API_KEY"]

    issues = deployment_preflight_issues(env)

    assert (
        "HERMES_GATEWAY_API_KEY, XPD_SERVICE_API_KEY and "
        "XPD_SESSION_SIGNING_SECRET must be different secrets."
    ) in issues


@pytest.mark.parametrize(
    "name",
    [
        "XPD_DB_HOST",
        "XPD_DB_PORT",
        "XPD_DB_NAME",
        "XPD_DB_USERNAME",
        "XPD_DB_PASSWORD",
    ],
)
def test_database_placeholders_are_rejected(name):
    env = _valid_environment()
    env[name] = "REPLACE_WITH_DATABASE_VALUE"

    issues = deployment_preflight_issues(env)

    assert any("still contains a deployment placeholder" in issue for issue in issues)


@pytest.mark.parametrize(
    "name",
    [
        "XPD_REPORT_OSS_ENDPOINT",
        "XPD_REPORT_OSS_REGION",
        "XPD_REPORT_OSS_BUCKET",
        "XPD_REPORT_OSS_PREFIX",
        "XPD_REPORT_OSS_ACCESS_KEY_ID",
        "XPD_REPORT_OSS_ACCESS_KEY_SECRET",
    ],
)
def test_enabled_oss_configuration_rejects_placeholders(name):
    env = _valid_environment()
    env[name] = "REPLACE_WITH_OSS_VALUE"

    issues = deployment_preflight_issues(env)

    assert f"{name} still contains a deployment placeholder." in issues


def test_disabled_oss_does_not_require_credentials():
    env = _valid_environment()
    env["XPD_REPORT_OSS_ENABLED"] = "false"
    for _label, names in (
        ("endpoint", ("XPD_REPORT_OSS_ENDPOINT",)),
        ("region", ("XPD_REPORT_OSS_REGION",)),
        ("bucket", ("XPD_REPORT_OSS_BUCKET",)),
        ("prefix", ("XPD_REPORT_OSS_PREFIX",)),
        ("access key", ("XPD_REPORT_OSS_ACCESS_KEY_ID",)),
        ("secret", ("XPD_REPORT_OSS_ACCESS_KEY_SECRET",)),
    ):
        for name in names:
            env.pop(name)

    validate_deployment_environment(env)


def test_validation_error_and_cli_output_do_not_expose_secret_values(capsys):
    env = _valid_environment()
    leaked_value = "short-secret-value"
    env["XPD_SERVICE_API_KEY"] = leaked_value

    with pytest.raises(DeploymentPreflightError) as caught:
        validate_deployment_environment(env)

    assert leaked_value not in str(caught.value)
    assert main([], env=env) == 1
    captured = capsys.readouterr()
    assert leaked_value not in captured.err
    assert "XPD_SERVICE_API_KEY must contain at least 32 characters." in captured.err


def test_cli_success_message_and_quiet_mode(capsys):
    env = _valid_environment()

    assert main([], env=env) == 0
    assert capsys.readouterr().out == "Deployment preflight passed.\n"

    assert main(["--quiet"], env=env) == 0
    assert capsys.readouterr().out == ""
