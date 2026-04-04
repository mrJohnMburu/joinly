from joinly.profiles import apply_profile_defaults, get_profile, get_profile_assets


def test_openclaw_pi_profile_uses_remote_audio_defaults() -> None:
    profile = get_profile("openclaw-pi")

    assert profile.name == "openclaw-pi"
    assert profile.settings["name"] == "OpenClaw"
    assert profile.option_defaults["prompt_style"] == "dyadic"
    assert profile.settings["vad"] == "webrtc"
    assert profile.settings["stt"] == "deepgram"
    assert profile.settings["tts"] == "deepgram"
    assert profile.settings["meeting_provider_args"] == {
        "audio_only": True,
        "browser_software_rendering": True,
        "display_size": (1024, 576),
        "snapshot_size": (384, 216),
        "browser_net_log_path": "/tmp/joinly-chromium-netlog.json",
        "google_meet_debug_artifact_dir": "/tmp/joinly-google-meet-debug",
        "google_meet_preflight_urls": (
            "https://www.google.com",
            "https://meet.google.com",
        ),
    }


def test_apply_profile_defaults_only_overrides_defaulted_values() -> None:
    settings = {
        "vad": "silero",
        "stt": "whisper",
        "tts": "kokoro",
        "meeting_provider_args": {"snapshot_size": (512, 288)},
    }
    sources = {
        "vad": "DEFAULT",
        "stt": "ENVIRONMENT",
        "tts": "COMMANDLINE",
        "meeting_provider_args": "DEFAULT",
    }

    merged = apply_profile_defaults(settings, "openclaw-pi", sources)

    assert merged["vad"] == "webrtc"
    assert merged["stt"] == "whisper"
    assert merged["tts"] == "kokoro"
    assert merged["meeting_provider_args"] == {
        "audio_only": True,
        "browser_software_rendering": True,
        "display_size": (1024, 576),
        "snapshot_size": (512, 288),
        "browser_net_log_path": "/tmp/joinly-chromium-netlog.json",
        "google_meet_debug_artifact_dir": "/tmp/joinly-google-meet-debug",
        "google_meet_preflight_urls": (
            "https://www.google.com",
            "https://meet.google.com",
        ),
    }


def test_openclaw_pi_profile_only_downloads_playwright_assets() -> None:
    assert get_profile_assets("openclaw-pi") == ("playwright",)
