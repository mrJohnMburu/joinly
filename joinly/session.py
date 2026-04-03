import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass

from joinly.core import (
    MeetingProvider,
    SpeechController,
    TranscriptionController,
    VideoReader,
)
from joinly.types import (
    ActionAnimation,
    MeetingChatHistory,
    MeetingParticipant,
    SpeechInterruptedError,
    Transcript,
    UIUpdate,
    VideoSnapshot,
)
from joinly.utils.clock import Clock
from joinly.utils.events import EventBus, EventType

logger = logging.getLogger(__name__)


@dataclass
class MeetingRuntime:
    """Concrete components required for an active meeting."""

    meeting_provider: MeetingProvider
    transcription_controller: TranscriptionController
    speech_controller: SpeechController
    video_reader: VideoReader


class MeetingSession:
    """Orchestrates meeting actions."""

    def __init__(
        self,
        meeting_provider: MeetingProvider | None = None,
        transcription_controller: TranscriptionController | None = None,
        speech_controller: SpeechController | None = None,
        video_reader: VideoReader | None = None,
        *,
        runtime_start: Callable[[], Awaitable[MeetingRuntime]] | None = None,
        runtime_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize a meeting session.

        Args:
            meeting_provider (MeetingProvider | None): The meeting provider to use.
            transcription_controller (TranscriptionController | None): Controller for
                managing transcriptions.
            speech_controller (SpeechController | None): Controller for managing speech
                actions.
            video_reader (VideoReader | None): Controller for managing video actions.
            runtime_start: Callback to lazily create the runtime when a meeting starts.
            runtime_stop: Callback to release the runtime after the meeting ends.
        """
        self._meeting_provider = meeting_provider
        self._transcription_controller = transcription_controller
        self._speech_controller = speech_controller
        self._video_reader = video_reader
        self._runtime_start = runtime_start
        self._runtime_stop = runtime_stop
        self._clock: Clock | None = None
        self._transcript: Transcript | None = None
        self._event_bus = EventBus()

    def _set_runtime(self, runtime: MeetingRuntime) -> None:
        """Store the active meeting runtime."""
        self._meeting_provider = runtime.meeting_provider
        self._transcription_controller = runtime.transcription_controller
        self._speech_controller = runtime.speech_controller
        self._video_reader = runtime.video_reader

    def _clear_runtime(self) -> None:
        """Drop references to the active meeting runtime."""
        self._meeting_provider = None
        self._transcription_controller = None
        self._speech_controller = None
        self._video_reader = None

    async def _ensure_runtime_started(self) -> bool:
        """Start the meeting runtime if it has not been initialized yet."""
        if (
            self._meeting_provider is not None
            and self._transcription_controller is not None
            and self._speech_controller is not None
            and self._video_reader is not None
        ):
            return False

        if self._runtime_start is None:
            msg = "Meeting session runtime is not initialized."
            raise RuntimeError(msg)

        self._set_runtime(await self._runtime_start())
        return True

    def _require_runtime(self) -> MeetingRuntime:
        """Return the active meeting runtime."""
        if (
            self._meeting_provider is None
            or self._transcription_controller is None
            or self._speech_controller is None
            or self._video_reader is None
        ):
            msg = "Not joined any meeting, cannot access meeting controls."
            raise RuntimeError(msg)

        return MeetingRuntime(
            meeting_provider=self._meeting_provider,
            transcription_controller=self._transcription_controller,
            speech_controller=self._speech_controller,
            video_reader=self._video_reader,
        )

    async def _stop_runtime(self) -> None:
        """Stop the current meeting runtime if it is container-managed."""
        if self._runtime_stop is None:
            return

        await self._runtime_stop()
        self._clear_runtime()

    @property
    def transcript(self) -> Transcript:
        """Return the current transcript of the meeting."""
        if self._transcript is None:
            msg = "Not joined any meeting, cannot access transcript."
            raise RuntimeError(msg)
        return self._transcript

    @property
    def meeting_seconds(self) -> float:
        """Return the current meeting duration in seconds."""
        if self._clock is None:
            msg = "Not joined any meeting, cannot access meeting duration."
            raise RuntimeError(msg)
        return self._clock.now_s

    def subscribe(
        self, event_type: EventType, handler: Callable[[], Coroutine[None, None, None]]
    ) -> Callable[[], None]:
        """Add a listener for transcription events.

        Args:
            event_type (EventType): The type of event to listen for.
            handler: A callable.

        Returns:
            A callable to remove the handler.
        """
        return self._event_bus.subscribe(event_type, handler)

    async def join_meeting(
        self,
        meeting_url: str | None = None,
        participant_name: str | None = None,
        passcode: str | None = None,
    ) -> None:
        """Join a meeting using the provided URL.

        Args:
            meeting_url (str | None): The URL of the meeting to join. Might be required
                depending on the meeting provider.
            participant_name (str | None): The name of the participant.
                Defaults to the sessions participant name.
            passcode (str | None): The password or passcode for the meeting
                (if required).
        """
        created_runtime = await self._ensure_runtime_started()
        runtime = self._require_runtime()

        try:
            await runtime.meeting_provider.join(meeting_url, participant_name, passcode)
            self._clock = Clock()
            self._transcript = Transcript()

            _unsubscribe: Callable[[], None] | None = None

            async def unmute_on_start() -> None:
                """Unmute the participant when the meeting starts."""
                if _unsubscribe is not None:
                    _unsubscribe()
                with contextlib.suppress(Exception):
                    await runtime.meeting_provider.unmute()

            _unsubscribe = self._event_bus.subscribe("segment", unmute_on_start)

            await runtime.transcription_controller.start(
                self._clock, self._transcript, self._event_bus
            )
            await runtime.speech_controller.start(
                self._clock, self._transcript, self._event_bus
            )
        except Exception:
            self._clock = None
            self._transcript = None
            with contextlib.suppress(Exception):
                await runtime.speech_controller.stop()
            with contextlib.suppress(Exception):
                await runtime.transcription_controller.stop()
            with contextlib.suppress(Exception):
                await runtime.meeting_provider.leave()
            if created_runtime:
                await self._stop_runtime()
            raise

    async def leave_meeting(self) -> None:
        """Leave the current meeting."""
        runtime = self._require_runtime()
        try:
            await runtime.meeting_provider.leave()
        finally:
            with contextlib.suppress(Exception):
                await runtime.transcription_controller.stop()
            with contextlib.suppress(Exception):
                await runtime.speech_controller.stop()
            self._clock = None
            self._transcript = None
            await self._stop_runtime()

    async def speak_text(self, text: str) -> None:
        """Speak the provided text using TTS.

        Args:
            text (str): The text to be spoken.
        """
        runtime = self._require_runtime()
        try:
            await runtime.speech_controller.speak_text(text)
        except SpeechInterruptedError:
            await self.set_animation("interrupted")
            await self.set_animation(None)
            raise

    async def send_chat_message(self, message: str) -> None:
        """Send a chat message in the meeting.

        Args:
            message (str): The message to be sent.
        """
        runtime = self._require_runtime()
        async with self.animation("typing"):
            await runtime.meeting_provider.send_chat_message(message)

    async def get_chat_history(self) -> MeetingChatHistory:
        """Get the chat history from the meeting.

        Returns:
            MeetingChatHistory: The chat history of the meeting.
        """
        runtime = self._require_runtime()
        async with self.animation("reading"):
            return await runtime.meeting_provider.get_chat_history()

    async def get_participants(self) -> list[MeetingParticipant]:
        """Get the list of participants in the meeting.

        Returns:
            list[MeetingParticipant]: A list of participants in the meeting.
        """
        runtime = self._require_runtime()
        async with self.animation("reading"):
            return await runtime.meeting_provider.get_participants()

    async def get_video_snapshot(self) -> VideoSnapshot:
        """Get a snapshot of the current video feed.

        Returns:
            VideoSnapshot: The current video snapshot.
        """
        runtime = self._require_runtime()
        return await runtime.video_reader.snapshot()

    async def share_screen(self, url: str) -> None:
        """Start sharing screen in the meeting.

        Args:
            url: URL to display while sharing.
        """
        runtime = self._require_runtime()
        async with self.animation("sharing"):
            await runtime.meeting_provider.share_screen(url)

    async def stop_sharing(self) -> None:
        """Stop sharing screen in the meeting."""
        runtime = self._require_runtime()
        await runtime.meeting_provider.stop_sharing()

    async def mute(self) -> None:
        """Mute yourself in the meeting."""
        runtime = self._require_runtime()
        await runtime.meeting_provider.mute()

    async def unmute(self) -> None:
        """Unmute yourself in the meeting."""
        runtime = self._require_runtime()
        await runtime.meeting_provider.unmute()

    async def set_animation(self, animation: ActionAnimation | None) -> None:
        """Set an action animation on the meeting provider."""
        runtime = self._require_runtime()
        await runtime.meeting_provider.set_animation(animation)

    @asynccontextmanager
    async def animation(self, name: ActionAnimation) -> AsyncIterator[None]:
        """Show an action animation for the duration of the block."""
        await self.set_animation(name)
        try:
            yield
        finally:
            await self.set_animation(None)

    async def update_ui(self, update: UIUpdate) -> None:
        """Update the UI on the meeting provider.

        Args:
            update: The UI update to apply.
        """
        runtime = self._require_runtime()
        await runtime.meeting_provider.update_ui(update)
