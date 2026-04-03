import asyncio
import contextlib
import json
import logging
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, TypeVar
from urllib.parse import urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController
from joinly.settings import get_settings
from joinly.types import MeetingChatHistory, MeetingChatMessage, MeetingParticipant

logger = logging.getLogger(__name__)

_TIME_RX = re.compile(r"^\d{1,2}:\d{2}(?:[AP]M)?$", re.IGNORECASE)
_MAX_MESSAGE_LENGTH = 500
_MAX_NAVIGATION_HTML_LOG_CHARS = 240
_NAVIGATION_TIMEOUT_MS = 20000
_NAVIGATION_STEP_DEADLINE_SECONDS = 25.0
_DIAGNOSTIC_STEP_DEADLINE_SECONDS = 1.0
_PREFLIGHT_NAVIGATION_TIMEOUT_MS = 10000
_PREFLIGHT_STEP_DEADLINE_SECONDS = 12.0
_POST_CLICK_JOIN_TIMEOUT_SECONDS = 30.0
_POST_CLICK_POLL_INTERVAL_MS = 500
_CLASSIFY_STATE_TIMEOUT_SECONDS = 5.0
_TRANSITIONING_SETTLE_COUNT = 3
_ACTIVE_SPEAKER_SETUP_TIMEOUT_SECONDS = 3.0

_WAITING_ROOM_TEXT_PATTERNS = (
    re.compile(r"asking to be let in", re.IGNORECASE),
    re.compile(r"someone lets you in", re.IGNORECASE),
    re.compile(r"no one else is here yet", re.IGNORECASE),
    re.compile(r"you're the first one here", re.IGNORECASE),
    re.compile(r"waiting for (?:the )?(?:host|others?|organizer)", re.IGNORECASE),
    re.compile(r"meeting (?:hasn't|has not) started", re.IGNORECASE),
)
_TERMINAL_FAILURE_TEXT_PATTERNS = (
    re.compile(r"you can't join this (?:video call|meeting)", re.IGNORECASE),
    re.compile(r"meeting code is invalid", re.IGNORECASE),
    re.compile(r"couldn't find the meeting", re.IGNORECASE),
    re.compile(r"(?:this )?meeting has ended", re.IGNORECASE),
    re.compile(r"call has ended", re.IGNORECASE),
)
_ACTIVE_MEETING_CONTROL_PATTERNS = (
    re.compile(r"leave", re.IGNORECASE),
    re.compile(r"exit call", re.IGNORECASE),
    re.compile(r"hang up", re.IGNORECASE),
    re.compile(r"end call", re.IGNORECASE),
    re.compile(r"turn (?:off|on) mic", re.IGNORECASE),
    re.compile(r"turn (?:off|on) camera", re.IGNORECASE),
)
_PREVIEW_JOIN_CONTROL_PATTERN = re.compile(
    r"(?:ask to join|request to join|join now|join meeting|join)",
    re.IGNORECASE,
)

_T = TypeVar("_T")


