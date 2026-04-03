from collections.abc import AsyncIterator

from joinly.services.stt.noop import NoopSTT
from joinly.services.tts.noop import NoopTTS
from joinly.types import SpeechWindow


async def _speech_windows() -> AsyncIterator[SpeechWindow]:
    yield SpeechWindow(
        data=b"\x00\x00" * 160,
        time_ns=0,
        is_speech=True,
        speaker=None,
    )


async def test_noop_stt_yields_no_segments() -> None:
    stt = NoopSTT()

    segments = [segment async for segment in stt.stream(_speech_windows())]

    assert segments == []
    assert stt.audio_format.sample_rate == 16000
    assert stt.audio_format.byte_depth == 2


async def test_noop_tts_yields_no_audio() -> None:
    tts = NoopTTS()

    chunks = [chunk async for chunk in tts.stream("hello from openclaw")]

    assert chunks == []
    assert tts.audio_format.sample_rate == 24000
    assert tts.audio_format.byte_depth == 2
