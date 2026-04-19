import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import ClassVar, Self

from joinly.providers.browser.devices.pulse_module_manager import (
    PulseModuleManager,
)

logger = logging.getLogger(__name__)

_RUNTIME_ENV_VAR = "PULSE_RUNTIME_PATH"
_SERVER_ENV_VAR = "PULSE_SERVER"
_AUTOSPAWN_ENV_VAR = "PULSE_DISABLE_AUTOSPAWN"
_SERVER_READY_TIMEOUT_SECONDS = 60.0
_SERVER_READY_RETRY_DELAY_SECONDS = 0.1


class PulseServer(PulseModuleManager):
    """A class to start and stop a pulse server instance."""

    _startup_lock: ClassVar[asyncio.Lock | None] = None

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Initialize the VirtualMicrophone.

        Args:
            env: Optional environment dictionary to set the audio server path.
        """
        self._env: dict[str, str] = env if env is not None else {}
        self.socket_path: Path | None = None
        self._dir: tempfile.TemporaryDirectory[str] | None = None
        self._proc: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> Self:
        """Start the audio server."""
        if self._proc is not None:
            msg = "Pulse server already started"
            raise RuntimeError(msg)

        async with self._get_startup_lock():
            self._dir = tempfile.TemporaryDirectory(prefix="pulseserver_")
            self.socket_path = Path(self._dir.name) / "native"
            self._env[_RUNTIME_ENV_VAR] = self._dir.name
            self._env[_SERVER_ENV_VAR] = f"unix:{self.socket_path}"
            self._env[_AUTOSPAWN_ENV_VAR] = "1"

            logger.debug("Starting PulseAudio server under %s", self._dir.name)
            self._proc = await asyncio.create_subprocess_exec(
                "/usr/bin/pulseaudio",
                "--daemonize=no",
                "--exit-idle-time=-1",
                "--file=/dev/null",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                start_new_session=True,
            )

            try:
                await asyncio.wait_for(
                    _wait_for_server(self.socket_path, env=self._env),
                    timeout=_SERVER_READY_TIMEOUT_SECONDS,
                )
            except TimeoutError as e:
                msg = (
                    "PulseAudio server did not start in time. "
                    "Check CPU/IO contention on Pi."
                )
                logger.error(msg)  # noqa: TRY400
                await self._cleanup_failed_startup()
                raise RuntimeError(msg) from e

        logger.debug("PulseAudio server started")

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop the audio server."""
        if self._proc is None or self._proc.returncode is not None:
            logger.warning("No PulseAudio server to stop")
        else:
            logger.debug("Stopping PulseAudio server")
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                logger.warning("PulseAudio server did not stop in time")
                self._proc.kill()
                await self._proc.wait()
            self._proc = None
            self._env.pop(_RUNTIME_ENV_VAR, None)
            self._env.pop(_SERVER_ENV_VAR, None)
            self._env.pop(_AUTOSPAWN_ENV_VAR, None)
            logger.debug("PulseAudio server stopped")

        if self._dir is not None:
            self._dir.cleanup()
            logger.debug("Temporary directory removed: %s", self._dir.name)
            self._dir = None

        self.socket_path = None

    @classmethod
    def _get_startup_lock(cls) -> asyncio.Lock:
        """Return the process-wide lock used for private PulseAudio startup."""
        if cls._startup_lock is None:
            cls._startup_lock = asyncio.Lock()
        return cls._startup_lock

    async def _cleanup_failed_startup(self) -> None:
        """Tear down partial state after a failed private-server startup."""
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()
            self._proc = None

        self._env.pop(_RUNTIME_ENV_VAR, None)
        self._env.pop(_SERVER_ENV_VAR, None)
        self._env.pop(_AUTOSPAWN_ENV_VAR, None)

        if self._dir is not None:
            self._dir.cleanup()
            self._dir = None
        self.socket_path = None


class SystemPulseServer:
    """A no-op pulse server wrapper that reuses the existing session server."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Initialize the system server wrapper."""
        self._env: dict[str, str] = env if env is not None else {}

    async def __aenter__(self) -> Self:
        """Verify that a session Pulse/PipeWire server is reachable."""
        logger.debug("Using existing PulseAudio/PipeWire server")
        if not await _pactl_info_succeeds(env=self._env):
            error_text = await _read_pactl_error(env=self._env)
            msg = (
                "System PulseAudio/PipeWire server is not available. "
                f"pactl info failed: {error_text}"
            )
            logger.error(msg)
            raise RuntimeError(msg)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Leave the existing session server running."""
        logger.debug("Leaving existing PulseAudio/PipeWire server running")


class AutoPulseServer:
    """Prefer the host Pulse/PipeWire session server and fall back to private."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Initialize the adaptive PulseAudio server wrapper."""
        self._env: dict[str, str] = env if env is not None else {}
        self._delegate: PulseServer | SystemPulseServer | None = None

    async def __aenter__(self) -> Self:
        """Enter the chosen PulseAudio server strategy."""
        if await _pactl_info_succeeds(env=self._env):
            logger.debug("Detected host Pulse/PipeWire server; reusing it")
            self._delegate = SystemPulseServer(env=self._env)
        else:
            logger.debug(
                "No host Pulse/PipeWire server detected; starting private PulseAudio"
            )
            self._delegate = PulseServer(env=self._env)

        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Exit the chosen PulseAudio server strategy."""
        if self._delegate is not None:
            await self._delegate.__aexit__(*_exc)
            self._delegate = None


async def _wait_for_server(path: Path, env: dict[str, str] | None = None) -> None:
    """Wait for the server socket and command interface to become ready."""
    await _wait_for_server_socket(path)
    await _wait_for_pactl_server(env=env)


async def _wait_for_server_socket(path: Path) -> None:
    """Wait for the PulseAudio Unix socket to accept connections."""
    while True:
        try:
            reader, writer = await asyncio.open_unix_connection(path)
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
        else:
            writer.close()
            await writer.wait_closed()
            return


async def _wait_for_pactl_server(env: dict[str, str] | None = None) -> None:
    """Wait for pactl info to succeed against the server."""
    while True:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/pactl",
            "info",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return

        error_text = stderr.decode().strip() or stdout.decode().strip()
        logger.debug("PulseAudio server not ready for pactl yet: %s", error_text)
        await asyncio.sleep(_SERVER_READY_RETRY_DELAY_SECONDS)


async def _pactl_info_succeeds(env: dict[str, str] | None = None) -> bool:
    """Return whether pactl can reach a Pulse/PipeWire server."""
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/pactl",
        "info",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    await proc.communicate()
    return proc.returncode == 0


async def _read_pactl_error(env: dict[str, str] | None = None) -> str:
    """Return the current pactl info error output."""
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/pactl",
        "info",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    return stderr.decode().strip() or stdout.decode().strip()
