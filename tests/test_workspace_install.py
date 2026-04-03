from pathlib import Path

from joinly.workspace_install import build_install_commands, project_install_spec


def test_project_install_spec_includes_requested_extras_in_order() -> None:
    repo_root = Path("/tmp/joinly")

    spec = project_install_spec(repo_root, ["openclaw-pi", "client"])

    assert spec == "/tmp/joinly[openclaw-pi,client]"


def test_build_install_commands_installs_local_common_before_root_project() -> None:
    repo_root = Path("/tmp/joinly")

    commands = build_install_commands(
        repo_root=repo_root,
        python_executable="/tmp/joinly/.venv/bin/python",
        extras=["openclaw-pi"],
    )

    assert commands == [
        [
            "/tmp/joinly/.venv/bin/python",
            "-m",
            "pip",
            "install",
            "-e",
            "/tmp/joinly/common",
        ],
        [
            "/tmp/joinly/.venv/bin/python",
            "-m",
            "pip",
            "install",
            "-e",
            "/tmp/joinly[openclaw-pi]",
        ],
    ]


def test_build_install_commands_prefers_local_client_when_requested() -> None:
    repo_root = Path("/tmp/joinly")

    commands = build_install_commands(
        repo_root=repo_root,
        python_executable="/tmp/joinly/.venv/bin/python",
        extras=["client", "openclaw-pi"],
    )

    assert commands == [
        [
            "/tmp/joinly/.venv/bin/python",
            "-m",
            "pip",
            "install",
            "-e",
            "/tmp/joinly/common",
        ],
        [
            "/tmp/joinly/.venv/bin/python",
            "-m",
            "pip",
            "install",
            "-e",
            "/tmp/joinly/client",
        ],
        [
            "/tmp/joinly/.venv/bin/python",
            "-m",
            "pip",
            "install",
            "-e",
            "/tmp/joinly[client,openclaw-pi]",
        ],
    ]
