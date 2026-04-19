import asyncio
from types import SimpleNamespace

from joinly.container import SessionContainer
from joinly.settings import Settings


class _FakeAudioReader:
    audio_format = SimpleNamespace(byte_depth=2)

    async def read(self) -> None:
        raise NotImplementedError


class _FakeAudioWriter:
    audio_format = SimpleNamespace(byte_depth=2)
    chunk_size = 1

    async def write(self, data: bytes) -> None:  # noqa: ARG002
        return None


class _FakeVideoReader:
    async def snapshot(self) -> None:
        return None


class FakeVAD:
    instances: list["FakeVAD"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.audio_format = SimpleNamespace(byte_depth=2)
        type(self).instances.append(self)


class DeepgramSTT:
    instances: list["DeepgramSTT"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.audio_format = SimpleNamespace(byte_depth=2)
        type(self).instances.append(self)


class WhisperSTT:
    instances: list["WhisperSTT"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.audio_format = SimpleNamespace(byte_depth=2)
        type(self).instances.append(self)


class FakeTTS:
    instances: list["FakeTTS"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.audio_format = SimpleNamespace(byte_depth=2)
        type(self).instances.append(self)


class BrowserMeetingProvider:
    instances: list["BrowserMeetingProvider"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.join_calls: list[tuple[str | None, str | None, str | None]] = []
        self.leave_calls = 0
        self.audio_reader = _FakeAudioReader()
        self.audio_writer = _FakeAudioWriter()
        self.video_reader = _FakeVideoReader()
        type(self).instances.append(self)

    async def join(
        self,
        url: str | None = None,
        name: str | None = None,
        passcode: str | None = None,
    ) -> None:
        self.join_calls.append((url, name, passcode))

    async def leave(self) -> None:
        self.leave_calls += 1

    async def send_chat_message(self, message: str) -> None:  # noqa: ARG002
        return None

    async def get_chat_history(self) -> None:
        return None

    async def get_participants(self) -> list[object]:
        return []

    async def mute(self) -> None:
        return None

    async def unmute(self) -> None:
        return None

    async def share_screen(self, url: str) -> None:  # noqa: ARG002
        return None

    async def stop_sharing(self) -> None:
        return None

    async def set_animation(self, animation: str | None) -> None:  # noqa: ARG002
        return None

    async def update_ui(self, update: object) -> None:  # noqa: ARG002
        return None


class FakeTranscriptionController:
    instances: list["FakeTranscriptionController"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.reader = None
        self.vad = None
        self.stt = None
        self.no_speech_event = asyncio.Event()
        self.start_calls = 0
        self.stop_calls = 0
        type(self).instances.append(self)

    async def start(self, *_args: object) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1


class FakeSpeechController:
    instances: list["FakeSpeechController"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.writer = None
        self.tts = None
        self.no_speech_event = None
        self.start_calls = 0
        self.stop_calls = 0
        self.spoken: list[str] = []
        type(self).instances.append(self)

    async def start(self, *_args: object) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    async def speak_text(self, text: str) -> None:
        self.spoken.append(text)


def _reset_fakes() -> None:
    FakeVAD.instances.clear()
    DeepgramSTT.instances.clear()
    WhisperSTT.instances.clear()
    FakeTTS.instances.clear()
    BrowserMeetingProvider.instances.clear()
    FakeTranscriptionController.instances.clear()
    FakeSpeechController.instances.clear()


def _build_settings() -> Settings:
    return Settings.model_construct(
        vad=FakeVAD,
        stt=DeepgramSTT,
        tts=FakeTTS,
        meeting_provider=BrowserMeetingProvider,
        transcription_controller=FakeTranscriptionController,
        speech_controller=FakeSpeechController,
    )


def _build_whisper_settings() -> Settings:
    return Settings.model_construct(
        device="cpu",
        vad=FakeVAD,
        stt=WhisperSTT,
        tts=FakeTTS,
        meeting_provider=BrowserMeetingProvider,
        transcription_controller=FakeTranscriptionController,
        speech_controller=FakeSpeechController,
    )


def test_session_container_defers_heavy_component_construction_until_join() -> None:
    async def scenario() -> None:
        _reset_fakes()
        session_container = SessionContainer(settings=_build_settings())
        meeting_session = await session_container.__aenter__()

        try:
            assert FakeVAD.instances == []
            assert DeepgramSTT.instances == []
            assert FakeTTS.instances == []
            assert BrowserMeetingProvider.instances == []
            assert FakeTranscriptionController.instances == []
            assert FakeSpeechController.instances == []

            await meeting_session.join_meeting(
                "https://meet.google.com/test-call",
                "OpenClaw",
            )

            assert len(FakeVAD.instances) == 1
            assert len(DeepgramSTT.instances) == 1
            assert len(FakeTTS.instances) == 1
            assert len(BrowserMeetingProvider.instances) == 1
            assert len(FakeTranscriptionController.instances) == 1
            assert len(FakeSpeechController.instances) == 1

            assert DeepgramSTT.instances[0].kwargs["finalize_silence"] == 0.375
            assert BrowserMeetingProvider.instances[0].kwargs == {
                "reader_byte_depth": 2,
                "writer_byte_depth": 2,
            }
            assert BrowserMeetingProvider.instances[0].join_calls == [
                ("https://meet.google.com/test-call", "OpenClaw", None)
            ]
            assert FakeTranscriptionController.instances[0].start_calls == 1
            assert FakeSpeechController.instances[0].start_calls == 1
        finally:
            await session_container.__aexit__()

    asyncio.run(scenario())


def test_leave_meeting_rebuilds_runtime_for_the_next_meeting() -> None:
    async def scenario() -> None:
        _reset_fakes()
        session_container = SessionContainer(settings=_build_settings())
        meeting_session = await session_container.__aenter__()

        try:
            await meeting_session.join_meeting(
                "https://meet.google.com/first-call",
                "OpenClaw",
            )
            first_provider = BrowserMeetingProvider.instances[0]
            first_transcription = FakeTranscriptionController.instances[0]
            first_speech = FakeSpeechController.instances[0]

            await meeting_session.leave_meeting()

            assert first_provider.leave_calls == 1
            assert first_transcription.stop_calls == 1
            assert first_speech.stop_calls == 1

            await meeting_session.join_meeting(
                "https://meet.google.com/second-call",
                "OpenClaw",
            )

            assert len(BrowserMeetingProvider.instances) == 2
            assert len(FakeTranscriptionController.instances) == 2
            assert len(FakeSpeechController.instances) == 2
            assert BrowserMeetingProvider.instances[1].join_calls == [
                ("https://meet.google.com/second-call", "OpenClaw", None)
            ]
        finally:
            await session_container.__aexit__()

    asyncio.run(scenario())


def test_session_container_tunes_cpu_whisper_for_single_worker_backpressure() -> None:
    async def scenario() -> None:
        _reset_fakes()
        session_container = SessionContainer(settings=_build_whisper_settings())
        meeting_session = await session_container.__aenter__()

        try:
            await meeting_session.join_meeting(
                "https://meet.google.com/test-call",
                "OpenClaw",
            )

            assert len(WhisperSTT.instances) == 1
            assert WhisperSTT.instances[0].kwargs == {}
            assert FakeTranscriptionController.instances[0].kwargs == {
                "max_stt_tasks": 1,
                "utterance_queue_size": 64,
                "window_queue_size": 512,
            }
        finally:
            await session_container.__aexit__()

    asyncio.run(scenario())
