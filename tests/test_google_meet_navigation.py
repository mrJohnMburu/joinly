import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


class _StubLocator:
    def __init__(self) -> None:
        self.fill_calls: list[tuple[str, int | None]] = []
        self.click_calls: list[int | None] = []

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.fill_calls.append((value, timeout))

    async def click(self, timeout: int | None = None) -> None:
        self.click_calls.append(timeout)


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


def test_google_meet_join_uses_domcontentloaded_navigation(monkeypatch) -> None:
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
        ("https://meet.google.com/test-call", "domcontentloaded", 60000)
    ]
    assert page.name_field.fill_calls == [("OpenClaw", 20000)]
    assert page.join_button.click_calls == [1000]
