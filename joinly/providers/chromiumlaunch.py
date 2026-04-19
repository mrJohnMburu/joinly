import asyncio
import base64
import contextlib
import logging
import os
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Self

from joinly.core import AudioReader, AudioWriter, VideoReader
from joinly.providers.base import BaseMeetingProvider
from joinly.providers.browser.devices.pulse_server import (
    AutoPulseServer,
    PulseServer,
    SystemPulseServer,
)
from joinly.providers.browser.devices.virtual_microphone import VirtualMicrophone
from joinly.providers.browser.devices.virtual_speaker import VirtualSpeaker
from joinly.types import VideoSnapshot

logger = logging.getLogger(__name__)

_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WnR0n4AAAAASUVORK5CYII="
)


class ChromiumLaunchMeetingProvider(BaseMeetingProvider, VideoReader):
    """Prototype meeting provider that launches plain Chromium without Playwright."""

    def __init__(
        self,
        *,
        reader_byte_depth: int | None = None,
        writer_byte_depth: int | None = None,
        executable_path: str | None = None,
        profile_dir: str | None = None,
        display: str | None = None,
        launch_ready_seconds: float = 5.0,
        use_system_pulse_server: bool | None = None,
        browser_args: tuple[str, ...] = (),
    ) -> None:
        self._env = os.environ.copy()
        if display is not None:
            self._env["DISPLAY"] = display
        self._env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

        if use_system_pulse_server is None:
            self._pulse_server = AutoPulseServer(env=self._env)
        elif use_system_pulse_server:
            self._pulse_server = SystemPulseServer(env=self._env)
        else:
            self._pulse_server = PulseServer(env=self._env)
        self._virtual_speaker = (
            VirtualSpeaker(env=self._env)
            if not reader_byte_depth
            else VirtualSpeaker(env=self._env, byte_depth=reader_byte_depth)
        )
        self._virtual_microphone = (
            VirtualMicrophone(env=self._env)
            if not writer_byte_depth
            else VirtualMicrophone(env=self._env, byte_depth=writer_byte_depth)
        )
        self._services = [
            self._pulse_server,
            self._virtual_speaker,
            self._virtual_microphone,
        ]
        self._stack = AsyncExitStack()
        self._executable_path = executable_path
        self._profile_dir = Path(profile_dir).expanduser() if profile_dir else None
        self._launch_ready_seconds = launch_ready_seconds
        self._browser_args = tuple(browser_args)
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def audio_reader(self) -> AudioReader:
        return self._virtual_speaker

    @property
    def audio_writer(self) -> AudioWriter:
        return self._virtual_microphone

    @property
    def video_reader(self) -> VideoReader:
        return self

    async def __aenter__(self) -> Self:
        try:
            for service in self._services:
                await self._stack.enter_async_context(service)
        except Exception:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            await self.leave()
        finally:
            await self._stack.aclose()

    async def join(
        self,
        url: str | None = None,
        name: str | None = None,
        passcode: str | None = None,
    ) -> None:
        del name, passcode
        if not url:
            msg = "Meeting URL is required."
            raise ValueError(msg)
        if self._proc is not None and self._proc.returncode is None:
            msg = "Chromium meeting process is already running."
            raise RuntimeError(msg)

        executable = self._resolve_executable()
        profile_dir = self._resolve_profile_dir()
        profile_dir.mkdir(parents=True, exist_ok=True)

        argv = [
            executable,
            f"--user-data-dir={profile_dir}",
            "--password-store=basic",
            "--no-first-run",
            "--new-window",
            "--alsa-output-device=pulse",
            "--alsa-input-device=pulse",
            "--autoplay-policy=no-user-gesture-required",
            *self._browser_args,
            url,
        ]
        logger.info("Launching Chromium prototype provider for %s", url)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            start_new_session=True,
        )

        try:
            await asyncio.wait_for(
                self._proc.wait(),
                timeout=self._launch_ready_seconds,
            )
        except TimeoutError:
            return

        stderr = await self._read_stderr(self._proc)
        code = self._proc.returncode
        self._proc = None
        msg = f"Chromium exited before it was ready (exit code {code}). {stderr}".strip()
        raise RuntimeError(msg)

    async def leave(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is not None:
            self._proc = None
            return

        self._proc.terminate()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        finally:
            self._proc = None

    async def snapshot(self) -> VideoSnapshot:
        return VideoSnapshot(data=_BLANK_PNG, media_type="image/png")

    def _resolve_executable(self) -> str:
        if self._executable_path:
            return self._executable_path

        for candidate in ("chromium", "chromium-browser", "google-chrome"):
            if path := shutil.which(candidate):
                return path

        msg = "Chromium executable not found."
        raise RuntimeError(msg)

    def _resolve_profile_dir(self) -> Path:
        if self._profile_dir is not None:
            return self._profile_dir
        return Path.home() / ".cache" / "joinly" / "chromium-launch-profile"

    @staticmethod
    async def _read_stderr(proc: asyncio.subprocess.Process) -> str:
        if proc.stderr is None:
            return ""
        with contextlib.suppress(Exception):
            return (await proc.stderr.read()).decode(errors="replace").strip()
        return ""
