import asyncio
import importlib.util
import logging
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest


class _StubPlaywrightTimeoutError(Exception):
    pass


class _StubLocator:
    def __init__(self) -> None:
        self.fill_calls: list[tuple[str, int | None]] = []
        self.click_calls: list[dict[str, object]] = []

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.fill_calls.append((value, timeout))

    async def click(self, timeout: int | None = None, **kwargs: object) -> None:
        self.click_calls.append({"timeout": timeout, **kwargs})


class _TimeoutFillLocator(_StubLocator):
    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.fill_calls.append((value, timeout))
        raise _StubPlaywrightTimeoutError("fill timed out")


class _StubPage:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, str, int]] = []
        self.name_field = _StubLocator()
        self.join_button = _StubLocator()

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    def get_by_placeholder(self, _pattern: object) -> _StubLocator:
        return self.name_field

    def get_by_role(self, _role: str, *, name: object) -> _StubLocator:
        return self.join_button


class _TimeoutPage(_StubPage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://accounts.google.com/signin"
        self.screenshot_calls: list[dict[str, object]] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        raise _StubPlaywrightTimeoutError("timed out")

    async def title(self) -> str:
        return "Sign in"

    async def content(self) -> str:
        return """
        <html>
          <body>
            Sign in to continue to Google Meet
          </body>
        </html>
        """

    async def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_calls.append(dict(kwargs))
        return b"png"


class _FillTimeoutPage(_StubPage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://meet.google.com/landing"
        self.name_field = _TimeoutFillLocator()
        self.screenshot_calls: list[dict[str, object]] = []

    async def title(self) -> str:
        return "Google Meet"

    async def content(self) -> str:
        return """
        <html>
          <body>
            Ready to join?
          </body>
        </html>
        """

    async def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_calls.append(dict(kwargs))
        return b"png"


class _JoinCheckFailurePage(_StubPage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://meet.google.com/landing"
        self.screenshot_calls: list[dict[str, object]] = []

    async def title(self) -> str:
        return "Google Meet"

    async def content(self) -> str:
        return """
        <html>
          <body>
            No one else is here yet
          </body>
        </html>
        """

    async def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_calls.append(dict(kwargs))
        return b"png"


class _DebugCapturePage(_StubPage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://meet.google.com/test-call"
        self.screenshot_calls: list[dict[str, object]] = []

    async def title(self) -> str:
        return "Meet"

    async def content(self) -> str:
        return """
        <html>
          <body>
            <main>
              <div>Ready to join</div>
            </main>
          </body>
        </html>
        """

    async def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_calls.append(dict(kwargs))
        return b"debug-png"


class _SlowGotoPage(_StubPage):
    def __init__(self, *, delay_seconds: float) -> None:
        super().__init__()
        self.delay_seconds = delay_seconds
        self.url = "https://meet.google.com/loading"
        self.screenshot_calls: list[dict[str, object]] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        await asyncio.sleep(self.delay_seconds)

    async def title(self) -> str:
        return "Loading"

    async def content(self) -> str:
        return """
        <html>
          <body>
            Loading Google Meet...
          </body>
        </html>
        """

    async def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_calls.append(dict(kwargs))
        return b"png"


class _SlowDiagnosticsPage(_SlowGotoPage):
    def __init__(
        self,
        *,
        goto_delay_seconds: float,
        diagnostics_delay_seconds: float,
    ) -> None:
        super().__init__(delay_seconds=goto_delay_seconds)
        self.diagnostics_delay_seconds = diagnostics_delay_seconds

    async def title(self) -> str:
        await asyncio.sleep(self.diagnostics_delay_seconds)
        return "Loading"

    async def content(self) -> str:
        await asyncio.sleep(self.diagnostics_delay_seconds)
        return """
        <html>
          <body>
            Slow diagnostics page
          </body>
        </html>
        """

    async def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_calls.append(dict(kwargs))
        await asyncio.sleep(self.diagnostics_delay_seconds)
        return b"png"


class _VisibilityLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self) -> "_VisibilityLocator":
        return self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        if not self._visible:
            raise _StubPlaywrightTimeoutError("not visible")

    async def is_visible(self) -> bool:
        return self._visible

    async def click(self, timeout: int | None = None) -> None:  # noqa: ARG002
        return None


class _SlowVisibilityLocator(_VisibilityLocator):
    def __init__(self, visible: bool, *, delay_seconds: float) -> None:
        super().__init__(visible)
        self._delay_seconds = delay_seconds

    async def is_visible(self) -> bool:
        await asyncio.sleep(self._delay_seconds)
        return await super().is_visible()


class _DialogStallLocator(_VisibilityLocator):
    def __init__(self, *, stall_seconds: float) -> None:
        super().__init__(True)
        self._stall_seconds = stall_seconds
        self.click_calls: list[int | None] = []

    async def click(self, timeout: int | None = None) -> None:
        self.click_calls.append(timeout)
        if timeout == 0:
            await asyncio.sleep(self._stall_seconds)
            return None
        raise _StubPlaywrightTimeoutError("dialog click timed out quickly")


class _JoinStatePage:
    # Active button aria-labels that map to the "active" meeting state.
    _ACTIVE_CONTROL_SUBSTRINGS = (
        "leave", "exit call", "hang up", "end call",
        "turn off mic", "turn on mic", "turn off camera", "turn on camera",
    )
    # Text fragments that map to the "waiting" meeting state.
    _WAITING_SUBSTRINGS = (
        "asking to be let in", "someone lets you in",
        "no one else is here yet", "you're the first one here",
        "waiting for the host", "waiting for others",
        "waiting for organizer", "meeting hasn't started",
        "meeting has not started",
    )
    # Text fragments that map to the "failed" meeting state.
    _FAILURE_SUBSTRINGS = (
        "you can't join this video call", "you can't join this meeting",
        "meeting code is invalid", "couldn't find the meeting",
        "this meeting has ended", "meeting has ended", "call has ended",
    )

    def __init__(
        self,
        *,
        url: str,
        html: str,
        visible_button_labels: tuple[str, ...] = (),
        preview_visible: bool = False,
    ) -> None:
        self.url = url
        self._html = html
        self._visible_button_labels = visible_button_labels
        self._preview_visible = preview_visible

    async def content(self) -> str:
        return self._html

    async def wait_for_timeout(self, timeout: int) -> None:  # noqa: ARG002
        await asyncio.sleep(0)

    def get_by_role(self, role: str, *, name: object) -> _VisibilityLocator:
        if role != "button":
            return _VisibilityLocator(False)

        if hasattr(name, "search"):
            visible = any(name.search(label) for label in self._visible_button_labels)
        else:
            visible = False
        return _VisibilityLocator(visible)

    def get_by_placeholder(self, _pattern: object) -> _VisibilityLocator:
        return _VisibilityLocator(self._preview_visible)

    def locator(self, selector: str) -> _VisibilityLocator:
        lowered_html = self._html.lower()
        lowered_selector = selector.lower()
        if "asking to be let in" in lowered_selector:
            return _VisibilityLocator("asking to be let in" in lowered_html)
        if 'aria-label^="someone lets you in"' in lowered_selector:
            return _VisibilityLocator("someone lets you in" in lowered_html)
        if 'div[role="dialog"] [data-mdc-dialog-action]' in lowered_selector:
            return _VisibilityLocator(False)
        return _VisibilityLocator(False)

    async def evaluate(self, _expression: object) -> str:  # noqa: ARG002
        """Simulate _classify_meeting_state JS using the stub's own fields."""
        lower = self._html.lower()

        for f in self._FAILURE_SUBSTRINGS:
            if f in lower:
                return "failed"

        if self._preview_visible:
            return "preview"

        for w in self._WAITING_SUBSTRINGS:
            if w in lower:
                return "waiting"

        for label in self._visible_button_labels:
            if any(a in label.lower() for a in self._ACTIVE_CONTROL_SUBSTRINGS):
                return "active"

        if not self._preview_visible:
            return "transitioning"

        return "unknown"


class _SlowJoinStatePage(_JoinStatePage):
    def __init__(
        self,
        *,
        url: str,
        html: str,
        delay_seconds: float,
    ) -> None:
        super().__init__(url=url, html=html)
        self._delay_seconds = delay_seconds

    def get_by_role(self, role: str, *, name: object) -> _VisibilityLocator:
        return _SlowVisibilityLocator(
            super().get_by_role(role, name=name)._visible,
            delay_seconds=self._delay_seconds,
        )

    def get_by_placeholder(self, pattern: object) -> _VisibilityLocator:
        return _SlowVisibilityLocator(
            super().get_by_placeholder(pattern)._visible,
            delay_seconds=self._delay_seconds,
        )

    async def evaluate(self, expression: object) -> str:
        """Slow variant: simulate an expensive evaluate() call on Pi."""
        await asyncio.sleep(self._delay_seconds)
        return await super().evaluate(expression)


class _DialogStallJoinStatePage(_JoinStatePage):
    def __init__(
        self,
        *,
        url: str,
        html: str,
        stall_seconds: float,
    ) -> None:
        super().__init__(url=url, html=html)
        self.dialog_locator = _DialogStallLocator(stall_seconds=stall_seconds)

    def locator(self, selector: str) -> _VisibilityLocator:
        lowered_selector = selector.lower()
        if "div[role='dialog'] [data-mdc-dialog-action]" == lowered_selector:
            return self.dialog_locator
        return super().locator(selector)


class _ClassifierScriptPage:
    def __init__(self) -> None:
        self.last_expression: object | None = None

    async def evaluate(self, expression: object) -> str:
        self.last_expression = expression
        script = str(expression)
        preview_idx = script.find("return 'preview'")
        active_idx = script.find("return 'active'")
        if preview_idx == -1 or (active_idx != -1 and preview_idx > active_idx):
            return "active"
        return "preview"


def _load_module(module_name: str, relative_path: list[str]) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[1].joinpath(*relative_path),
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_google_meet_module() -> ModuleType:
    playwright_module = ModuleType("playwright")
    playwright_async_api = ModuleType("playwright.async_api")
    playwright_async_api.Page = object
    playwright_async_api.TimeoutError = _StubPlaywrightTimeoutError
    sys.modules.setdefault("playwright", playwright_module)
    sys.modules["playwright.async_api"] = playwright_async_api
    playwright_module.async_api = playwright_async_api

    browser_pkg = ModuleType("joinly.providers.browser")
    browser_pkg.__path__ = []
    platforms_pkg = ModuleType("joinly.providers.browser.platforms")
    platforms_pkg.__path__ = []
    sys.modules["joinly.providers.browser"] = browser_pkg
    sys.modules["joinly.providers.browser.platforms"] = platforms_pkg

    _load_module(
        "joinly.providers.browser.platforms.base",
        ["joinly", "providers", "browser", "platforms", "base.py"],
    )
    return _load_module(
        "joinly.providers.browser.platforms.google_meet",
        ["joinly", "providers", "browser", "platforms", "google_meet.py"],
    )


def test_google_meet_join_uses_commit_navigation_and_waits_for_name_field(monkeypatch) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _StubPage()

    async def _check_joined(_page: object) -> bool:
        return True

    async def _setup_active_speaker_observer(_page: object) -> None:
        return None

    monkeypatch.setattr(controller, "_check_joined", _check_joined)
    monkeypatch.setattr(
        controller,
        "_setup_active_speaker_observer",
        _setup_active_speaker_observer,
    )

    asyncio.run(
        controller.join(
            page,
            "https://meet.google.com/test-call",
            name="OpenClaw",
        )
    )

    assert page.goto_calls == [
        ("https://meet.google.com/test-call", "commit", 20000)
    ]
    assert page.name_field.fill_calls == [("OpenClaw", 60000)]
    assert page.join_button.click_calls == [
        {"timeout": 10000, "force": True, "no_wait_after": True}
    ]


def test_google_meet_join_runs_configured_preflight_urls_before_target_navigation(
    monkeypatch,
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController(
        navigation_preflight_urls=(
            "https://www.google.com",
            "https://meet.google.com",
        )
    )
    page = _StubPage()

    async def _check_joined(_page: object) -> bool:
        return True

    async def _setup_active_speaker_observer(_page: object) -> None:
        return None

    monkeypatch.setattr(controller, "_check_joined", _check_joined)
    monkeypatch.setattr(
        controller,
        "_setup_active_speaker_observer",
        _setup_active_speaker_observer,
    )

    asyncio.run(
        controller.join(
            page,
            "https://meet.google.com/test-call",
            name="OpenClaw",
        )
    )

    assert page.goto_calls == [
        ("https://www.google.com", "domcontentloaded", 10000),
        ("https://meet.google.com", "domcontentloaded", 10000),
        ("https://meet.google.com/test-call", "commit", 20000),
    ]


def test_google_meet_join_logs_navigation_timeout_context(caplog) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _TimeoutPage()

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StubPlaywrightTimeoutError):
            asyncio.run(
                controller.join(
                    page,
                    "https://meet.google.com/test-call",
                    name="OpenClaw",
                )
            )

    assert page.goto_calls == [
        ("https://meet.google.com/test-call", "commit", 20000)
    ]
    assert page.screenshot_calls
    assert "Google Meet navigation timed out" in caplog.text
    assert "https://accounts.google.com/signin" in caplog.text
    assert "Sign in" in caplog.text
    assert "Sign in to continue to Google Meet" in caplog.text


def test_google_meet_join_logs_name_field_timeout_context(caplog) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _FillTimeoutPage()

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StubPlaywrightTimeoutError):
            asyncio.run(
                controller.join(
                    page,
                    "https://meet.google.com/test-call",
                    name="OpenClaw",
                )
            )

    assert page.goto_calls == [
        ("https://meet.google.com/test-call", "commit", 20000)
    ]
    assert page.name_field.fill_calls == [("OpenClaw", 60000)]
    assert page.screenshot_calls
    assert "Google Meet join step timed out" in caplog.text
    assert "step=name_field.fill" in caplog.text
    assert "https://meet.google.com/landing" in caplog.text
    assert "Ready to join?" in caplog.text


def test_google_meet_join_enforces_outer_navigation_timeout(monkeypatch, caplog) -> None:
    google_meet_module = _load_google_meet_module()
    monkeypatch.setattr(google_meet_module, "_NAVIGATION_STEP_DEADLINE_SECONDS", 0.01)

    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _SlowGotoPage(delay_seconds=0.05)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(TimeoutError):
            asyncio.run(
                controller.join(
                    page,
                    "https://meet.google.com/test-call",
                    name="OpenClaw",
                )
            )

    assert page.goto_calls == [
        ("https://meet.google.com/test-call", "commit", 20000)
    ]
    assert page.screenshot_calls
    assert "Google Meet navigation timed out" in caplog.text
    assert "Loading Google Meet..." in caplog.text


def test_google_meet_join_bounds_timeout_diagnostics(monkeypatch, caplog) -> None:
    google_meet_module = _load_google_meet_module()
    monkeypatch.setattr(google_meet_module, "_NAVIGATION_STEP_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(google_meet_module, "_DIAGNOSTIC_STEP_DEADLINE_SECONDS", 0.01)

    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _SlowDiagnosticsPage(
        goto_delay_seconds=0.05,
        diagnostics_delay_seconds=0.2,
    )

    started_at = time.monotonic()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(TimeoutError):
            asyncio.run(
                controller.join(
                    page,
                    "https://meet.google.com/test-call",
                    name="OpenClaw",
                )
            )
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.15
    assert "Google Meet navigation timed out" in caplog.text


def test_google_meet_join_logs_state_when_post_click_join_check_fails(
    monkeypatch, caplog
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinCheckFailurePage()

    async def _check_joined(_page: object) -> bool:
        return False

    monkeypatch.setattr(controller, "_check_joined", _check_joined)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="Join check failed"):
            asyncio.run(
                controller.join(
                    page,
                    "https://meet.google.com/test-call",
                    name="OpenClaw",
                )
            )

    assert page.screenshot_calls
    assert "Google Meet join did not reach active or waiting state" in caplog.text
    assert "No one else is here yet" in caplog.text


def test_google_meet_join_captures_debug_artifacts_when_configured(
    monkeypatch, tmp_path
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController(
        debug_artifact_dir=str(tmp_path)
    )
    page = _DebugCapturePage()

    async def _check_joined(_page: object) -> bool:
        return True

    async def _setup_active_speaker_observer(_page: object) -> None:
        return None

    monkeypatch.setattr(controller, "_check_joined", _check_joined)
    monkeypatch.setattr(
        controller,
        "_setup_active_speaker_observer",
        _setup_active_speaker_observer,
    )

    asyncio.run(
        controller.join(
            page,
            "https://meet.google.com/test-call",
            name="OpenClaw",
        )
    )

    assert page.screenshot_calls == [{"type": "png"}, {"type": "png"}]
    files = sorted(path.name for path in tmp_path.iterdir())
    assert len(files) == 6
    assert any(name.endswith("-pre_click.png") for name in files)
    assert any(name.endswith("-pre_click.html") for name in files)
    assert any(name.endswith("-pre_click.json") for name in files)
    assert any(name.endswith("-post_click.png") for name in files)
    assert any(name.endswith("-post_click.html") for name in files)
    assert any(name.endswith("-post_click.json") for name in files)


def test_google_meet_check_joined_accepts_waiting_room_text_state() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <main>
              <div>No one else is here yet</div>
            </main>
          </body>
        </html>
        """,
    )

    result = asyncio.run(controller._check_joined(page, timeout=0.01))

    assert result is True


def test_google_meet_classify_meeting_state_checks_preview_before_active_controls() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _ClassifierScriptPage()

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "preview"


def test_google_meet_check_joined_rejects_preview_state_even_with_mic_camera_controls() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <main>
              <div>What's your name?</div>
              <button>Ask to join</button>
            </main>
          </body>
        </html>
        """,
        visible_button_labels=("Turn off microphone", "Turn off camera"),
        preview_visible=True,
    )

    result = asyncio.run(controller._check_joined(page, timeout=0.01))

    assert result is False


def test_google_meet_check_joined_accepts_live_meet_url_after_preview_controls_disappear() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    # preview_visible=False and no active labels → evaluate() returns
    # "transitioning". With _TRANSITIONING_SETTLE_COUNT=3 polls and a generous
    # timeout the loop must stabilise and return True.
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body><main>Meet</main></body></html>",
        visible_button_labels=(),
        preview_visible=False,
    )

    result = asyncio.run(controller._check_joined(page, timeout=5.0))

    assert result is True


def test_google_meet_check_joined_rejects_terminal_failure_text_state() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <main>
              <div>You can't join this video call</div>
            </main>
          </body>
        </html>
        """,
    )

    result = asyncio.run(controller._check_joined(page, timeout=0.01))

    assert result is False


def test_google_meet_join_does_not_block_on_active_speaker_setup_timeout(
    monkeypatch, caplog
) -> None:
    google_meet_module = _load_google_meet_module()
    monkeypatch.setattr(
        google_meet_module,
        "_ACTIVE_SPEAKER_SETUP_TIMEOUT_SECONDS",
        0.01,
    )
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _StubPage()

    async def _check_joined(_page: object) -> bool:
        return True

    async def _setup_active_speaker_observer(_page: object) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(controller, "_check_joined", _check_joined)
    monkeypatch.setattr(
        controller,
        "_setup_active_speaker_observer",
        _setup_active_speaker_observer,
    )

    started_at = time.monotonic()
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            controller.join(
                page,
                "https://meet.google.com/test-call",
                name="OpenClaw",
            )
        )
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.05
    assert "Active speaker observer setup timed out" in caplog.text


def test_google_meet_check_joined_bounds_slow_visibility_probes(monkeypatch) -> None:
    google_meet_module = _load_google_meet_module()
    # Make the classify call itself slow (simulate Pi evaluate() latency).
    monkeypatch.setattr(
        google_meet_module, "_CLASSIFY_STATE_TIMEOUT_SECONDS", 5.0
    )
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    # Slow evaluate (0.05s per call) but waiting-room text → resolves quickly.
    page = _SlowJoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <main>
              <div>No one else is here yet</div>
            </main>
          </body>
        </html>
        """,
        delay_seconds=0.05,
    )

    started_at = time.monotonic()
    result = asyncio.run(controller._check_joined(page, timeout=2.0))
    elapsed = time.monotonic() - started_at

    assert result is True
    assert elapsed < 1.0


def test_google_meet_check_joined_does_not_block_on_dialog_dismissal() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _DialogStallJoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <main>
              <div>No one else is here yet</div>
            </main>
          </body>
        </html>
        """,
        stall_seconds=0.05,
    )

    started_at = time.monotonic()
    result = asyncio.run(controller._check_joined(page, timeout=0.2))
    elapsed = time.monotonic() - started_at

    assert result is True
    assert elapsed < 0.05
    assert page.dialog_locator.click_calls
    assert all(timeout != 0 for timeout in page.dialog_locator.click_calls)


# ---------------------------------------------------------------------------
# New tests: _classify_meeting_state via stub evaluate()
# ---------------------------------------------------------------------------

def test_google_meet_classify_state_returns_active() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body></body></html>",
        visible_button_labels=("Turn off mic", "Leave call"),
        preview_visible=False,
    )

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "active"


def test_google_meet_classify_state_returns_waiting() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body>No one else is here yet</body></html>",
        preview_visible=False,
    )

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "waiting"


def test_google_meet_classify_state_returns_failed() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body>You can't join this video call</body></html>",
        preview_visible=True,
    )

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "failed"


def test_google_meet_classify_state_returns_transitioning() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    # No failure, no active controls, no waiting text, no preview → transitioning
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body><main>Loading...</main></body></html>",
        visible_button_labels=(),
        preview_visible=False,
    )

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "transitioning"


def test_google_meet_check_joined_stabilizes_on_transitioning() -> None:
    """Verify _check_joined returns True after _TRANSITIONING_SETTLE_COUNT polls."""
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    # preview_visible=False, no active controls → always "transitioning".
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body><main>Loading...</main></body></html>",
        visible_button_labels=(),
        preview_visible=False,
    )

    result = asyncio.run(controller._check_joined(page, timeout=10.0))

    assert result is True


def test_google_meet_check_joined_logs_iteration_timing(caplog) -> None:
    """Verify each probe iteration is logged with timing information."""
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body>No one else is here yet</body></html>",
        preview_visible=False,
    )

    import logging
    with caplog.at_level(logging.DEBUG):
        asyncio.run(controller._check_joined(page, timeout=5.0))

    assert "Google Meet post-click probe" in caplog.text
    assert "iteration=" in caplog.text
    assert "state=" in caplog.text
    assert "elapsed=" in caplog.text
