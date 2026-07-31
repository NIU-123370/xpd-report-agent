from __future__ import annotations

from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    """Locate the checkout root containing pyproject.toml."""
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate

    raise RuntimeError("Could not locate the xpd-report-agent project root.")


PROJECT_ROOT = find_project_root()

