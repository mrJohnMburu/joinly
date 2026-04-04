import sys
from types import SimpleNamespace

from click.testing import CliRunner

from joinly.main import cli


def test_client_mode_requires_optional_client_dependency(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "path",
        [path for path in sys.path if not path.endswith("/joinly/client")],
    )
    sys.modules.pop("joinly_client", None)

    result = CliRunner().invoke(
        cli,
        ["--client", "https://meet.google.com/test-call"],
    )

    assert result.exit_code == 2
    assert "Install the optional client dependencies" in result.output


def test_openclaw_pi_profile_applies_client_name_and_prompt_style(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_run(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "joinly_client",
        SimpleNamespace(run=_fake_run),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--client",
            "--profile",
            "openclaw-pi",
            "https://teams.microsoft.com/meet/example",
        ],
    )

    assert result.exit_code == 0
    assert captured["name"] == "OpenClaw"
    assert captured["prompt_style"] == "dyadic"