class GoogleMeetBrowserPlatformController(BaseBrowserPlatformController):
    """Controller for managing Google Meet browser meetings."""

    url_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:https?://)?(?:www\.)?meet\.google\.com/"
    )

    def __init__(
        self,
        *,
        navigation_preflight_urls: tuple[str, ...] = (),
        debug_artifact_dir: str | None = None,
    ) -> None:
        """Initialize the Google Meet browser platform controller."""
        self._state: dict[str, Any] = {}
        self._navigation_preflight_urls = tuple(navigation_preflight_urls)
        self._debug_artifact_dir = (
            Path(debug_artifact_dir) if debug_artifact_dir is not None else None
        )

    @property
    def active_speaker(self) -> str | None:
        """Get the name of the active speaker in the Google Meet meeting."""
        return self._state.get("active_speaker")

    async def join(
        self,
        page: Page,
        url: str,
        name: str,
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """Join the Google Meet meeting.

        Args:
            page: The Playwright page instance.
            url: The URL of the Google Meet meeting.
            name: The name of the participant.
            passcode: The passcode for the meeting (if required).
        """
        await self._run_navigation_preflight(page)
        logger.debug("Google Meet join: navigating to %s", url)
        try:
            await self._goto_with_deadline(
                page,
                url=url,
                wait_until="commit",
                timeout_ms=_NAVIGATION_TIMEOUT_MS,
                deadline_seconds=_NAVIGATION_STEP_DEADLINE_SECONDS,
            )
        except (PlaywrightTimeoutError, TimeoutError):
            await self._log_navigation_timeout(page, target_url=url)
            raise
        logger.debug("Google Meet join: navigation committed")

        name_field = page.get_by_placeholder(re.compile("name", re.IGNORECASE))
        logger.debug("Google Meet join: waiting for guest name field")
        try:
            await name_field.fill(name, timeout=60000)
        except PlaywrightTimeoutError:
            await self._log_join_step_timeout(
                page,
                target_url=url,
                step="name_field.fill",
            )
            raise
        logger.debug("Google Meet join: guest name entered")

        join_btn = page.get_by_role(
            "button", name=re.compile(r"^(?!.*other ways).*join.*$", re.IGNORECASE)
        )
        await self._capture_debug_snapshot(page, stage="pre_click")
        logger.debug("Google Meet join: clicking join button")
        try:
            await join_btn.click(timeout=10000, force=True, no_wait_after=True)
        except PlaywrightTimeoutError:
            await self._log_join_step_timeout(
                page,
                target_url=url,
                step="join_button.click",
            )
            raise
        await self._capture_debug_snapshot(page, stage="post_click")

        if not await self._check_joined(page):
            await self._log_join_step_timeout(
                page,
                target_url=url,
                step="post_click.join_state",
                message="Google Meet join did not reach active or waiting state",
            )
            msg = "Join check failed: Failed to join the Google Meet meeting."
            raise RuntimeError(msg)

        await self._setup_active_speaker_observer_with_timeout(page)

    async def leave(self, page: Page) -> None:
        """Leave the Google Meet meeting.

        Args:
            page: The Playwright page instance.
        """
        await self._dismiss_dialog(page)

        leave_btn = page.get_by_role("button", name=re.compile(r"leave", re.IGNORECASE))
        if not await leave_btn.is_visible():
            msg = "Leave button not found or not visible."
            raise RuntimeError(msg)
        await leave_btn.click(timeout=1000)
        await page.wait_for_timeout(500)

    async def send_chat_message(self, page: Page, message: str) -> None:
        """Send a chat message in the Google Meet meeting.

        Args:
            page: The Playwright page instance.
            message: The message to send.
        """
        if len(message) > _MAX_MESSAGE_LENGTH:
            msg = (
                f"Message exceeds the maximum length of {_MAX_MESSAGE_LENGTH} "
                f"characters, got {len(message)}."
            )
            raise ValueError(msg)

        await self._open_chat(page)

        chat_input = page.locator("textarea[placeholder*='Send a message']")
        if not await chat_input.is_visible():
            msg = "Chat input not found or not visible."
            raise RuntimeError(msg)
        await chat_input.fill(message)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """Get the chat history from a Google Meet meeting."""
        await self._open_chat(page)

        messages: list[MeetingChatMessage] = []

        chat_panel = page.locator('aside[aria-label="Side panel"]')
        blobs = await chat_panel.locator("div:has(> div > div[data-message-id])").all()

        for blob in blobs:
            header = blob.locator(":scope > div").first
            inner_text = await header.inner_text()
            parts = [p.strip() for p in inner_text.splitlines() if p.strip()]

            sender: str | None = None
            ts: str | None = None
            for part in parts:
                clean = re.sub(r"[\u00A0\u202F]", "", part).strip()

                if _TIME_RX.fullmatch(clean):
                    ts = clean
                elif sender is None:
                    sender = clean or None

            bubbles = await blob.locator("div[data-message-id]").all()
            for bubble in bubbles:
                el = bubble.locator(
                    "div:not(:has(*:not(a)))", has_text=re.compile(r"\S")
                ).first
                text = (await el.inner_text()).strip() if await el.count() else None
                if text:
                    messages.append(
                        MeetingChatMessage(text=text, timestamp=ts, sender=sender)
                    )

        return MeetingChatHistory(messages=messages)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """Get the list of participants in the Google Meet meeting.

        Args:
            page: The Playwright page instance.

        Returns:
            list[MeetingParticipant]: A list of participants in the meeting.
        """
        await self._dismiss_dialog(page)

        participants_list = page.locator('div[aria-label="Participants"][role="list"]')
        is_participant_list_visible = await participants_list.is_visible()

        if not is_participant_list_visible:
            participants_button = page.get_by_role(
                "button", name=re.compile(r"^people", re.IGNORECASE)
            )
            if not await participants_button.is_visible():
                msg = "Participants button not found or not visible."
                raise RuntimeError(msg)
            await participants_button.click()
            await page.wait_for_timeout(1000)
            if not await participants_list.is_visible():
                await page.wait_for_timeout(1000)

        participants: list[MeetingParticipant] = []
        for item in await participants_list.locator("div[role='listitem']").all():
            name = await item.get_attribute("aria-label")
            infos = []
            if await item.locator('span:has-text("(You)")').count() > 0:
                infos.append("You")
            if await item.locator('div:has-text("Meeting host")').count() > 0:
                infos.append("Meeting host")
            unmute_btn = item.get_by_role(
                "button", name=re.compile(r"unmute", re.IGNORECASE)
            )
            mute_btn = item.get_by_role(
                "button", name=re.compile(r"mute", re.IGNORECASE)
            )
            if await unmute_btn.count() > 0:
                infos.append("Muted")
            elif await mute_btn.count() > 0:
                infos.append("Unmuted")
            if name:
                participants.append(MeetingParticipant(name=name, infos=infos))

        return participants

    async def mute(self, page: Page) -> None:
        """Mute the participant in the Google Meet meeting.

        Args:
            page: The Playwright page instance.
        """
        await self._dismiss_dialog(page)

        mute_btn = page.get_by_role(
            "button", name=re.compile(r"^turn off mic", re.IGNORECASE)
        )
        if await mute_btn.is_visible():
            await mute_btn.click(timeout=1000)
        elif not await page.get_by_role(
            "button", name=re.compile(r"^turn on mic", re.IGNORECASE)
        ).is_visible():
            msg = "Mute button not found or not visible."
            raise RuntimeError(msg)

    async def unmute(self, page: Page) -> None:
        """Unmute the participant in the Google Meet meeting.

        Args:
            page: The Playwright page instance.
        """
        await self._dismiss_dialog(page)

        unmute_btn = page.get_by_role(
            "button", name=re.compile(r"^turn on mic", re.IGNORECASE)
        )
        if await unmute_btn.is_visible():
            await unmute_btn.click(timeout=1000)
        elif not await page.get_by_role(
            "button", name=re.compile(r"^turn off mic", re.IGNORECASE)
        ).is_visible():
            msg = "Unmute button not found or not visible."
            raise RuntimeError(msg)

    async def share_screen(self, page: Page) -> None:
        """Start sharing screen in the Google Meet meeting.

        Args:
            page: The Playwright page instance.
        """
        await self._dismiss_dialog(page)

        share_btn = page.get_by_role(
            "button",
            name=re.compile(r"present now|share screen", re.IGNORECASE),
        ).first
        if not await share_btn.is_visible():
            msg = "Share/Present button not found or not visible."
            raise RuntimeError(msg)
        await share_btn.click(timeout=5000)
        await page.wait_for_timeout(2000)

    async def stop_sharing(self, page: Page) -> None:
        """Stop sharing screen in the Google Meet meeting.

        Args:
            page: The Playwright page instance.
        """
        await self._dismiss_dialog(page)

        stop_btn = page.get_by_role(
            "button",
            name=re.compile(r"stop (sharing|present)", re.IGNORECASE),
        ).first
        if not await stop_btn.is_visible():
            msg = "Stop sharing button not found or not visible."
            raise RuntimeError(msg)
        await stop_btn.click(timeout=2000)
        await page.wait_for_timeout(500)

    async def _check_joined(
        self,
        page: Page,
        timeout: float = _POST_CLICK_JOIN_TIMEOUT_SECONDS,
    ) -> bool:  # noqa: ASYNC109
        """Check if the Google Meet meeting has been joined successfully.

        Uses a single in-page JS evaluation per poll cycle instead of multiple
        Playwright locator roundtrips.  On ARM hardware (Raspberry Pi 5) each
        Playwright CDP call adds significant overhead; replacing 8-10 roundtrips
        with one keeps every iteration under ~2 seconds instead of ~14 seconds.

        Args:
            page: The Playwright page instance.
            timeout: The timeout in seconds for checking the join status.

        Returns:
            bool: True if joined (active or waiting-room), False on terminal
                failure, False on timeout.
        """
        deadline = time.monotonic() + timeout
        iteration = 0
        transitioning_count = 0

        while time.monotonic() < deadline:
            iteration += 1
            iter_start = time.monotonic()

            await self._dismiss_dialog(page, timeout=0)

            state = await self._classify_meeting_state(page)

            iter_elapsed = time.monotonic() - iter_start
            logger.debug(
                "Google Meet post-click probe "
                "iteration=%d state=%s elapsed=%.2fs remaining=%.1fs",
                iteration,
                state,
                iter_elapsed,
                deadline - time.monotonic(),
            )

            if state == "active":
                return True
            if state == "waiting":
                return True
            if state == "failed":
                return False

            # "transitioning": preview controls gone but no active controls yet.
            # Stabilise over several polls before treating it as joined so we
            # do not return prematurely during the brief animation between
            # click and the in-call UI appearing.
            if state == "transitioning":
                transitioning_count += 1
                if transitioning_count >= _TRANSITIONING_SETTLE_COUNT:
                    logger.info(
                        "Google Meet join: assuming admitted after %d "
                        "consecutive transitioning states.",
                        transitioning_count,
                    )
                    return True
            else:
                transitioning_count = 0

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await page.wait_for_timeout(
                min(_POST_CLICK_POLL_INTERVAL_MS, int(remaining * 1000))
            )

        return False

    async def _classify_meeting_state(self, page: Page) -> str:
        """Classify the current Google Meet page state via a single JS call.

        Replaces multiple Playwright locator roundtrips with one CDP message.
        The JS runs inside the renderer process so it has zero serialisation
        overhead for the DOM — it only returns a short state string.

        Returns one of:
            ``"preview"``      – prejoin UI is still visible
            ``"active"``       – in-call controls visible (mic/leave buttons)
            ``"waiting"``      – waiting room / alone-in-room text present
            ``"failed"``       – terminal error text detected
            ``"transitioning"``– preview controls gone but in-call UI not yet
            ``"unknown"``      – nothing matched; keep polling
        """
        _js = """
() => {
    const lower = (document.body && document.body.innerText)
        ? document.body.innerText.toLowerCase()
        : '';
    const isVisible = (el) => {
        if (!el) return false;
        if (el.offsetParent === null && el.getClientRects().length === 0) {
            return false;
        }
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden';
    };

    // --- Terminal failure ---
    const failures = [
        "you can't join this video call",
        "you can't join this meeting",
        "meeting code is invalid",
        "couldn't find the meeting",
        "this meeting has ended",
        "meeting has ended",
        "call has ended",
    ];
    for (const f of failures) {
        if (lower.includes(f)) return 'failed';
    }

    // --- Waiting room / alone-in-room ---
    const waiting = [
        'asking to be let in',
        'someone lets you in',
        'no one else is here yet',
        "you're the first one here",
        'waiting for the host',
        'waiting for others',
        'waiting for organizer',
        "meeting hasn't started",
        'meeting has not started',
    ];
    for (const w of waiting) {
        if (lower.includes(w)) return 'waiting';
    }

    // --- Prejoin preview UI ---
    const hasVisibleNameField = [...document.querySelectorAll(
        'input[placeholder], input[aria-label], textarea[placeholder], textarea[aria-label]'
    )].some(el => {
        const label = `${el.getAttribute('placeholder') || ''} ${el.getAttribute('aria-label') || ''}`;
        return isVisible(el) && /\bname\b/i.test(label);
    });
    const hasVisibleNamePrompt = [...document.querySelectorAll(
        'div, span, label, h1, h2, h3, p'
    )].some(el => isVisible(el) && /what'?s your name?/i.test(el.textContent || ''));
    const hasVisibleJoinBtn = [...document.querySelectorAll(
        'button, [role="button"]'
    )].some(el => {
        const label = `${el.textContent || ''} ${el.getAttribute('aria-label') || ''}`;
        return isVisible(el) && /^(?!.*other ways).*join.*/i.test(label.trim());
    });
    if (hasVisibleNameField || hasVisibleNamePrompt || hasVisibleJoinBtn) {
        return 'preview';
    }

    // --- Active meeting controls (in-call UI visible) ---
    const activeLabels = [
        /leave/i, /exit call/i, /hang up/i, /end call/i,
        /turn off mic/i, /turn on mic/i,
        /turn off camera/i, /turn on camera/i,
    ];
    for (const btn of document.querySelectorAll('button[aria-label]')) {
        const label = btn.getAttribute('aria-label') || '';
        if (isVisible(btn) && activeLabels.some(rx => rx.test(label))) {
            return 'active';
        }
    }

    // --- Transitioning: preview controls gone, in-call UI not yet visible ---
    return 'transitioning';

}
"""
        try:
            result = await asyncio.wait_for(
                page.evaluate(_js),
                timeout=_CLASSIFY_STATE_TIMEOUT_SECONDS,
            )
            return result if isinstance(result, str) else "unknown"
        except Exception:
            logger.debug(
                "Meeting state classification failed", exc_info=True
            )
            return "unknown"

    async def _dismiss_dialog(self, page: Page, timeout: int = 100) -> None:  # noqa: ASYNC109
        """Dismiss any popups that may appear."""
        action_btn = page.locator("div[role='dialog'] [data-mdc-dialog-action]")
        with contextlib.suppress(Exception):
            await action_btn.first.click(timeout=timeout)

    async def _has_active_meeting_controls(self, page: Page) -> bool:
        """Return True when in-call controls are already visible."""
        for pattern in _ACTIVE_MEETING_CONTROL_PATTERNS:
            locator = page.get_by_role("button", name=pattern)
            if await self._locator_is_visible(locator):
                return True
        return False

    async def _preview_join_controls_visible(self, page: Page) -> bool:
        """Return True when preview-only join controls are still visible."""
        if await self._locator_is_visible(
            page.get_by_placeholder(re.compile("name", re.IGNORECASE))
        ):
            return True
        return await self._locator_is_visible(
            page.get_by_role("button", name=_PREVIEW_JOIN_CONTROL_PATTERN)
        )

    async def _locator_is_visible(self, locator: Any) -> bool:
        """Safely check whether a locator is visible."""
        with contextlib.suppress(Exception):
            return await asyncio.wait_for(
                locator.is_visible(),
                timeout=_DIAGNOSTIC_STEP_DEADLINE_SECONDS,
            )
        return False

    async def _read_page_text(self, page: Page) -> str:
        """Return normalized text content for state classification."""
        html_content = await self._read_page_diagnostic(
            lambda: page.content(),
            fallback="",
        )
        return self._strip_html(html_content)

    @staticmethod
    def _matches_any_pattern(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
        """Return True when any pattern matches the normalized text."""
        return any(pattern.search(text) for pattern in patterns)

    @classmethod
    def _looks_like_live_meeting_url(cls, current_url: str) -> bool:
        """Return True when the page is on a concrete Meet room URL."""
        if not current_url or not cls.url_pattern.match(current_url):
            return False

        parsed = urlparse(current_url)
        path = parsed.path.strip("/")
        return bool(path) and path.lower() not in {"landing"}

    @staticmethod
    def _strip_html(content: str) -> str:
        """Return normalized visible text from HTML content."""
        without_non_text = re.sub(
            r"(?is)<(script|style).*?>.*?</\1>",
            " ",
            content,
        )
        without_tags = re.sub(r"(?s)<[^>]+>", " ", without_non_text)
        return re.sub(r"\s+", " ", without_tags).strip()

    async def _log_navigation_timeout(self, page: Page, *, target_url: str) -> None:
        """Log browser state when Google Meet navigation stalls."""
        await self._log_join_step_timeout(
            page,
            target_url=target_url,
            step="navigate",
            message="Google Meet navigation timed out",
        )

    async def _log_join_step_timeout(
        self,
        page: Page,
        *,
        target_url: str,
        step: str,
        message: str = "Google Meet join step timed out",
    ) -> None:
        """Log browser state when a Google Meet join step stalls."""
        current_url = getattr(page, "url", "<unavailable>")
        title = await self._read_page_diagnostic(
            lambda: page.title(),
            fallback="<unavailable>",
        )
        html_content = await self._read_page_diagnostic(
            lambda: page.content(),
            fallback="<unavailable>",
        )
        html_snippet = self._summarize_html(html_content)
        screenshot_path = "<unavailable>"

        candidate_screenshot_path = str(
            Path(tempfile.gettempdir())
            / f"joinly-google-meet-timeout-{int(time.time() * 1000)}.png"
        )
        captured_screenshot = await self._read_page_diagnostic(
            lambda: page.screenshot(path=candidate_screenshot_path, type="png"),
            fallback=None,
        )
        if captured_screenshot is not None:
            screenshot_path = candidate_screenshot_path

        logger.error(
            "%s "
            "(step=%s target_url=%s current_url=%s title=%r screenshot_path=%s html_snippet=%s)",
            message,
            step,
            target_url,
            current_url,
            title,
            screenshot_path,
            html_snippet,
        )

    async def _capture_debug_snapshot(self, page: Page, *, stage: str) -> None:
        """Write optional debug artifacts for the current Meet page state."""
        if self._debug_artifact_dir is None:
            return

        artifact_dir = self._debug_artifact_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)

        timestamp_ms = int(time.time() * 1000)
        stage_slug = re.sub(r"[^a-z0-9_-]+", "-", stage.lower()).strip("-") or "stage"
        prefix = artifact_dir / f"joinly-google-meet-{timestamp_ms}-{stage_slug}"

        screenshot_bytes = await self._read_page_diagnostic(
            lambda: page.screenshot(type="png"),
            fallback=None,
        )
        html_content = await self._read_page_diagnostic(
            lambda: page.content(),
            fallback="<unavailable>",
        )
        title = await self._read_page_diagnostic(
            lambda: page.title(),
            fallback="<unavailable>",
        )
        classified_state = await self._read_page_diagnostic(
            lambda: self._classify_meeting_state(page),
            fallback="unknown",
        )

        metadata = {
            "stage": stage,
            "captured_at_ms": timestamp_ms,
            "current_url": getattr(page, "url", "<unavailable>"),
            "title": title,
            "classified_state": classified_state,
        }

        try:
            if screenshot_bytes is not None:
                prefix.with_suffix(".png").write_bytes(screenshot_bytes)
            prefix.with_suffix(".html").write_text(html_content, encoding="utf-8")
            prefix.with_suffix(".json").write_text(
                json.dumps(metadata, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            logger.warning(
                "Failed to write Google Meet debug artifacts for stage %s.",
                stage,
                exc_info=True,
            )
            return

        logger.info(
            "Captured Google Meet debug artifacts "
            "(stage=%s artifact_prefix=%s)",
            stage,
            prefix,
        )

    @staticmethod
    def _summarize_html(content: str) -> str:
        """Return a compact HTML snippet for timeout diagnostics."""
        squashed = re.sub(r"\s+", " ", content).strip()
        if not squashed:
            return "<empty>"
        if len(squashed) <= _MAX_NAVIGATION_HTML_LOG_CHARS:
            return squashed
        return squashed[: _MAX_NAVIGATION_HTML_LOG_CHARS - 3] + "..."

    async def _read_page_diagnostic(
        self,
        reader: Callable[[], Awaitable[_T]],
        *,
        fallback: _T,
    ) -> _T:
        """Return a bounded diagnostic value from a potentially stuck page."""
        try:
            return await asyncio.wait_for(
                reader(),
                timeout=_DIAGNOSTIC_STEP_DEADLINE_SECONDS,
            )
        except Exception:
            return fallback

    async def _run_navigation_preflight(self, page: Page) -> None:
        """Probe a few simpler URLs before loading the full Meet join URL."""
        for preflight_url in self._navigation_preflight_urls:
            logger.debug("Google Meet preflight: navigating to %s", preflight_url)
            try:
                await self._goto_with_deadline(
                    page,
                    url=preflight_url,
                    wait_until="domcontentloaded",
                    timeout_ms=_PREFLIGHT_NAVIGATION_TIMEOUT_MS,
                    deadline_seconds=_PREFLIGHT_STEP_DEADLINE_SECONDS,
                )
            except (PlaywrightTimeoutError, TimeoutError):
                current_url = getattr(page, "url", "<unavailable>")
                title = await self._read_page_diagnostic(
                    lambda: page.title(),
                    fallback="<unavailable>",
                )
                logger.warning(
                    "Google Meet preflight failed "
                    "(target_url=%s current_url=%s title=%r)",
                    preflight_url,
                    current_url,
                    title,
                )
                continue

            current_url = getattr(page, "url", "<unavailable>")
            title = await self._read_page_diagnostic(
                lambda: page.title(),
                fallback="<unavailable>",
            )
            logger.debug(
                "Google Meet preflight succeeded "
                "(target_url=%s current_url=%s title=%r)",
                preflight_url,
                current_url,
                title,
            )

    async def _goto_with_deadline(
        self,
        page: Page,
        *,
        url: str,
        wait_until: str,
        timeout_ms: int,
        deadline_seconds: float,
    ) -> None:
        """Navigate with both Playwright and outer asyncio deadlines."""
        await asyncio.wait_for(
            page.goto(url, wait_until=wait_until, timeout=timeout_ms),
            timeout=deadline_seconds,
        )

    async def _setup_active_speaker_observer_with_timeout(self, page: Page) -> None:
        """Try to enable active speaker tracking without blocking the join."""
        try:
            await asyncio.wait_for(
                self._setup_active_speaker_observer(page),
                timeout=_ACTIVE_SPEAKER_SETUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("Active speaker observer setup timed out.")
        except Exception:
            logger.warning("Active speaker observer setup failed.", exc_info=True)

    async def _open_chat(self, page: Page) -> None:
        """Open the chat in the Google Meet meeting."""
        await self._dismiss_dialog(page)

        chat_input = page.locator("textarea[placeholder*='Send a message']")
        is_chat_visible = await chat_input.is_visible()

        if not is_chat_visible:
            chat_button = page.get_by_role(
                "button", name=re.compile(r"^chat", re.IGNORECASE)
            )
            if not await chat_button.is_visible():
                msg = "Chat button not found or not visible."
                raise RuntimeError(msg)
            await chat_button.click()
            await page.wait_for_timeout(1000)
            if not await chat_input.is_visible():
                await page.wait_for_timeout(1000)

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """Setup the active speaker observer for Google Meet."""
        await page.expose_binding(
            "report",
            lambda _, name: self._state.update({"active_speaker": name}),
        )
        await page.evaluate(
            """
            (nameArg) => {
                const emit = n => window.report(n);
                const find = () => {
                    for (
                        const t of document.querySelectorAll('div[data-participant-id]')
                    ) {
                        if (![...t.querySelectorAll('div')].some(d =>
                                !d.children.length &&
                                getComputedStyle(d).display === 'none' &&
                                parseFloat(getComputedStyle(d).borderTopWidth) > 3
                            ))
                        {
                            const el = t.querySelector('span.notranslate')
                            const name = el?.textContent.trim();
                            if (name && name.length > 0 && name !== nameArg)
                                return name;
                        }
                    }
                    return null;
                };

                let last = null, cur;
                new MutationObserver(() => {
                    cur = find();
                    if (cur !== last) { last = cur; emit(cur); }
                }).observe(
                    document,
                    {
                        subtree: true,
                        childList: true,
                        attributes: true,
                        attributeFilter: ['style', 'class']
                    }
                );
                emit(find());
            }
            """,
            get_settings().name,
        )
