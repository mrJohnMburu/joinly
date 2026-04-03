from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeploymentProfile:
    """A named deployment profile with recommended settings and assets."""

    name: str
    description: str
    settings: dict[str, Any]
    assets: tuple[str, ...] = ()


_PROFILE_OVERRIDE_BLOCKLIST = {"COMMANDLINE", "ENVIRONMENT"}

_PROFILES: dict[str, DeploymentProfile] = {
    "openclaw-pi": DeploymentProfile(
        name="openclaw-pi",
        description=(
            "Run Joinly as a lean MCP meeting bridge for OpenClaw on "
            "resource-constrained hosts such as a Raspberry Pi."
        ),
        settings={
            "vad": "webrtc",
            "stt": "deepgram",
            "tts": "deepgram",
            "meeting_provider_args": {
                "audio_only": True,
                "display_size": (1024, 576),
                "snapshot_size": (384, 216),
            },
        },
        assets=("playwright",),
    ),
}


def profile_names() -> tuple[str, ...]:
    """Return the available deployment profile names."""
    return tuple(_PROFILES)


def get_profile(profile_name: str) -> DeploymentProfile:
    """Return the requested deployment profile."""
    try:
        return _PROFILES[profile_name]
    except KeyError as exc:
        msg = f"Unknown deployment profile: {profile_name}"
        raise ValueError(msg) from exc


def apply_profile_defaults(
    settings: dict[str, Any],
    profile_name: str | None,
    parameter_sources: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Apply deployment profile defaults without overriding explicit user choices."""
    if profile_name is None:
        return deepcopy(settings)

    profile = get_profile(profile_name)
    merged = deepcopy(settings)
    sources = parameter_sources or {}

    for key, value in profile.settings.items():
        if sources.get(key) in _PROFILE_OVERRIDE_BLOCKLIST:
            continue

        current_value = merged.get(key)
        if isinstance(value, dict) and isinstance(current_value, dict):
            merged[key] = deepcopy(value) | deepcopy(current_value)
            continue

        merged[key] = deepcopy(value)

    return merged


def get_profile_assets(profile_name: str | None) -> tuple[str, ...]:
    """Return the assets required by a deployment profile."""
    if profile_name is None:
        return ()
    return get_profile(profile_name).assets
