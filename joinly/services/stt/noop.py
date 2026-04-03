from collections.abc import AsyncIterator

from joinly.core import STT
from joinly.types import AudioFormat, SpeechWindow, TranscriptSegment


class NoopSTT(STT):
    """A speech-to-text provider that consumes audio and emits no segments."""

    def __init__(self, *, sample_rate: int = 16000) -> None:
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def stream(
        self, windows: AsyncIterator[SpeechWindow]
    ) -> AsyncIterator[TranscriptSegment]:
        async for _window in windows:
            continue
        if False:
            yield TranscriptSegment(text="", start=0.0, end=0.0)
