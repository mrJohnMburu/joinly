import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

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


class _FakeProc:
    def __init__(self) -> None:
        self.stderr = _FakeStderr(
            b"DevTools listening on ws://127.0.0.1/devtools/browser/test\n"
        )
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode or 0


class _FakeChromium:
    def __init__(self, executable_path: str) -> None:
        self.executable_path = executable_path
        self.connected_to: list[str] = []

    async def connect_over_cdp(self, cdp_endpoint: str) -> SimpleNamespace:
        self.connected_to.append(cdp_endpoint)
        return SimpleNamespace(contexts=[SimpleNamespace(pages=[])])


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
    fake_proc = _FakeProc()
    fake_playwright = _FakePlaywright("/playwright/chromium")

    async def fake_create_subprocess_exec(
        *args: object, **kwargs: object
    ) -> _FakeProc:
        launched["args"] = args
        launched["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr(
        browser_session_module,
        "async_playwright",
        lambda: _FakePlaywrightStarter(fake_playwright),
    )
    monkeypatch.setattr(
        browser_session_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    executable_path = tmp_path / "chromium"
    executable_path.write_text("")
    profile_dir = tmp_path / "persistent-profile"
    session = BrowserSession(
        executable_path=str(executable_path),
        profile_dir=str(profile_dir),
    )

    async def scenario() -> None:
        await session.__aenter__()
        try:
            assert launched["args"][0] == str(executable_path)
            assert f"--user-data-dir={profile_dir}" in launched["args"]
            assert profile_dir.exists()
        finally:
            await session.__aexit__()

    asyncio.run(scenario())

    assert profile_dir.exists()
    assert fake_playwright.stop_calls == 1
    assert fake_proc.terminated is True


def test_browser_session_creates_and_cleans_up_temporary_profile(
    monkeypatch, tmp_path: Path
) -> None:
    launched: dict[str, object] = {}
    fake_proc = _FakeProc()
    fake_playwright = _FakePlaywright("/playwright/chromium")

    async def fake_create_subprocess_exec(
        *args: object, **kwargs: object
    ) -> _FakeProc:
        launched["args"] = args
        launched["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr(
        browser_session_module,
        "async_playwright",
        lambda: _FakePlaywrightStarter(fake_playwright),
    )
    monkeypatch.setattr(
        browser_session_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
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
            assert launched["args"][0] == str(playwright_browser)
            profile_arg = next(
                arg
                for arg in launched["args"]
                if isinstance(arg, str) and arg.startswith("--user-data-dir=")
            )
            captured_profile_dir = Path(profile_arg.removeprefix("--user-data-dir="))
            assert captured_profile_dir.exists()
        finally:
            await session.__aexit__()

    asyncio.run(scenario())

    assert captured_profile_dir is not None
    assert not captured_profile_dir.exists()
