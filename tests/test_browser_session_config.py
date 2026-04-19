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
        self.grant_permissions_calls: list[dict[str, object]] = []

    @property
    def browser(self) -> SimpleNamespace:
        return SimpleNamespace()

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.close_calls += 1

    async def grant_permissions(
        self, permissions: list[str], origin: str | None = None
    ) -> None:
        self.grant_permissions_calls.append(
            {"permissions": permissions, "origin": origin}
        )


class _FakeChromium:
    def __init__(self, executable_path: str) -> None:
        self.executable_path = executable_path
        self.launch_calls: list[dict[str, object]] = []
        self.context = _FakeBrowserContext()
        self.launch_side_effects: list[Exception | _FakeBrowserContext] = []

    async def launch_persistent_context(self, **kwargs: object) -> _FakeBrowserContext:
        self.launch_calls.append(kwargs)
        if self.launch_side_effects:
            result = self.launch_side_effects.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
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
        window_size=(1024, 576),
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
            assert "--window-size=1024,576" in launch_kwargs["args"]
            assert f"--log-net-log={net_log_path}" in launch_kwargs["args"]
            assert "--net-log-capture-mode=Everything" in launch_kwargs["args"]
            assert fake_playwright.chromium.context.grant_permissions_calls == [
                {"permissions": ["microphone", "camera"], "origin": None}
            ]
            assert profile_dir.exists()
        finally:
            await session.__aexit__()

    asyncio.run(scenario())

    assert profile_dir.exists()
    assert fake_playwright.stop_calls == 1
    assert fake_playwright.chromium.context.close_calls == 1


def test_browser_session_software_rendering_uses_swiftshader_flags(
    monkeypatch, tmp_path: Path
) -> None:
    fake_playwright = _FakePlaywright("/playwright/chromium")

    monkeypatch.setattr(
        browser_session_module,
        "async_playwright",
        lambda: _FakePlaywrightStarter(fake_playwright),
    )

    executable_path = tmp_path / "chromium"
    executable_path.write_text("")
    session = BrowserSession(
        executable_path=str(executable_path),
        software_rendering=True,
    )

    async def scenario() -> None:
        await session.__aenter__()
        try:
            launch_kwargs = fake_playwright.chromium.launch_calls[0]
            args = launch_kwargs["args"]
            assert launch_kwargs["ignore_default_args"] == [
                "--mute-audio",
                "--use-angle=gles",
                "--enable-gpu-rasterization",
            ]
            assert "--disable-gpu" not in args
            assert "--disable-gpu-rasterization" in args
            assert "--use-gl=angle" in args
            assert "--use-angle=swiftshader" in args
            assert "--enable-unsafe-swiftshader" in args
            assert any(
                arg.startswith("--disable-features=")
                and "Vulkan" in arg
                and "UseSkiaRenderer" in arg
                for arg in args
            )
        finally:
            await session.__aexit__()

    asyncio.run(scenario())


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
            assert {event for event, _ in first_page.listeners} == {
                "console",
                "pageerror",
                "requestfailed",
            }
        finally:
            await session.__aexit__()

    asyncio.run(scenario())


def test_browser_session_falls_back_when_persistent_profile_is_locked(
    monkeypatch, tmp_path: Path
) -> None:
    fake_playwright = _FakePlaywright("/playwright/chromium")
    fake_playwright.chromium.launch_side_effects = [
        RuntimeError(
            "BrowserType.launch_persistent_context: Failed to create "
            "/tmp/profile/SingletonLock: File exists (17)\n"
            "Failed to create a ProcessSingleton for your profile directory."
        ),
        fake_playwright.chromium.context,
    ]

    monkeypatch.setattr(
        browser_session_module,
        "async_playwright",
        lambda: _FakePlaywrightStarter(fake_playwright),
    )

    executable_path = tmp_path / "chromium"
    executable_path.write_text("")
    profile_dir = tmp_path / "persistent-profile"
    session = BrowserSession(
        executable_path=str(executable_path),
        profile_dir=str(profile_dir),
    )

    fallback_profile_dir: Path | None = None

    async def scenario() -> None:
        nonlocal fallback_profile_dir
        await session.__aenter__()
        try:
            assert len(fake_playwright.chromium.launch_calls) == 2
            assert fake_playwright.chromium.launch_calls[0]["user_data_dir"] == str(
                profile_dir
            )
            fallback_profile_dir = Path(
                fake_playwright.chromium.launch_calls[1]["user_data_dir"]
            )
            assert fallback_profile_dir != profile_dir
            assert fallback_profile_dir.exists()
        finally:
            await session.__aexit__()

    asyncio.run(scenario())

    assert fallback_profile_dir is not None
    assert not fallback_profile_dir.exists()
