import sys

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
