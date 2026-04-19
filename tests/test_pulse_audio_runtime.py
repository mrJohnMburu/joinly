import asyncio

import pytest

from joinly.providers.browser.devices import pulse_server as pulse_server_module
from joinly.providers.browser.devices.pulse_module_manager import PulseModuleManager
from joinly.providers.browser.devices.pulse_server import (
    AutoPulseServer,
    PulseServer,
    _wait_for_pactl_server,
)

_EXPECTED_MODULE_ID = 17
_EXPECTED_PACTL_INFO_CALLS = 3
_EXPECTED_PRIVATE_SERVER_COUNT = 2


class _FakeProc:
    def __init__(
        self,
        *,
        returncode: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = 0

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _ProbePulseModuleManager(PulseModuleManager):
    async def load_module_for_test(self, *cmd_args: str) -> int:
        """Expose the protected loader for focused unit tests."""
        return await self._load_module(*cmd_args)


def test_load_module_retries_transient_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry transient pactl connection failures when loading a module."""
    calls: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*cmd: str, **_kwargs: object) -> _FakeProc:
        calls.append(cmd)
        if len(calls) == 1:
            return _FakeProc(
                returncode=1,
                stderr=b"Connection failure: Connection refused",
            )
        return _FakeProc(returncode=0, stdout=b"17\n")

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    module_id = asyncio.run(
        _ProbePulseModuleManager().load_module_for_test(
            "module-pipe-sink",
            "sink_name=test",
        )
    )

    assert module_id == _EXPECTED_MODULE_ID
    assert calls == [
        ("/usr/bin/pactl", "load-module", "module-pipe-sink", "sink_name=test"),
        ("/usr/bin/pactl", "load-module", "module-pipe-sink", "sink_name=test"),
    ]


def test_load_module_raises_on_non_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop immediately on non-transient pactl failures."""
    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: object) -> _FakeProc:
        return _FakeProc(returncode=1, stderr=b"Module initialization failed")

    async def fake_sleep(_delay: float) -> None:
        pytest.fail("sleep should not be used for non-transient pactl errors")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="Module initialization failed"):
        asyncio.run(_ProbePulseModuleManager().load_module_for_test("module-pipe-sink"))


def test_wait_for_pactl_server_retries_until_info_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Poll pactl info until the server responds successfully."""
    calls = 0
    env = {"PULSE_SERVER": "unix:pulse-test"}

    async def fake_create_subprocess_exec(*cmd: str, **kwargs: object) -> _FakeProc:
        nonlocal calls
        calls += 1
        assert cmd == ("/usr/bin/pactl", "info")
        assert kwargs["env"] == env
        if calls < _EXPECTED_PACTL_INFO_CALLS:
            return _FakeProc(
                returncode=1,
                stderr=b"Connection failure: Connection refused",
            )
        return _FakeProc(returncode=0, stdout=b"Server String: unix:pulse-test")

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(_wait_for_pactl_server(env=env))

    assert calls == _EXPECTED_PACTL_INFO_CALLS


def test_private_pulse_servers_start_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serialize private PulseAudio bootstrap to reduce concurrent Pi contention."""
    launches: list[dict[str, str]] = []
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    allow_first_ready = asyncio.Event()
    allow_second_ready = asyncio.Event()
    wait_calls = 0

    async def fake_create_subprocess_exec(
        *cmd: str,
        **kwargs: object,
    ) -> _FakeProc:
        assert cmd[0] == "/usr/bin/pulseaudio"
        launches.append(dict(kwargs["env"]))
        if len(launches) == _EXPECTED_PRIVATE_SERVER_COUNT:
            second_started.set()
        return _FakeProc(returncode=None)

    async def fake_wait_for_server(
        _path: object,
        _env: dict[str, str] | None = None,
    ) -> None:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            first_started.set()
            await allow_first_ready.wait()
        else:
            await allow_second_ready.wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(pulse_server_module, "_wait_for_server", fake_wait_for_server)
    monkeypatch.setattr(PulseServer, "_startup_lock", None)

    async def scenario() -> None:
        server_a = PulseServer(env={})
        server_b = PulseServer(env={})

        task_a = asyncio.create_task(server_a.__aenter__())
        await first_started.wait()

        task_b = asyncio.create_task(server_b.__aenter__())
        await asyncio.sleep(0)
        assert len(launches) == 1

        allow_first_ready.set()
        await task_a

        await second_started.wait()
        allow_second_ready.set()
        await task_b

        await server_b.__aexit__()
        await server_a.__aexit__()

    asyncio.run(scenario())

    assert len(launches) == _EXPECTED_PRIVATE_SERVER_COUNT
    assert launches[0]["PULSE_SERVER"] != launches[1]["PULSE_SERVER"]


def test_auto_pulse_server_prefers_system_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse the host Pulse/PipeWire server when pactl info already succeeds."""
    entered: list[str] = []

    async def fake_pactl_info_succeeds(
        env: dict[str, str] | None = None,
    ) -> bool:
        assert env == {"BASE": "1"}
        return True

    class _StubSystemServer:
        def __init__(self, *, env: dict[str, str] | None = None) -> None:
            assert env == {"BASE": "1"}

        async def __aenter__(self) -> "_StubSystemServer":
            entered.append("system")
            return self

        async def __aexit__(self, *_exc: object) -> None:
            entered.append("system-exit")

    class _ForbiddenPrivateServer:
        def __init__(self, **_kwargs: object) -> None:
            pytest.fail("private PulseServer should not be constructed")

    monkeypatch.setattr(
        pulse_server_module,
        "_pactl_info_succeeds",
        fake_pactl_info_succeeds,
    )
    monkeypatch.setattr(pulse_server_module, "SystemPulseServer", _StubSystemServer)
    monkeypatch.setattr(pulse_server_module, "PulseServer", _ForbiddenPrivateServer)

    async def scenario() -> None:
        server = AutoPulseServer(env={"BASE": "1"})
        await server.__aenter__()
        await server.__aexit__()

    asyncio.run(scenario())

    assert entered == ["system", "system-exit"]


def test_auto_pulse_server_falls_back_to_private_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start a private PulseAudio server when no host session server exists."""
    entered: list[str] = []

    async def fake_pactl_info_succeeds(
        env: dict[str, str] | None = None,
    ) -> bool:
        assert env == {"BASE": "1"}
        return False

    class _StubPrivateServer:
        def __init__(self, *, env: dict[str, str] | None = None) -> None:
            assert env == {"BASE": "1"}

        async def __aenter__(self) -> "_StubPrivateServer":
            entered.append("private")
            return self

        async def __aexit__(self, *_exc: object) -> None:
            entered.append("private-exit")

    class _ForbiddenSystemServer:
        def __init__(self, **_kwargs: object) -> None:
            pytest.fail("system Pulse server should not be constructed")

    monkeypatch.setattr(
        pulse_server_module,
        "_pactl_info_succeeds",
        fake_pactl_info_succeeds,
    )
    monkeypatch.setattr(pulse_server_module, "PulseServer", _StubPrivateServer)
    monkeypatch.setattr(
        pulse_server_module,
        "SystemPulseServer",
        _ForbiddenSystemServer,
    )

    async def scenario() -> None:
        server = AutoPulseServer(env={"BASE": "1"})
        await server.__aenter__()
        await server.__aexit__()

    asyncio.run(scenario())

    assert entered == ["private", "private-exit"]
