from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xpd_report_agent.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
DEFAULT_TIMEOUT_SECONDS = 60


class LaunchError(RuntimeError):
    """Raised when launch configuration or process management fails."""


@dataclass(frozen=True)
class RuntimeConfig:
    env: dict[str, str]


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: list[str]
    health_url: str | None = None
    health_token: str | None = None


NEW_ENV_KEYS = {
    "HERMES_GATEWAY_HOST",
    "HERMES_GATEWAY_PORT",
    "HERMES_GATEWAY_API_KEY",
    "HERMES_GATEWAY_MODEL",
    "HERMES_GATEWAY_ALLOW_ALL_USERS",
    "HERMES_LLM_PROVIDER",
    "HERMES_LLM_MODEL",
    "HERMES_LLM_BASE_URL",
    "HERMES_LLM_API_MODE",
    "HERMES_LLM_API_KEY",
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
    "HERMES_BOOTSTRAP_ON_START",
    "HERMES_REQUIRE_LLM_API_KEY",
    "XPD_SESSION_SIGNING_SECRET",
    "XPD_SESSION_ENABLED",
    "XPD_MEMORY_ENABLED",
    "XPD_PERIODIC_REFLECTION_ENABLED",
    "XPD_FINAL_REFLECTION_ENABLED",
    "XPD_REFLECTION_INTERVAL",
    "XPD_SESSION_IDLE_MINUTES",
    "XPD_MEMORY_CHAR_LIMIT",
    "XPD_USER_CHAR_LIMIT",
    "XPD_MEMORY_CONSOLIDATION_RATIO",
    "XPD_REFLECTION_STATE_PATH",
    "FASTAPI_HOST",
    "FASTAPI_PORT",
    "FASTAPI_RELOAD",
}

DEFAULTS = {
    "HERMES_GATEWAY_HOST": "127.0.0.1",
    "HERMES_GATEWAY_PORT": "8642",
    "HERMES_GATEWAY_API_KEY": "dev-secret",
    "HERMES_GATEWAY_MODEL": "hermes-agent",
    "HERMES_GATEWAY_ALLOW_ALL_USERS": "true",
    "HERMES_LLM_PROVIDER": "custom",
    "HERMES_LLM_MODEL": "qwen3.7-max",
    "HERMES_LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "HERMES_LLM_API_MODE": "chat_completions",
    "HERMES_LLM_API_KEY": "",
    "HERMES_BOOTSTRAP_ON_START": "false",
    "HERMES_REQUIRE_LLM_API_KEY": "true",
    "XPD_SESSION_ENABLED": "true",
    "XPD_MEMORY_ENABLED": "true",
    "XPD_PERIODIC_REFLECTION_ENABLED": "true",
    "XPD_FINAL_REFLECTION_ENABLED": "true",
    "XPD_REFLECTION_INTERVAL": "3",
    "XPD_SESSION_IDLE_MINUTES": "30",
    "XPD_MEMORY_CHAR_LIMIT": "2200",
    "XPD_USER_CHAR_LIMIT": "1375",
    "XPD_MEMORY_CONSOLIDATION_RATIO": "0.8",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "root",
    "MYSQL_DATABASE": "taobao_reports_test",
    "FASTAPI_HOST": "127.0.0.1",
    "FASTAPI_PORT": "8000",
    "FASTAPI_RELOAD": "false",
}


def _fallback_dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values

        return {
            key: value
            for key, value in dotenv_values(path).items()
            if key and value is not None
        }
    except Exception:
        return _fallback_dotenv_values(path)


def normalize_env(raw_env: dict[str, str], *, root: Path = ROOT) -> RuntimeConfig:
    normalized = {
        key: value
        for key in NEW_ENV_KEYS
        if (value := raw_env.get(key)) is not None and value != ""
    }

    for key, value in DEFAULTS.items():
        normalized.setdefault(key, value)

    env = dict(os.environ)
    env.update(normalized)
    env.update(
        {
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": normalized["HERMES_GATEWAY_HOST"],
            "API_SERVER_PORT": normalized["HERMES_GATEWAY_PORT"],
            "API_SERVER_KEY": normalized["HERMES_GATEWAY_API_KEY"],
            "GATEWAY_ALLOW_ALL_USERS": normalized["HERMES_GATEWAY_ALLOW_ALL_USERS"],
            "LAUNCH_MANAGED": "true",
        }
    )
    return RuntimeConfig(env=env)


def load_runtime_config(*, root: Path = ROOT, environ: dict[str, str] | None = None) -> RuntimeConfig:
    raw_env = load_env_file(root / ".env")
    raw_env.update(load_env_file(root / "configs" / "local.env"))
    raw_env.update(os.environ if environ is None else environ)
    return normalize_env(raw_env, root=root)


