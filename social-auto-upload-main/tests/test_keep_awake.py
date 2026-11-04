from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_execution_state_flags_are_forwarded():
    from utils import keep_awake

    calls = []

    def setter(flags):
        calls.append(flags)
        return 1

    assert keep_awake._set_execution_state(
        keep_awake.ES_CONTINUOUS | keep_awake.ES_SYSTEM_REQUIRED,
        setter,
    )
    assert calls == [keep_awake.ES_CONTINUOUS | keep_awake.ES_SYSTEM_REQUIRED]


def test_non_windows_platform_is_a_noop(monkeypatch):
    from utils import keep_awake

    monkeypatch.setattr(keep_awake, "_held", False)
    assert keep_awake.hold_system_awake("linux") is False
