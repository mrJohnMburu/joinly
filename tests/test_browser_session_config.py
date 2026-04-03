import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

sys.modules.setdefault(
    "PIL",
    SimpleNamespace(Image=SimpleNamespace(), ImageOps=SimpleNamespace()),
)

playwright_module = sys.modules.setdefault("playwright", ModuleType("playwright"))
playwright_async_api = sys.modules.setdefault(
    "playwright.async_api", ModuleType("playwright.async_api")
)
playwright_async_api.Browser = object
playwright_async_api.BrowserContext = object
playwright_async_api.Page = object
playwright_async_api.Playwright = object
playwright_async_api.async_playwright = lambda: None
playwright_module.async_api = playwright_async_api

_BROWSER_SESSION_PATH = (
    Path(__file__).resolve().parents[1]
    / "joinly"
    / "providers"
    / "browser"
    / "browser_session.py"
)
_BROWSER_SESSION_SPEC = importlib.util.spec_from_file_location(
    "joinly_test_browser_session",
    _BROWSER_SESSION_PATH,
)
assert _BROWSER_SESSION_SPEC is not None
assert _BROWSER_SESSION_SPEC.loader is not None
browser_session_module = importlib.util.module_from_spec(_BROWSER_SESSION_SPEC)
_BROWSER_SESSION_SPEC.loader.exec_module(browser_session_module)
BrowserSession = browser_session_module.BrowserSession


class _FakeStderr:
    def __init__(self, *lines: bytes) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.listeners: list[tuple[str, object]] = []

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    def on(self, event: str, listener: object) -> None:
        self.listeners.append((event, listener))


class _FakeBrowserContext:
    def __init__(self, pages: list[_FakePage] | None = None) -> None:
        self.pages = list(pages or [])
        self.close_calls = 0

    @property
    def browser(self) -> SimpleNamespace:
        return SimpleNamespace()

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.close_calls += 1


class _FakeChromium:
    def __init__(self, executable_path: str) -> None:
        self.executable_path = executable_path
        self.launch_calls: list[dict[str, object]] = []
        self.context = _FakeBrowserContext()

    async def launch_persistent_context(self, **kwargs: object) -> _FakeBrowserContext:
        self.launch_calls.append(kwargs)
        return self.context


class _FakePlaywright:
    def __init__(self, executable_path: str) -> None:
        self.chromium = _FakeChromium(executable_path)
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class _FakePlaywrightStarter:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self.playwright


def test_browser_session_uses_configured_executable_and_persistent_profile(
    monkeypatch, tmp_path: Path
) -> None:
    launched: dict[str, object] = {}
    fake_playwright = _FakePlaywright("/playwright/chromium")

    monkeypatch.setattr(
        browser_session_module,
        "async_playwright",
        lambda: _FakePlaywrightStarter(fake_playwright),
    )

    executable_path = tmp_path / "chromium"
    executable_path.write_text("")
    profile_dir = tmp_path / "persistent-profile"
    net_log_path = tmp_path / "chromium-netlog.json"
    session = BrowserSession(
        executable_path=str(executable_path),
        profile_dir=str(profile_dir),
        net_log_path=str(net_log_path),
    )

    async def scenario() -> None:
        await session.__aenter__()
        try:
            launch_kwargs = fake_playwright.chromium.launch_calls[0]
            launched["kwargs"] = launch_kwargs
            assert launch_kwargs["executable_path"] == str(executable_path)
            assert launch_kwargs["user_data_dir"] == str(profile_dir)
            assert launch_kwargs["headless"] is False
            assert launch_kwargs["chromium_sandbox"] is False
            assert launch_kwargs["ignore_default_args"] == ["--mute-audio"]
            assert f"--log-net-log={net_log_path}" in launch_kwargs["args"]
            assert "--net-log-capture-mode=Everything" in launch_kwargs["args"]
            assert profile_dir.exists()
        finally:
            await session.__aexit__()

    asyncio.run(scenario())

    assert profile_dir.exists()
    assert fake_playwright.stop_calls == 1
    assert fake_playwright.chromium.context.close_calls == 1


def test_browser_session_creates_and_cleans_up_temporary_profile(
    monkeypatch, tmp_path: Path
) -> None:
    fake_playwright = _FakePlaywright("/playwright/chromium")

    monkeypatch.setattr(
        browser_session_module,
        "async_playwright",
        lambda: _FakePlaywrightStarter(fake_playwright),
    )

    playwright_browser = tmp_path / "playwright-chromium"
    playwright_browser.write_text("")
    fake_playwright.chromium.executable_path = str(playwright_browser)
    session = BrowserSession()

    captured_profile_dir: Path | None = None

    async def scenario() -> None:
        nonlocal captured_profile_dir
        await session.__aenter__()
        try:
            launch_kwargs = fake_playwright.chromium.launch_calls[0]
            assert launch_kwargs["executable_path"] == str(playwright_browser)
            captured_profile_dir = Path(launch_kwargs["user_data_dir"])
            assert captured_profile_dir.exists()
        finally:
            await session.__aexit__()

    asyncio.run(scenario())

    assert captured_profile_dir is not None
    assert not captured_profile_dir.exists()


def test_browser_session_reuses_default_page_before_creating_new_pages(
    monkeypatch, tmp_path: Path
) -> None:
    fake_playwright = _FakePlaywright("/playwright/chromium")
    default_page = _FakePage()
    fake_playwright.chromium.context = _FakeBrowserContext([default_page])

    monkeypatch.setattr(
        browser_session_module,
        "async_playwright",
        lambda: _FakePlaywrightStarter(fake_playwright),
    )

    executable_path = tmp_path / "chromium"
    executable_path.write_text("")
    session = BrowserSession(executable_path=str(executable_path))

    async def scenario() -> None:
        await session.__aenter__()
        try:
            first_page = await session.get_page()
            second_page = await session.get_page()
            assert first_page is default_page
            assert second_page is not default_page
        finally:
            await session.__aexit__()

    asyncio.run(scenario())
