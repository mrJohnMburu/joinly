import asyncio
from collections.abc import AsyncIterator

from joinly.controllers.transcription.default import DefaultTranscriptionController
from joinly.types import (
    AudioChunk,
    AudioFormat,
    SpeechWindow,
    Transcript,
    TranscriptSegment,
)
from joinly.utils.clock import Clock
from joinly.utils.events import EventBus

_EXPECTED_SEGMENT_COUNT = 2


class _FakeReader:
    audio_format = AudioFormat(sample_rate=16000, byte_depth=4)

    def __init__(self, chunks: list[AudioChunk]) -> None:
        self._chunks = list(chunks)

    async def read(self) -> AudioChunk:
        if not self._chunks:
            msg = "reader exhausted"
            raise RuntimeError(msg)
        return self._chunks.pop(0)


class _FakeVAD:
    audio_format = AudioFormat(sample_rate=16000, byte_depth=4)

    def __init__(self, windows: list[SpeechWindow]) -> None:
        self._windows = list(windows)

    async def stream(
        self,
        _chunks: AsyncIterator[AudioChunk],
    ) -> AsyncIterator[SpeechWindow]:
        for window in self._windows:
            yield window


class _FakeSTT:
    audio_format = AudioFormat(sample_rate=16000, byte_depth=4)

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.completed = asyncio.Event()
        self.calls = 0

    async def stream(
        self,
        windows: AsyncIterator[SpeechWindow],
    ) -> AsyncIterator[TranscriptSegment]:
        self.calls += 1
        self.started.set()
        buffered = [window async for window in windows]
        await self.allow_finish.wait()
        yield TranscriptSegment(
            text=f"utterance-{self.calls}",
            start=buffered[0].time_ns / 1e9,
            end=buffered[-1].time_ns / 1e9,
            speaker=buffered[0].speaker,
        )
        if self.calls == _EXPECTED_SEGMENT_COUNT:
            self.completed.set()


def test_transcription_controller_queues_utterances_without_dropping() -> None:
    """Queue back-to-back utterances instead of dropping them under STT load."""
    controller = DefaultTranscriptionController(
        max_stt_tasks=1,
        utterance_tail_seconds=0.1,
        utterance_queue_size=4,
        window_queue_size=4,
    )
    controller.reader = _FakeReader(
        [
            AudioChunk(data=b"\x00" * 4, time_ns=0, speaker="Alpha"),
            AudioChunk(data=b"\x00" * 4, time_ns=200_000_000, speaker="Beta"),
        ]
    )
    controller.vad = _FakeVAD(
        [
            SpeechWindow(data=b"\x00" * 4, time_ns=0, is_speech=True, speaker="Alpha"),
            SpeechWindow(
                data=b"\x00" * 4,
                time_ns=150_000_000,
                is_speech=False,
                speaker="Alpha",
            ),
            SpeechWindow(
                data=b"\x00" * 4,
                time_ns=200_000_000,
                is_speech=True,
                speaker="Beta",
            ),
            SpeechWindow(
                data=b"\x00" * 4,
                time_ns=350_000_000,
                is_speech=False,
                speaker="Beta",
            ),
        ]
    )
    stt = _FakeSTT()
    controller.stt = stt
    transcript = Transcript()

    async def scenario() -> None:
        await controller.start(Clock(), transcript, EventBus())
        await stt.started.wait()

        # The first utterance is still in STT; the second should be queued, not dropped.
        assert controller._utterance_queue is not None  # noqa: SLF001
        assert controller._utterance_queue.qsize() == 1  # noqa: SLF001

        stt.allow_finish.set()
        assert controller._vad_task is not None  # noqa: SLF001
        await controller._vad_task  # noqa: SLF001
        await stt.completed.wait()
        await controller.stop()

    asyncio.run(scenario())

    assert stt.calls == _EXPECTED_SEGMENT_COUNT
    assert [segment.speaker for segment in transcript.segments] == ["Alpha", "Beta"]
