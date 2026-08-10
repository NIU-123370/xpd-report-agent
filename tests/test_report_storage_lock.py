from __future__ import annotations

import multiprocessing
from pathlib import Path

from xpd_report_agent.hermes_plugin.db_query.report_export import (
    _artifact_storage_lock,
)


def _hold_storage_lock(root: str, entered, release) -> None:
    with _artifact_storage_lock(Path(root)):
        entered.set()
        release.wait(timeout=5)


def _enter_storage_lock(root: str, entered) -> None:
    with _artifact_storage_lock(Path(root)):
        entered.set()


def test_report_storage_lock_serializes_separate_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    first_entered = context.Event()
    release_first = context.Event()
    second_entered = context.Event()
    first = context.Process(
        target=_hold_storage_lock,
        args=(str(tmp_path), first_entered, release_first),
    )
    second = context.Process(
        target=_enter_storage_lock,
        args=(str(tmp_path), second_entered),
    )

    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert not second_entered.wait(timeout=0.2)

    release_first.set()
    assert second_entered.wait(timeout=2)
    first.join(timeout=2)
    second.join(timeout=2)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert (tmp_path / ".xpd-report-storage.lock").is_file()
