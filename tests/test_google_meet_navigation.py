import asyncio
import importlib.util
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


class _StubPlaywrightTimeoutError(Exception):
    pass


class _StubLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        enabled: bool = True,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        self.fill_calls: list[tuple[str, int | None]] = []
        self.click_calls: list[dict[str, object]] = []
        self.visible = visible
        self.enabled = enabled
        self._on_click = on_click

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.fill_calls.append((value, timeout))

    async def click(self, timeout: int | None = None, **kwargs: object) -> None:
        self.click_calls.append({"timeout": timeout, **kwargs})
        if self._on_click is not None:
            self._on_click()

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    def get_by_role(self, _role: str, *, name: object = None) -> "_StubLocatorChain":
        del name
        return _StubLocatorChain(self)


class _TimeoutFillLocator(_StubLocator):
    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.fill_calls.append((value, timeout))
        raise _StubPlaywrightTimeoutError("fill timed out")


class _TimeoutThenSuccessClickLocator(_StubLocator):
    def __init__(self) -> None:
        super().__init__()
        self._attempt = 0

    async def click(self, timeout: int | None = None, **kwargs: object) -> None:
        self.click_calls.append({"timeout": timeout, **kwargs})
        self._attempt += 1
        if self._attempt == 1:
            raise _StubPlaywrightTimeoutError("click timed out")


class _StubLocatorChain:
    """Wraps a _StubLocator to support chained .first access."""

    def __init__(self, locator: "_StubLocator") -> None:
        self._locator = locator

    @property
    def first(self) -> "_StubLocator":
        return self._locator

    def get_by_role(self, role: str, *, name: object = None) -> "_StubLocatorChain":
        return self._locator.get_by_role(role, name=name)


class _StubPage:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, str, int]] = []
        self.name_field = _StubLocator()
        self.join_button = _StubLocator()
        self.permission_prompt = _StubLocator(visible=False)
        self.dialog_action = _StubLocator(visible=False)
        self.mouse = None  # _wake_meeting_ui checks for mouse attribute
        self.viewport_size: dict | None = None

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    def get_by_text(self, _text: str, *, exact: bool = False) -> _StubLocator: # noqa: ARG002
        return self.name_field

    def get_by_role(self, role: str, *, name: object = None) -> _StubLocator:
        if role == "textbox":
            return _StubLocatorChain(self.name_field)
        return self.join_button

    def locator(self, _selector: str) -> "_StubLocatorChain":
        """Return a chain whose .first resolves to a stub locator."""
        if _selector == "usermedia[type='microphone']":
            return _StubLocatorChain(self.permission_prompt)
        if _selector == "div[role='dialog'] [data-mdc-dialog-action]":
            return _StubLocatorChain(self.dialog_action)
        return _StubLocatorChain(self.name_field)

    async def wait_for_timeout(self, timeout: int) -> None:  # noqa: ARG002
        await asyncio.sleep(0)


class _EnableAfterWaitPage(_StubPage):
    def __init__(self) -> None:
        super().__init__()
        self.join_button = _StubLocator(enabled=False)
        self._wait_calls = 0

    async def wait_for_timeout(self, timeout: int) -> None:  # noqa: ARG002
        self._wait_calls += 1
        if self._wait_calls >= 1:
            self.join_button.enabled = True
        await asyncio.sleep(0)


class _PermissionPromptPage(_StubPage):
    def __init__(self) -> None:
        super().__init__()
        self.join_button.enabled = False
        self.permission_prompt = _StubLocator(
            visible=True,
            on_click=self._enable_join_button,
        )

    def _enable_join_button(self) -> None:
        self.join_button.enabled = True


class _DialogPromptPage(_StubPage):
    def __init__(self) -> None:
        super().__init__()
        self.join_button.enabled = False
        self.dialog_action.visible = False
        self.dialog_button = _StubLocator(
            visible=True,
            on_click=self._enable_join_button,
        )

    def locator(self, _selector: str) -> "_StubLocatorChain":
        if _selector == "div[role='dialog']":
            return _StubLocatorChain(self.dialog_button)
        return super().locator(_selector)

    def get_by_role(self, role: str, *, name: object = None) -> _StubLocator:
        if role == "button" and name is not None:
            label = getattr(name, "pattern", "")
            if any(text in label.lower() for text in ("got it", "dismiss", "close")):
                return self.dialog_button
        return super().get_by_role(role, name=name)

    def _enable_join_button(self) -> None:
        self.join_button.enabled = True


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


