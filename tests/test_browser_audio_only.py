import asyncio
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from joinly.types import ProviderNotSupportedError


class _StubService:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "_StubService":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _StubPulseServer(_StubService):
    instances: list["_StubPulseServer"] = []

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        type(self).instances.append(self)


class _StubSystemPulseServer(_StubService):
    instances: list["_StubSystemPulseServer"] = []

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        type(self).instances.append(self)


class _StubPage:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def goto(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def bring_to_front(self) -> None:
        return None

    async def evaluate(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def wait_for_timeout(self, *_args: object, **_kwargs: object) -> None:
        return None


class _StubBrowserSession(_StubService):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.page = _StubPage()

    async def get_page(self) -> _StubPage:
        return self.page


class _StubVirtualSpeaker(_StubService):
    audio_format = SimpleNamespace(byte_depth=2)

    async def read(self) -> None:
        return None


class _StubVirtualMicrophone(_StubService):
    audio_format = SimpleNamespace(byte_depth=2)
    chunk_size = 1

    async def write(self, _data: bytes) -> None:
        return None


class _StubCameraFeed:
    instances: list["_StubCameraFeed"] = []

    def __init__(self, writer: object) -> None:
        self.writer = writer
        self.audio_writer = object()
        self.install_calls = 0
        self.stop_calls = 0
        self.effects: list[str | None] = []
        type(self).instances.append(self)

    async def install(self, _page: object) -> None:
        self.install_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    def set_effect(self, name: str | None) -> None:
        self.effects.append(name)


class _StubPlatformController:
    instances: list["_StubPlatformController"] = []
    url_pattern = re.compile(r"^$")

    def __init__(self) -> None:
        self.join_calls: list[tuple[str, str | None, str | None]] = []
        self.leave_calls = 0
        self.active_speaker: str | None = None
        type(self).instances.append(self)

    async def join(
        self, _page: object, url: str, name: str | None = None, passcode: str | None = None
    ) -> None:
        self.join_calls.append((url, name, passcode))

    async def leave(self, _page: object) -> None:
        self.leave_calls += 1

    async def send_chat_message(self, _page: object, _message: str) -> None:
        return None

    async def get_chat_history(self, _page: object) -> list[object]:
        return []

    async def get_participants(self, _page: object) -> list[object]:
        return []

    async def mute(self, _page: object) -> None:
        return None

    async def unmute(self, _page: object) -> None:
        return None

    async def share_screen(self, _page: object) -> None:
        return None

    async def stop_sharing(self, _page: object) -> None:
        return None


class _StubGoogleMeetController(_StubPlatformController):
    instances: list["_StubGoogleMeetController"] = []
    url_pattern = re.compile(r"https://meet\.google\.com/.*")

    def __init__(
        self,
        *,
        navigation_preflight_urls: tuple[str, ...] = (),
        debug_artifact_dir: str | None = None,
    ) -> None:
        super().__init__()
        self.navigation_preflight_urls = navigation_preflight_urls
        self.debug_artifact_dir = debug_artifact_dir


class _StubTeamsController(_StubPlatformController):
    instances: list["_StubTeamsController"] = []
    url_pattern = re.compile(r"https://teams\.microsoft\.com/.*")


class _StubZoomController(_StubPlatformController):
    instances: list["_StubZoomController"] = []
    url_pattern = re.compile(r"https://.*zoom\.us/.*")


def _load_meeting_provider_module(monkeypatch) -> ModuleType:
    sys.modules.setdefault(
        "PIL",
        SimpleNamespace(
            Image=SimpleNamespace(
                Resampling=SimpleNamespace(LANCZOS="lanczos"),
                open=lambda *_args, **_kwargs: None,
            ),
            ImageOps=SimpleNamespace(crop=lambda image, border: image, fit=lambda *args, **kwargs: None),
        ),
    )

    playwright_module = ModuleType("playwright")
    playwright_async_api = ModuleType("playwright.async_api")
    playwright_async_api.Page = object
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_async_api)
    playwright_module.async_api = playwright_async_api

    browser_pkg = ModuleType("joinly.providers.browser")
    browser_pkg.__path__ = []
    devices_pkg = ModuleType("joinly.providers.browser.devices")
    devices_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "joinly.providers.browser", browser_pkg)
    monkeypatch.setitem(sys.modules, "joinly.providers.browser.devices", devices_pkg)

    browser_session_module = ModuleType("joinly.providers.browser.browser_session")
    browser_session_module.BrowserSession = _StubBrowserSession
    camera_feed_module = ModuleType("joinly.providers.browser.camera_feed")
    camera_feed_module.CameraFeed = _StubCameraFeed
    pulse_module = ModuleType("joinly.providers.browser.devices.pulse_server")
    pulse_module.PulseServer = _StubPulseServer
    pulse_module.SystemPulseServer = _StubSystemPulseServer
    display_module = ModuleType("joinly.providers.browser.devices.virtual_display")
    display_module.VirtualDisplay = _StubService
    mic_module = ModuleType("joinly.providers.browser.devices.virtual_microphone")
    mic_module.VirtualMicrophone = _StubVirtualMicrophone
    speaker_module = ModuleType("joinly.providers.browser.devices.virtual_speaker")
    speaker_module.VirtualSpeaker = _StubVirtualSpeaker
    platforms_module = ModuleType("joinly.providers.browser.platforms")
    platforms_module.BrowserPlatformController = _StubPlatformController
    platforms_module.GoogleMeetBrowserPlatformController = _StubGoogleMeetController
    platforms_module.TeamsBrowserPlatformController = _StubTeamsController
    platforms_module.ZoomBrowserPlatformController = _StubZoomController
    screen_share_module = ModuleType("joinly.providers.browser.screen_share")

    async def _remove_overlay(_page: object) -> None:
        return None

    async def _setup_content_stream(
        _page: object, _content_page: object, _display_size: tuple[int, int]
    ) -> None:
        return None

    screen_share_module.remove_overlay = _remove_overlay
    screen_share_module.setup_content_stream = _setup_content_stream

    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.browser_session",
        browser_session_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.camera_feed",
        camera_feed_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.devices.pulse_server",
        pulse_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.devices.virtual_display",
        display_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.devices.virtual_microphone",
        mic_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.devices.virtual_speaker",
        speaker_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.platforms",
        platforms_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "joinly.providers.browser.screen_share",
        screen_share_module,
    )

    spec = importlib.util.spec_from_file_location(
        "joinly_test_meeting_provider",
        Path(__file__).resolve().parents[1]
        / "joinly"
        / "providers"
        / "browser"
        / "meeting_provider.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reset_stubs() -> None:
    _StubCameraFeed.instances.clear()
    _StubGoogleMeetController.instances.clear()
    _StubTeamsController.instances.clear()
    _StubZoomController.instances.clear()
    _StubPulseServer.instances.clear()
    _StubSystemPulseServer.instances.clear()


def test_audio_only_provider_skips_camera_feed_and_uses_microphone_writer(
    monkeypatch,
) -> None:
    _reset_stubs()
    meeting_provider_module = _load_meeting_provider_module(monkeypatch)
    provider = meeting_provider_module.BrowserMeetingProvider(audio_only=True)

    async def scenario() -> None:
        await provider.join("https://meet.google.com/test-call", "OpenClaw")
        assert provider.audio_writer is provider._virtual_microphone
        await provider.leave()

    asyncio.run(scenario())

    assert _StubCameraFeed.instances == []
    assert len(_StubGoogleMeetController.instances) == 1
    assert _StubGoogleMeetController.instances[0].join_calls == [
        ("https://meet.google.com/test-call", "OpenClaw", None)
    ]
    assert _StubGoogleMeetController.instances[0].leave_calls == 1


def test_provider_passes_browser_diagnostics_config_to_session_and_google_meet(
    monkeypatch,
) -> None:
    _reset_stubs()
    meeting_provider_module = _load_meeting_provider_module(monkeypatch)
    provider = meeting_provider_module.BrowserMeetingProvider(
        audio_only=True,
        display_size=(1024, 576),
        browser_software_rendering=True,
        browser_net_log_path="/tmp/joinly-chromium-netlog.json",
        google_meet_preflight_urls=(
            "https://www.google.com",
            "https://meet.google.com",
        ),
        google_meet_debug_artifact_dir="/tmp/joinly-google-meet-debug",
    )

    async def scenario() -> None:
        await provider.join("https://meet.google.com/test-call", "OpenClaw")
        await provider.leave()

    asyncio.run(scenario())

    assert provider._browser_session.kwargs["net_log_path"] == (
        "/tmp/joinly-chromium-netlog.json"
    )
    assert provider._browser_session.kwargs["software_rendering"] is True
    assert provider._browser_session.kwargs["window_size"] == (1024, 576)
    assert len(_StubGoogleMeetController.instances) == 1
    assert _StubGoogleMeetController.instances[0].navigation_preflight_urls == (
        "https://www.google.com",
        "https://meet.google.com",
    )
    assert _StubGoogleMeetController.instances[0].debug_artifact_dir == (
        "/tmp/joinly-google-meet-debug"
    )


def test_provider_can_reuse_system_pulse_server(monkeypatch) -> None:
    _reset_stubs()
    meeting_provider_module = _load_meeting_provider_module(monkeypatch)
    provider = meeting_provider_module.BrowserMeetingProvider(
        audio_only=True,
        use_system_pulse_server=True,
    )

    async def scenario() -> None:
        await provider.join("https://meet.google.com/test-call", "OpenClaw")
        await provider.leave()

    asyncio.run(scenario())

    assert len(_StubPulseServer.instances) == 0
    assert len(_StubSystemPulseServer.instances) == 1


def test_audio_only_provider_rejects_video_features(monkeypatch) -> None:
    _reset_stubs()
    meeting_provider_module = _load_meeting_provider_module(monkeypatch)
    provider = meeting_provider_module.BrowserMeetingProvider(audio_only=True)

    async def scenario() -> None:
        await provider.join("https://meet.google.com/test-call", "OpenClaw")
        with pytest.raises(ProviderNotSupportedError, match="audio-only"):
            await provider.share_screen("https://example.com")
        with pytest.raises(ProviderNotSupportedError, match="audio-only"):
            await provider.snapshot()
        await provider.leave()

    asyncio.run(scenario())
