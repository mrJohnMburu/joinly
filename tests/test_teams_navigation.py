import asyncio
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType


class _StubPlaywrightTimeoutError(Exception):
    pass


class _StubLocator:
    def __init__(self, *, visible: bool = True, on_click=None) -> None:
        self.visible = visible
        self.fill_calls: list[tuple[str, int | None]] = []
        self.click_calls: list[dict[str, object]] = []
        self._on_click = on_click

    @property
    def first(self) -> "_StubLocator":
        return self

    async def fill(self, value: str, timeout: int | None = None) -> None:
        if not self.visible:
            raise _StubPlaywrightTimeoutError("not visible")
        self.fill_calls.append((value, timeout))

    async def click(self, timeout: int | None = None, **kwargs: object) -> None:
        if not self.visible:
            raise _StubPlaywrightTimeoutError("not visible")
        self.click_calls.append({"timeout": timeout, **kwargs})
        if self._on_click is not None:
            self._on_click()

    async def is_visible(self) -> bool:
        return self.visible


class _TeamsJoinPage:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, str, int]] = []
        self.wait_calls: list[int] = []
        self.name_placeholder = _StubLocator(visible=False)
        self.name_locator = _StubLocator(visible=False)
        self.join_browser_button = _StubLocator(
            visible=True, on_click=self._show_guest_name_field
        )
        self.join_button = _StubLocator(visible=True)

    def _show_guest_name_field(self) -> None:
        self.name_locator.visible = True

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    async def click(self, _selector: str, timeout: int = 0) -> None:  # noqa: ARG002
        raise _StubPlaywrightTimeoutError("no dialog")

    async def wait_for_timeout(self, timeout: int) -> None:
        self.wait_calls.append(timeout)

    def get_by_placeholder(self, _pattern: object) -> _StubLocator:
        return self.name_placeholder

    def locator(self, selector: str) -> _StubLocator:
        if "input[placeholder*=\"name\"" in selector.lower():
            return self.name_locator
        return _StubLocator(visible=False)

    def get_by_role(self, role: str, *, name: object) -> _StubLocator:
        if role != "button":
            return _StubLocator(visible=False)

        labels = {
            "Continue on this browser": self.join_browser_button,
            "Join now": self.join_button,
        }
        for label, locator in labels.items():
            if hasattr(name, "search") and name.search(label):
                return locator
        return _StubLocator(visible=False)


def _load_teams_module() -> ModuleType:
    playwright_module = ModuleType("playwright")
    playwright_async_api = ModuleType("playwright.async_api")
    playwright_async_api.Page = object
    playwright_async_api.TimeoutError = _StubPlaywrightTimeoutError
    playwright_module.async_api = playwright_async_api
    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.async_api"] = playwright_async_api

    browser_pkg = ModuleType("joinly.providers.browser")
    browser_pkg.__path__ = []
    platforms_pkg = ModuleType("joinly.providers.browser.platforms")
    platforms_pkg.__path__ = []
    sys.modules["joinly.providers.browser"] = browser_pkg
    sys.modules["joinly.providers.browser.platforms"] = platforms_pkg

    base_spec = importlib.util.spec_from_file_location(
        "joinly.providers.browser.platforms.base",
        Path(__file__).resolve().parents[1]
        / "joinly"
        / "providers"
        / "browser"
        / "platforms"
        / "base.py",
    )
    assert base_spec is not None
    assert base_spec.loader is not None
    base_module = importlib.util.module_from_spec(base_spec)
    base_spec.loader.exec_module(base_module)
    sys.modules["joinly.providers.browser.platforms.base"] = base_module

    spec = importlib.util.spec_from_file_location(
        "joinly.providers.browser.platforms.teams",
        Path(__file__).resolve().parents[1]
        / "joinly"
        / "providers"
        / "browser"
        / "platforms"
        / "teams.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_teams_join_standard_handles_browser_redirect_and_locator_name_field(
    monkeypatch,
) -> None:
    teams_module = _load_teams_module()
    controller = teams_module.TeamsBrowserPlatformController()
    page = _TeamsJoinPage()

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
            "https://teams.microsoft.com/meet/348688768667033?p=test",
            name="OpenClaw",
        )
    )

    assert page.goto_calls == [
        ("https://teams.microsoft.com/meet/348688768667033?p=test", "load", 20000)
    ]
    assert page.join_browser_button.click_calls == [{"timeout": 1000}]
    assert page.name_locator.fill_calls == [("OpenClaw", 10000)]
    assert page.join_button.click_calls == [{"timeout": 10000}]
