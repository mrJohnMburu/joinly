from collections.abc import AsyncIterator

from joinly.core import TTS
from joinly.types import AudioFormat


class NoopTTS(TTS):
    """A text-to-speech provider that emits no audio."""

    def __init__(self, *, sample_rate: int = 24000) -> None:
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        _ = text
        if False:
            yield b""
