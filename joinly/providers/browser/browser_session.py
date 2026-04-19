import contextlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Self

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from joinly.utils.logging import LOGGING_TRACE

logger = logging.getLogger(__name__)

_PROFILE_LOCK_ERROR_SNIPPETS = (
    "Failed to create a ProcessSingleton",
    "Failed to create ",
    "SingletonLock",
)


class BrowserSession:
    """A class to represent a browser session using Playwright."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        cdp_port: int = 0,
        executable_path: str | Path | None = None,
        profile_dir: str | Path | None = None,
        net_log_path: str | Path | None = None,
        window_size: tuple[int, int] = (1280, 720),
        software_rendering: bool = False,
    ) -> None:
        """Initialize the browser params.

        Args:
            env: Environment variables to set for the browser (default: None)
            cdp_port (int): The port for the CDP connection (default: 0, auto-assign)
            executable_path: Optional browser executable path. Uses the Playwright
                Chromium binary when not provided.
            profile_dir: Optional browser profile directory. When provided, it is
                reused across runs instead of creating a temporary profile.
            net_log_path: Optional Chromium net log output path.
            window_size: Browser window size passed to Chromium.
            software_rendering: Whether to prefer SwiftShader-backed software
                rendering instead of the default Pi/browser flags.
        """
        self._env: dict[str, str] = env if env is not None else os.environ.copy()
        self._cdp_port: int = cdp_port
        self._executable_path = Path(executable_path).expanduser() if executable_path else None
        self._persistent_profile_dir = (
            Path(profile_dir).expanduser() if profile_dir else None
        )
        self._net_log_path = Path(net_log_path).expanduser() if net_log_path else None
        self._window_size = window_size
        self._software_rendering = software_rendering

        self._profile_dir: tempfile.TemporaryDirectory | None = None
        self._profile_path: Path | None = None
        self._playwright: Playwright | None = None
        self._pw_browser: PlaywrightBrowser | None = None
        self._pw_context: BrowserContext | None = None
        self._default_page: Page | None = None
        self._pages = list[Page]()
        self.cdp_url: str | None = None

    def _get_browser_executable_path(self) -> Path:
        if self._executable_path is not None:
            return self._executable_path
        if self._playwright is None:
            msg = "Playwright is not initialized."
            raise RuntimeError(msg)
        return Path(self._playwright.chromium.executable_path)

    def _get_profile_dir(self) -> Path:
        if self._persistent_profile_dir is not None:
            self._persistent_profile_dir.mkdir(parents=True, exist_ok=True)
            self._profile_path = self._persistent_profile_dir
            return self._persistent_profile_dir

        return self._create_temporary_profile_dir()

    def _create_temporary_profile_dir(self) -> Path:
        self._profile_dir = tempfile.TemporaryDirectory(prefix="pw-profile_")
        self._profile_path = Path(self._profile_dir.name)
        return self._profile_path

    @staticmethod
    def _is_profile_lock_error(exc: Exception) -> bool:
        message = str(exc)
        return all(snippet in message for snippet in _PROFILE_LOCK_ERROR_SNIPPETS)

    async def _launch_context(self, profile_dir: Path) -> BrowserContext:
        return await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=str(self._get_browser_executable_path()),
            headless=False,
            chromium_sandbox=False,
            ignore_default_args=self._build_ignore_default_args(),
            args=self._build_launch_args(),
            env=self._env,
        )

    def _build_launch_args(self) -> list[str]:
        chromium_args = self._build_chromium_args()
        if self._net_log_path is not None:
            self._net_log_path.parent.mkdir(parents=True, exist_ok=True)
            chromium_args.extend(
                [
                    f"--log-net-log={self._net_log_path}",
                    "--net-log-capture-mode=Everything",
                ]
            )
            logger.debug("Chromium net log path: %s", self._net_log_path)
        return chromium_args

    async def __aenter__(self) -> Self:
        """Start and connect to the Playwright browser."""
        self._playwright = await async_playwright().start()

        bin_path = self._get_browser_executable_path()
        logger.debug("Chromium binary path: %s", bin_path)
        if not bin_path.exists():
            msg = "Chromium binary not found"
            logger.error(msg)
            raise RuntimeError(msg)

        profile_dir = self._get_profile_dir()
        logger.debug("Profile directory ready at: %s", profile_dir)
        logger.debug("Launching Chromium browser context.")
        try:
            self._pw_context = await self._launch_context(profile_dir)
        except Exception as exc:
            if (
                self._persistent_profile_dir is None
                or not self._is_profile_lock_error(exc)
            ):
                raise
            logger.warning(
                "Persistent Chromium profile is locked; retrying with an isolated "
                "temporary profile: %s",
                self._persistent_profile_dir,
            )
            profile_dir = self._create_temporary_profile_dir()
            logger.debug("Fallback profile directory ready at: %s", profile_dir)
            self._pw_context = await self._launch_context(profile_dir)
        await self._grant_media_permissions()
        self._pw_browser = self._pw_context.browser
        self._default_page = self._pw_context.pages[0] if self._pw_context.pages else None

        logger.debug("Playwright started.")

        return self

    def _build_chromium_args(self) -> list[str]:
        disable_features = [
            "TranslateUI",
            "MediaRouter",
            "WebRtcAutomaticGainControl",
        ]
        chromium_args = [
            # "--use-fake-ui-for-media-stream",  # removed: causes Teams mic to be greyed out
            "--alsa-output-device=pulse",
            f"--alsa-input-device={self._env.get('PULSE_SOURCE')}",
            "--autoplay-policy=no-user-gesture-required",
            "--allow-http-screen-capture",
            "--auto-select-desktop-capture-source=Entire",
            "--enable-usermedia-screen-capturing",
            "--enable-features=WebRTCPipeWireCapturer",
            "--ozone-platform=x11",
            "--disable-focus-on-load",
            f"--window-size={self._window_size[0]},{self._window_size[1]}",
            "--lang=en-US",
            "--no-sandbox",  # required for docker
            "--disable-dev-shm-usage",
            "--disable-gpu-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--no-xshm",
            "--force-device-scale-factor=1",
            # Disable keyring/OSCrypt access: on Linux, without this Chromium tries to
            # encrypt cookies via gnome-keyring/D-Bus secret service at page load time.
            # A locked keyring (e.g. fresh boot/login) blocks Chromium here indefinitely.
            "--password-store=basic",
            "--use-mock-keyring",
            "--disable-backgrounding-occluded-windows",
        ]
        if self._software_rendering:
            disable_features.extend(["Vulkan", "UseSkiaRenderer"])
            chromium_args.extend(
                [
                    "--disable-gpu-rasterization",
                    "--use-gl=angle",
                    "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader",
                ]
            )
        else:
            chromium_args.append("--disable-gpu")

        chromium_args.append(f"--disable-features={','.join(disable_features)}")
        return chromium_args

    async def _grant_media_permissions(self) -> None:
        """Pre-grant browser media permissions for meeting providers.

        Fresh Chromium profiles can surface first-run microphone/camera prompts
        that block Meet's preview join button. Granting permissions up front keeps
        the join flow profile-independent.
        """
        if self._pw_context is None:
            return

        with contextlib.suppress(Exception):
            await self._pw_context.grant_permissions(["microphone", "camera"])
            logger.debug("Granted browser media permissions for the session.")

    def _build_ignore_default_args(self) -> list[str]:
        ignored = ["--mute-audio"]
        if self._software_rendering:
            ignored.extend(
                [
                    "--use-angle=gles",
                    "--enable-gpu-rasterization",
                ]
            )
        return ignored

    async def __aexit__(self, *exc: object) -> None:
        """Stop the browser."""
        logger.debug("Stopping browser.")

        if self._pw_context is not None:
            await self._pw_context.close()
        if self._playwright:
            await self._playwright.stop()
        logger.debug("Browser stopped.")

        if self._profile_dir is not None:
            self._profile_dir.cleanup()
            logger.debug("Profile directory removed: %s", self._profile_dir.name)

        self._pw_context = None
        self._pw_browser = None
        self._playwright = None
        self._profile_dir = None
        self._profile_path = None
        self._default_page = None
        self._pages = []
        self.cdp_url = None

    async def get_page(self) -> Page:
        """Get a new page in the browser context."""
        if self._pw_context is None:
            msg = "Playwright context is not initialized."
            raise RuntimeError(msg)

        if (
            self._default_page is not None
            and not self._default_page.is_closed()
            and self._default_page not in self._pages
        ):
            page = self._default_page
            logger.debug("Reusing default page in the browser context.")
        else:
            page = await self._pw_context.new_page()
            logger.debug("New page created in the browser context.")

        self._attach_page_listeners(page)
        if page not in self._pages:
            self._pages.append(page)

        return page

    def _attach_page_listeners(self, page: Page) -> None:
        page.on(
            "console",
            lambda msg: logger.log(
                LOGGING_TRACE, "[console][%s] %s", msg.type, msg.text
            ),
        )
        page.on(
            "pageerror",
            lambda exc: logger.warning("Playwright pageerror: %s", exc),
        )
        page.on(
            "requestfailed",
            lambda request: logger.warning(
                "Playwright request failed: url=%s failure=%s",
                getattr(request, "url", "<unknown>"),
                self._request_failure_text(request),
            ),
        )

    @staticmethod
    def _request_failure_text(request: object) -> str:
        with contextlib.suppress(Exception):
            failure = getattr(request, "failure")
            details = failure() if callable(failure) else failure
            if isinstance(details, dict):
                return str(details.get("errorText") or details)
            if details:
                return str(details)
        return "<unknown>"