class _JoinDeniedPage(_StubPage):
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
              <div>You can't join this video call</div>
              <div>No one can join a meeting unless invited or admitted by the host</div>
            </main>
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
    )
    # Text fragments that map to the "waiting" meeting state.
    _WAITING_SUBSTRINGS = (
        "asking to be let in", "someone lets you in",
        "no one else is here yet", "you're the first one here",
        "waiting for the host", "waiting for others",
        "waiting for organizer", "meeting hasn't started",
        "meeting has not started",
        "still trying to get in",
        "please wait until a meeting host brings you into the call",
    )
    # Text fragments that map to the "failed" meeting state.
    _FAILURE_SUBSTRINGS = (
        "you can't join this video call", "you can't join this meeting",
        "meeting code is invalid", "couldn't find the meeting",
        "this meeting has ended", "meeting has ended", "call has ended",
    )
    _PREVIEW_TEXT_SUBSTRINGS = (
        "ready to join",
        "ask to join",
        "other ways to join",
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

        for p in self._PREVIEW_TEXT_SUBSTRINGS:
            if p in lower:
                return "preview"

        for w in self._WAITING_SUBSTRINGS:
            if w in lower:
                return "waiting"

        for label in self._visible_button_labels:
            if any(a in label.lower() for a in self._ACTIVE_CONTROL_SUBSTRINGS):
                return "active"

        if 'data-in-call="true"' in lower:
            return "shell_active"

        if not self._preview_visible:
            return "transitioning"

        return "unknown"


class _StubMouse:
    def __init__(self, on_move: Callable[[], None] | None = None) -> None:
        self.move_calls: list[tuple[int, int]] = []
        self._on_move = on_move

    async def move(self, x: int, y: int) -> None:
        self.move_calls.append((x, y))
        if self._on_move is not None:
            self._on_move()


class _WakeableShellJoinStatePage(_JoinStatePage):
    def __init__(self) -> None:
        super().__init__(
            url="https://meet.google.com/abc-defg-hij",
            html='<html><body><div data-in-call="true"></div></body></html>',
            visible_button_labels=(),
            preview_visible=False,
        )
        self._controls_visible = False
        self.mouse = _StubMouse(self._show_controls)
        self.viewport_size = {"width": 1280, "height": 720}

    def _show_controls(self) -> None:
        self._controls_visible = True
        self._visible_button_labels = (
            "Turn off microphone",
            "Leave call",
            "Show everyone",
        )


class _CountLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _ParticipantsItemLocator:
    def __init__(self, name: str) -> None:
        self._name = name

    async def get_attribute(self, attr: str) -> str | None:
        if attr == "aria-label":
            return self._name
        return None

    def locator(self, selector: str) -> _CountLocator:
        return _CountLocator(0)

    def get_by_role(self, role: str, *, name: object) -> _CountLocator:  # noqa: ARG002
        return _CountLocator(0)


class _ParticipantsListLocator(_VisibilityLocator):
    def __init__(self, page: "_ParticipantsPage") -> None:
        super().__init__(False)
        self._page = page

    async def is_visible(self) -> bool:
        return self._page.participants_open

    def locator(self, selector: str) -> object:
        if selector == "div[role='listitem']":
            return self
        raise AssertionError(f"Unexpected nested selector: {selector}")

    async def all(self) -> list[_ParticipantsItemLocator]:
        if not self._page.participants_open:
            return []
        return [_ParticipantsItemLocator("OpenClaw")]


class _ParticipantsButtonLocator(_VisibilityLocator):
    def __init__(self, page: "_ParticipantsPage", visible: bool) -> None:
        super().__init__(visible)
        self._page = page

    async def click(self, timeout: int | None = None) -> None:  # noqa: ARG002
        self._page.participants_open = True


class _ParticipantsPage(_WakeableShellJoinStatePage):
    def __init__(self) -> None:
        super().__init__()
        self.participants_open = False

    def locator(self, selector: str) -> object:
        if selector == 'div[aria-label="Participants"][role="list"]':
            return _ParticipantsListLocator(self)
        return super().locator(selector)

    def get_by_role(self, role: str, *, name: object) -> object:
        if role == "button" and hasattr(name, "search"):
            labels: tuple[str, ...] = ()
            if self._controls_visible:
                labels = self._visible_button_labels
            visible = any(name.search(label) for label in labels)
            if any("show everyone" in label.lower() for label in labels):
                if name.search("Show everyone"):
                    return _ParticipantsButtonLocator(self, visible=True)
            if any("leave call" in label.lower() for label in labels):
                if name.search("Leave call"):
                    return _VisibilityLocator(True)
        return super().get_by_role(role, name=name)


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
    assert page.name_field.fill_calls == [("OpenClaw", 10000)]
    assert page.join_button.click_calls == [{"timeout": 5000}]


def test_google_meet_join_skips_guest_name_when_signed_in_preview_has_no_name_field(
    monkeypatch,
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _StubPage()
    page.name_field = _StubLocator(visible=False)

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

    assert page.name_field.fill_calls == []
    assert page.join_button.click_calls == [{"timeout": 5000}]


def test_google_meet_join_fills_name_field_when_heading_is_missing_but_textbox_exists(
    monkeypatch,
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _StubPage()

    async def _check_joined(_page: object) -> bool:
        return True

    async def _setup_active_speaker_observer(_page: object) -> None:
        return None

    async def _wait_for_locator_visible(_locator: object, timeout: float = 0) -> bool:
        del timeout
        return False

    monkeypatch.setattr(controller, "_check_joined", _check_joined)
    monkeypatch.setattr(
        controller,
        "_setup_active_speaker_observer",
        _setup_active_speaker_observer,
    )
    monkeypatch.setattr(
        controller,
        "_wait_for_locator_visible",
        _wait_for_locator_visible,
    )

    asyncio.run(
        controller.join(
            page,
            "https://meet.google.com/test-call",
            name="OpenClaw",
        )
    )

    assert page.name_field.fill_calls == [("OpenClaw", 10000)]
    assert page.join_button.click_calls == [{"timeout": 5000}]


def test_google_meet_join_waits_for_join_button_to_become_enabled(monkeypatch) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _EnableAfterWaitPage()

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

    assert page.join_button.click_calls == [{"timeout": 5000}]
    assert page._wait_calls == 1


def test_google_meet_join_clears_microphone_prompt_before_clicking_join(
    monkeypatch,
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _PermissionPromptPage()

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

    assert page.permission_prompt.click_calls == [{"timeout": 1000}]
    assert page.join_button.click_calls == [{"timeout": 5000}]


def test_google_meet_join_retries_with_forced_click_when_primary_click_times_out(
    monkeypatch,
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _StubPage()
    page.join_button = _TimeoutThenSuccessClickLocator()

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

    assert page.join_button.click_calls == [
        {"timeout": 5000},
        {"timeout": 10000, "force": True, "no_wait_after": True},
    ]


def test_google_meet_join_retries_click_when_post_click_state_is_still_preview(
    monkeypatch,
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _StubPage()

    states = iter(["preview", "preview"])

    async def _classify_meeting_state(_page: object) -> str:
        return next(states)

    async def _check_joined(_page: object) -> bool:
        return True

    async def _setup_active_speaker_observer(_page: object) -> None:
        return None

    monkeypatch.setattr(controller, "_classify_meeting_state", _classify_meeting_state)
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

    assert page.join_button.click_calls == [
        {"timeout": 5000},
        {"timeout": 10000},
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
    assert page.name_field.fill_calls == [("OpenClaw", 10000)]
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


def test_google_meet_join_fails_fast_when_navigation_lands_on_terminal_failure(
    monkeypatch, caplog
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinDeniedPage()

    async def _classify_meeting_state(_page: object) -> str:
        return "failed"

    monkeypatch.setattr(controller, "_classify_meeting_state", _classify_meeting_state)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="Google Meet denied access"):
            asyncio.run(
                controller.join(
                    page,
                    "https://meet.google.com/test-call",
                    name="OpenClaw",
                )
            )

    assert not page.name_field.fill_calls
    assert not page.join_button.click_calls
    assert "post_navigation.access_denied" in caplog.text


def test_google_meet_join_reports_terminal_failure_after_post_click_check(
    monkeypatch, caplog
) -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinDeniedPage()

    async def _check_joined(_page: object) -> bool:
        return False

    states = iter(["preview", "preview", "failed"])

    async def _classify_meeting_state(_page: object) -> str:
        return next(states)

    monkeypatch.setattr(controller, "_check_joined", _check_joined)
    monkeypatch.setattr(controller, "_classify_meeting_state", _classify_meeting_state)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="Google Meet denied access"):
            asyncio.run(
                controller.join(
                    page,
                    "https://meet.google.com/test-call",
                    name="OpenClaw",
                )
            )

    assert page.name_field.fill_calls == [("OpenClaw", 10000)]
    assert page.join_button.click_calls
    assert "post_click.access_denied" in caplog.text


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


def test_google_meet_classify_meeting_state_active_js_ignores_preview_device_controls() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _ClassifierScriptPage()

    asyncio.run(controller._classify_meeting_state(page))

    assert page.last_expression is not None
    script = str(page.last_expression).lower()
    active_start = script.index("const activelabels")
    shell_start = script.index("// --- joined shell")
    active_section = script[active_start:shell_start]

    assert "turn off mic" not in active_section
    assert "turn on mic" not in active_section
    assert "turn off camera" not in active_section
    assert "turn on camera" not in active_section


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


def test_google_meet_check_joined_rejects_live_meet_url_without_waiting_or_in_call_controls() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    # preview_visible=False and no active labels → evaluate() returns
    # "transitioning". A bare live Meet URL is no longer sufficient to claim a
    # successful join.
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body><main>Meet</main></body></html>",
        visible_button_labels=(),
        preview_visible=False,
    )

    result = asyncio.run(controller._check_joined(page, timeout=5.0))

    assert result is False


def test_google_meet_check_joined_rejects_active_state_when_preview_join_controls_remain() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html='<html><body><div data-in-call="true"></div></body></html>',
        visible_button_labels=("Ask to join", "Leave call"),
        preview_visible=False,
    )

    result = asyncio.run(controller._check_joined(page, timeout=1.0))

    assert result is False


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


def test_google_meet_classify_state_rejects_preview_device_controls_without_leave_button() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="<html><body><main>Connecting...</main></body></html>",
        visible_button_labels=("Turn off microphone", "Turn off camera"),
        preview_visible=False,
    )

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "transitioning"


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


def test_google_meet_classify_state_returns_waiting_for_still_trying_to_get_in() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            Still trying to get in...
            Please wait until a meeting host brings you into the call
          </body>
        </html>
        """,
        visible_button_labels=("Leave call",),
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
    """Verify _check_joined does not accept a bare transitioning shell."""
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

    assert result is False


def test_google_meet_classify_state_returns_shell_active() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html='<html><body><div data-in-call="true"></div></body></html>',
        visible_button_labels=(),
        preview_visible=False,
    )

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "shell_active"


def test_google_meet_classify_state_keeps_signed_in_preview_out_of_shell_active() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <div data-in-call="true"></div>
            <main>
              <div>Ready to join?</div>
              <button>Ask to join</button>
              <button>Other ways to join</button>
            </main>
          </body>
        </html>
        """,
        visible_button_labels=(),
        preview_visible=False,
    )

    result = asyncio.run(controller._classify_meeting_state(page))

    assert result == "preview"


def test_google_meet_check_joined_wakes_shell_active_ui_to_validate_controls() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _WakeableShellJoinStatePage()

    result = asyncio.run(controller._check_joined(page, timeout=1.0))

    assert result is True
    assert page.mouse.move_calls


def test_google_meet_check_joined_accepts_stable_shell_active_without_controls() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <div data-in-call="true"></div>
          </body>
        </html>
        """,
        visible_button_labels=(),
        preview_visible=False,
    )
    page.mouse = _StubMouse()
    page.viewport_size = {"width": 1280, "height": 720}

    result = asyncio.run(controller._check_joined(page, timeout=1.0))

    assert result is True
    assert page.mouse.move_calls


def test_google_meet_check_joined_rejects_shell_with_only_preview_device_controls() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _JoinStatePage(
        url="https://meet.google.com/abc-defg-hij",
        html="""
        <html>
          <body>
            <div data-in-call="true"></div>
          </body>
        </html>
        """,
        visible_button_labels=("Turn off microphone", "Turn off camera"),
        preview_visible=False,
    )

    result = asyncio.run(controller._check_joined(page, timeout=1.0))

    assert result is False


def test_google_meet_get_participants_wakes_ui_and_accepts_show_everyone_button() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _ParticipantsPage()

    participants = asyncio.run(controller.get_participants(page))

    assert [participant.name for participant in participants] == ["OpenClaw"]
    assert page.mouse.move_calls


def test_google_meet_leave_wakes_ui_and_accepts_leave_call_label() -> None:
    google_meet_module = _load_google_meet_module()
    controller = google_meet_module.GoogleMeetBrowserPlatformController()
    page = _WakeableShellJoinStatePage()

    asyncio.run(controller.leave(page))

    assert page.mouse.move_calls


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
    with caplog.at_level(
        logging.DEBUG, logger="joinly.providers.browser.platforms.google_meet"
    ):
        asyncio.run(controller._check_joined(page, timeout=5.0))

    assert "Google Meet post-click probe" in caplog.text
    assert "iteration=" in caplog.text
    assert "state=" in caplog.text
    assert "elapsed=" in caplog.text
