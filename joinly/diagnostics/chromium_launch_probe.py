from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
)
from typing import Any

from dotenv import load_dotenv

from joinly.container import _resolve
from joinly.core import AudioWriter, TTS

try:
    from joinly.utils.audio import convert_audio_format as _convert_audio_format_impl
except ModuleNotFoundError:
    _convert_audio_format_impl = None


def convert_audio_format(data: bytes, source_format: Any, target_format: Any) -> bytes:
    if _convert_audio_format_impl is None:
        from joinly.utils.audio import convert_audio_format as runtime_impl

        return runtime_impl(data, source_format, target_format)

    return _convert_audio_format_impl(data, source_format, target_format)


def _parse_kv(value: list[str]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for item in value:
        key, separator, raw_value = item.partition("=")
        if not separator:
            msg = f"{item!r} is not of the form key=value"
            raise ValueError(msg)
        try:
            parsed[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed[key] = raw_value
    return parsed


async def build_component(
    spec: str | type[Any],
    base: str,
    suffix: str,
    args: dict[str, Any],
    stack: AsyncExitStack,
) -> Any:
    cls = _resolve(spec, base=base, suffix=suffix)
    instance = cls(**args)
    if isinstance(instance, AbstractAsyncContextManager):
        return await stack.enter_async_context(instance)
    if isinstance(instance, AbstractContextManager):
        return stack.enter_context(instance)
    return instance


async def stream_tts_to_writer(*, text: str, tts: TTS, writer: AudioWriter) -> None:
    buffer = bytearray()
    async for chunk in tts.stream(text):
        buffer.extend(convert_audio_format(chunk, tts.audio_format, writer.audio_format))
        while len(buffer) >= writer.chunk_size:
            await writer.write(bytes(buffer[: writer.chunk_size]))
            del buffer[: writer.chunk_size]

    if buffer:
        await writer.write(bytes(buffer))


async def run_probe(
    *,
    meeting_url: str,
    provider_args: dict[str, Any],
    speak_text: str | None,
    join_delay_seconds: float,
    hold_seconds: float,
    repeat_count: int,
    repeat_interval_seconds: float,
    tts: str,
    tts_args: dict[str, Any],
) -> None:
    async with AsyncExitStack() as stack:
        provider = await build_component(
            "chromium_launch",
            "joinly.providers",
            "MeetingProvider",
            provider_args,
            stack,
        )
        await provider.join(meeting_url)

        if join_delay_seconds > 0:
            await asyncio.sleep(join_delay_seconds)

        if speak_text:
            tts_instance = await build_component(
                tts,
                "joinly.services.tts",
                "TTS",
                tts_args,
                stack,
            )
            for idx in range(max(1, repeat_count)):
                await stream_tts_to_writer(
                    text=speak_text,
                    tts=tts_instance,
                    writer=provider.audio_writer,
                )
                if idx < max(1, repeat_count) - 1 and repeat_interval_seconds > 0:
                    await asyncio.sleep(repeat_interval_seconds)

        if hold_seconds > 0:
            await asyncio.sleep(hold_seconds)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Launch a plain Chromium meeting session with Joinly audio plumbing and "
            "optionally speak a short test phrase into the meeting."
        )
    )
    parser.add_argument("meeting_url", type=str)
    parser.add_argument("--env-file", type=str, default=None)
    parser.add_argument("--speak-text", type=str, default=None)
    parser.add_argument("--join-delay-seconds", type=float, default=8.0)
    parser.add_argument("--hold-seconds", type=float, default=20.0)
    parser.add_argument("--repeat-count", type=int, default=1)
    parser.add_argument("--repeat-interval-seconds", type=float, default=15.0)
    parser.add_argument("--tts", type=str, default="deepgram")
    parser.add_argument(
        "--tts-arg",
        dest="tts_args",
        action="append",
        default=[],
        metavar="KEY=VAL",
    )
    parser.add_argument(
        "--provider-arg",
        dest="provider_args",
        action="append",
        default=[],
        metavar="KEY=VAL",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file)

    asyncio.run(
        run_probe(
            meeting_url=args.meeting_url,
            provider_args=_parse_kv(args.provider_args),
            speak_text=args.speak_text,
            join_delay_seconds=args.join_delay_seconds,
            hold_seconds=args.hold_seconds,
            repeat_count=args.repeat_count,
            repeat_interval_seconds=args.repeat_interval_seconds,
            tts=args.tts,
            tts_args=_parse_kv(args.tts_args),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
