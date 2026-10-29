from __future__ import annotations

from interactive_login import InteractiveDouyinLogin, InteractiveLoginManager


def test_interactive_session_exposes_frame_and_status():
    session = InteractiveDouyinLogin("session-1", "测试抖音", None, timeout_seconds=60)
    session._publish_frame(b"jpeg-frame")

    frame, version = session.current_frame()
    snapshot = session.snapshot()

    assert frame == b"jpeg-frame"
    assert version == 1
    assert snapshot["status"] == "starting"
    assert snapshot["expires_in"] <= 60


def test_manager_does_not_autostart_real_browser(monkeypatch):
    started = []
    monkeypatch.setattr(
        InteractiveDouyinLogin,
        "start",
        lambda self: started.append(self.session_id),
    )
    manager = InteractiveLoginManager(max_sessions=1)

    session_id, session = manager.start("测试账号")

    assert manager.get(session_id) is session
    assert started == [session_id]
    assert manager.stop(session_id) is True
    assert session.stop_event.is_set()


def test_manager_rejects_more_than_active_limit(monkeypatch):
    monkeypatch.setattr(InteractiveDouyinLogin, "start", lambda self: None)
    manager = InteractiveLoginManager(max_sessions=1)
    manager.start("账号一")

    try:
        manager.start("账号二")
    except RuntimeError as exc:
        assert "过多" in str(exc)
    else:
        raise AssertionError("second active login should be rejected")


class _FakeMouse:
    def __init__(self):
        self.calls = []

    def move(self, x, y, steps=1):
        self.calls.append(("move", x, y, steps))

    def down(self):
        self.calls.append(("down",))

    def up(self):
        self.calls.append(("up",))

    def click(self, x, y):
        self.calls.append(("click", x, y))


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.evaluations = []

    def evaluate(self, script, argument):
        self.evaluations.append((script, argument))


def test_pointer_commands_use_pressed_coordinates_and_dom_fallback():
    session = InteractiveDouyinLogin("session-2", "测试抖音", None, timeout_seconds=60)
    page = _FakePage()

    session._apply_command(page, "mouse_down", {"x": 560, "y": 380})
    session._apply_command(page, "mouse_move", {"x": 561, "y": 380})
    session._apply_command(page, "mouse_up", {"x": 561, "y": 380})

    assert page.mouse.calls[:3] == [
        ("move", 560.0, 380.0, 1),
        ("down",),
        ("move", 561.0, 380.0, 2),
    ]
    assert page.mouse.calls[-2:] == [("move", 561.0, 380.0, 1), ("up",)]
    assert page.evaluations[-1][1] == [561.0, 380.0]


def test_text_input_updates_focused_element_without_keyboard_events():
    session = InteractiveDouyinLogin("session-3", "测试抖音", None, timeout_seconds=60)
    page = _FakePage()

    session._apply_command(page, "text", {"text": "13800138000"})

    assert page.evaluations[-1][1] == "13800138000"
