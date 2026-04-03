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
        self.click_calls: list[int | None] = []

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.fill_calls.append((value, timeout))

    async def click(self, timeout: int | None = None) -> None:
        self.click_calls.append(timeout)


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
    assert page.join_button.click_calls == [1000]


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