class LaunchManager:
    def __init__(
        self,
        *,
        root: Path = ROOT,
        env: dict[str, str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        services: dict[str, ServiceSpec] | None = None,
        run_dir: Path | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.env = env or load_runtime_config(root=root).env
        self.timeout_seconds = timeout_seconds
        self.run_dir = run_dir or root / ".run"
        self.log_dir = log_dir or root / "logs"
        self.services = services or self._default_services()

    def _default_services(self) -> dict[str, ServiceSpec]:
        return {
            "hermes": ServiceSpec(
                name="hermes",
                command=[
                    "bash",
                    str(self.root / "scripts" / "services" / "hermes.sh"),
                    "run",
                ],
                health_url=(
                    f"http://{self.env['HERMES_GATEWAY_HOST']}:"
                    f"{self.env['HERMES_GATEWAY_PORT']}/v1/health"
                ),
                health_token=self.env["HERMES_GATEWAY_API_KEY"],
            ),
            "fastapi": ServiceSpec(
                name="fastapi",
                command=[
                    "bash",
                    str(self.root / "scripts" / "services" / "fastapi.sh"),
                    "run",
                ],
                health_url=(
                    f"http://{self.env['FASTAPI_HOST']}:"
                    f"{self.env['FASTAPI_PORT']}/health"
                ),
            ),
        }

    def names_for_target(self, target: str) -> list[str]:
        if target == "all":
            return ["hermes", "fastapi"]
        if target not in self.services:
            raise LaunchError(f"Unknown service: {target}.")
        return [target]

    def pid_file(self, name: str) -> Path:
        return self.run_dir / f"{name}.pid"

    def log_file(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def read_pid(self, name: str) -> int | None:
        path = self.pid_file(name)
        if not path.exists():
            return None
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            path.unlink(missing_ok=True)
            return None

    @staticmethod
    def is_process_running(pid: int) -> bool:
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def signal_process_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except PermissionError:
            os.kill(pid, sig)

    def probe(self, spec: ServiceSpec, *, timeout: float = 2.0) -> bool:
        if not spec.health_url:
            return False
        headers = {}
        if spec.health_token:
            headers["Authorization"] = f"Bearer {spec.health_token}"
        request = Request(spec.health_url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return 200 <= response.status < 300
        except HTTPError as exc:
            return 200 <= exc.code < 300
        except (OSError, URLError):
            return False

    def wait_for_health(self, name: str) -> bool:
        spec = self.services[name]
        if not spec.health_url:
            return True
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self.probe(spec):
                return True
            pid = self.read_pid(name)
            if pid is not None and not self.is_process_running(pid):
                return False
            time.sleep(0.5)
        return False

    def tail_log(self, name: str, *, max_chars: int = 2000) -> str:
        path = self.log_file(name)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]

    def start_service(self, name: str) -> bool:
        spec = self.services[name]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        pid = self.read_pid(name)
        if pid is not None:
            if self.is_process_running(pid):
                print(f"{name}: already running pid={pid}")
                return False
            self.pid_file(name).unlink(missing_ok=True)
            print(f"{name}: removed stale pid={pid}")

        if spec.health_url and self.probe(spec):
            raise LaunchError(
                f"{name}: health endpoint is already available but no launch PID exists. "
                "Stop the external process or use launch status to inspect managed services."
            )

        with self.log_file(name).open("ab") as log_handle:
            process = subprocess.Popen(
                spec.command,
                cwd=self.root,
                env=self.env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        self.pid_file(name).write_text(f"{process.pid}\n", encoding="utf-8")
        print(f"{name}: started pid={process.pid}")

        if spec.health_url:
            if not self.wait_for_health(name):
                self.stop_service(name)
                raise LaunchError(f"{name}: failed to become healthy.\n{self.tail_log(name)}")
        else:
            time.sleep(0.2)
            if process.poll() is not None:
                self.pid_file(name).unlink(missing_ok=True)
                raise LaunchError(f"{name}: process exited early.\n{self.tail_log(name)}")

        return True

    def stop_service(self, name: str) -> bool:
        pid = self.read_pid(name)
        if pid is None:
            print(f"{name}: not running")
            return False
        if not self.is_process_running(pid):
            self.pid_file(name).unlink(missing_ok=True)
            print(f"{name}: removed stale pid={pid}")
            return False

        try:
            self.signal_process_group(pid, signal.SIGTERM)
        except ProcessLookupError:
            self.pid_file(name).unlink(missing_ok=True)
            print(f"{name}: removed stale pid={pid}")
            return False

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.is_process_running(pid):
                self.pid_file(name).unlink(missing_ok=True)
                print(f"{name}: stopped pid={pid}")
                return True
            time.sleep(0.2)

        try:
            self.signal_process_group(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.pid_file(name).unlink(missing_ok=True)
        print(f"{name}: killed pid={pid}")
        return True

    def start(self, target: str = "all") -> None:
        started: list[str] = []
        try:
            for name in self.names_for_target(target):
                if self.start_service(name):
                    started.append(name)
        except Exception:
            for name in reversed(started):
                self.stop_service(name)
            raise

    def stop(self, target: str = "all") -> None:
        for name in reversed(self.names_for_target(target)):
            self.stop_service(name)

    def restart(self, target: str = "all") -> None:
        self.stop(target)
        self.start(target)

    def status(self, target: str = "all") -> None:
        for name in self.names_for_target(target):
            spec = self.services[name]
            pid = self.read_pid(name)
            if pid is None:
                print(f"{name}: stopped")
                continue
            if not self.is_process_running(pid):
                print(f"{name}: stale pid={pid}")
                continue
            healthy = self.probe(spec) if spec.health_url else True
            print(f"{name}: running pid={pid} healthy={'yes' if healthy else 'no'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage xpd-report-agent services.")
    parser.add_argument("command", choices=["start", "stop", "restart", "status"])
    parser.add_argument("service", nargs="?", default="all", choices=["all", "hermes", "fastapi"])
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("LAUNCH_STARTUP_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))),
        help="Startup health-check timeout in seconds.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_runtime_config(root=ROOT)
        manager = LaunchManager(root=ROOT, env=config.env, timeout_seconds=args.timeout)
        getattr(manager, args.command)(args.service)
    except LaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
