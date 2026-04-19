import asyncio
import logging

logger = logging.getLogger(__name__)

_PACTL_RETRY_ATTEMPTS = 20
_PACTL_RETRY_DELAY_SECONDS = 0.1
_TRANSIENT_PACTL_ERROR_FRAGMENTS = (
    "connection refused",
    "connection failure",
    "connection terminated",
    "transport endpoint is not connected",
    "no such file or directory",
)


class PulseModuleManager:
    """A class to load and unload pulse modules via pactl."""

    async def _run_pactl(
        self,
        *cmd_args: str,
        env: dict[str, str] | None = None,
        retry_attempts: int = _PACTL_RETRY_ATTEMPTS,
        retry_delay_seconds: float = _PACTL_RETRY_DELAY_SECONDS,
    ) -> tuple[str, str]:
        """Run a pactl command with bounded retries for transient server races."""
        cmd = ["/usr/bin/pactl", *cmd_args]
        last_error = ""

        for attempt in range(1, retry_attempts + 1):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            stdout = stdout_bytes.decode().strip()
            stderr = stderr_bytes.decode().strip()
            if proc.returncode == 0:
                return stdout, stderr

            error_text = stderr or stdout or f"pactl exited with {proc.returncode}"
            last_error = error_text
            if attempt < retry_attempts and self._is_transient_pactl_error(error_text):
                logger.debug(
                    "pactl %s failed with a transient error on attempt %d/%d: %s",
                    " ".join(cmd_args),
                    attempt,
                    retry_attempts,
                    error_text,
                )
                await asyncio.sleep(retry_delay_seconds)
                continue
            break

        msg = f"Failed to run pactl {' '.join(cmd_args)}: {last_error}"
        logger.error(msg)
        raise RuntimeError(msg)

    @staticmethod
    def _is_transient_pactl_error(error_text: str) -> bool:
        """Return whether the pactl failure looks like a startup race."""
        lowered = error_text.lower()
        return any(
            fragment in lowered for fragment in _TRANSIENT_PACTL_ERROR_FRAGMENTS
        )

    async def _load_module(
        self, *cmd_args: str, env: dict[str, str] | None = None
    ) -> int:
        """Load a pulse module using pactl.

        Args:
            cmd_args: Arguments to pass to the pactl command.
            env: Optional environment variables to set for the command.

        Returns:
            The module id.
        """
        stdout, _stderr = await self._run_pactl("load-module", *cmd_args, env=env)
        return int(stdout)

    async def _unload_module(
        self, module_id: int, env: dict[str, str] | None = None
    ) -> None:
        """Unload a pulse module using pactl.

        Args:
            module_id: The ID of the module to unload.
            env: Optional environment variables to set for the command.

        Raises:
            RuntimeError: If the module unload fails.
        """
        await self._run_pactl("unload-module", str(module_id), env=env)
