import re
import tomllib
from pathlib import Path


def _load_pyproject() -> dict[str, object]:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    return tomllib.loads(pyproject_path.read_text())


def _dependency_names(requirements: list[str]) -> set[str]:
    names = set()
    for requirement in requirements:
        match = re.match(r"^[A-Za-z0-9_.-]+", requirement)
        if match is not None:
            names.add(match.group(0))
    return names


def test_base_dependencies_stay_lightweight() -> None:
    pyproject = _load_pyproject()
    dependencies = pyproject["project"]["dependencies"]
    names = _dependency_names(dependencies)

    assert {
        "aiohttp",
        "click",
        "fastmcp",
        "joinly-common",
        "pydantic",
        "pydantic-settings",
        "python-dotenv",
        "semchunk",
    } <= names

    assert {
        "deepgram-sdk",
        "elevenlabs",
        "faster-whisper",
        "google-genai",
        "joinly-client",
        "kokoro-onnx",
        "numpy",
        "onnxruntime",
        "pillow",
        "playwright",
        "webrtcvad-wheels",
    }.isdisjoint(names)


def test_optional_extras_cover_browser_and_audio_profiles() -> None:
    pyproject = _load_pyproject()
    extras = pyproject["project"]["optional-dependencies"]

    assert {
        "browser",
        "client",
        "local-audio",
        "openclaw-pi",
        "remote-audio",
        "stt-whisper",
        "tts-kokoro",
        "vad-silero",
        "vad-webrtc",
    } <= set(extras)

    assert _dependency_names(extras["browser"]) == {
        "numpy",
        "pillow",
        "playwright",
    }
    assert _dependency_names(extras["vad-webrtc"]) == {"webrtcvad-wheels"}
    assert _dependency_names(extras["vad-silero"]) == {"numpy", "onnxruntime"}
    assert _dependency_names(extras["stt-whisper"]) == {"faster-whisper", "numpy"}
    assert _dependency_names(extras["tts-kokoro"]) == {"kokoro-onnx"}


def test_openclaw_pi_extra_stays_remote_audio_only() -> None:
    pyproject = _load_pyproject()
    extras = pyproject["project"]["optional-dependencies"]
    openclaw_pi = _dependency_names(extras["openclaw-pi"])

    assert openclaw_pi == {
        "deepgram-sdk",
        "numpy",
        "pillow",
        "playwright",
        "webrtcvad-wheels",
    }
