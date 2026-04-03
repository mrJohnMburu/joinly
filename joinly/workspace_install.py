from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path


def normalize_extras(extras: Iterable[str]) -> list[str]:
    """Deduplicate extras while preserving the requested order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for extra in extras:
        if extra in seen:
            continue
        normalized.append(extra)
        seen.add(extra)
    return normalized


def project_install_spec(repo_root: Path, extras: Sequence[str]) -> str:
    """Build the editable pip spec for the root project."""
    normalized = normalize_extras(extras)
    extras_suffix = f"[{','.join(normalized)}]" if normalized else ""
    return f"{repo_root}{extras_suffix}"


def build_install_commands(
    *,
    repo_root: Path,
    python_executable: str,
    extras: Sequence[str],
) -> list[list[str]]:
    """Install local workspace packages before the root project."""
    normalized = normalize_extras(extras)
    commands = [
        [
            python_executable,
            "-m",
            "pip",
            "install",
            "-e",
            str(repo_root / "common"),
        ],
    ]

    if "client" in normalized:
        commands.append(
            [
                python_executable,
                "-m",
                "pip",
                "install",
                "-e",
                str(repo_root / "client"),
            ]
        )

    commands.append(
        [
            python_executable,
            "-m",
            "pip",
            "install",
            "-e",
            project_install_spec(repo_root, normalized),
        ]
    )
    return commands


def run_install_commands(commands: Sequence[Sequence[str]]) -> None:
    """Run the generated pip commands in sequence."""
    for command in commands:
        print("$", shlex.join(command))
        subprocess.run(command, check=True)  # noqa: S603


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install local workspace packages from a Joinly checkout before "
            "installing the root project. This keeps pip-based native installs "
            "from accidentally resolving published workspace packages."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing the common/, client/, and joinly/ packages.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable whose pip should perform the install.",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="Optional project extra to install. Repeat for multiple extras.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated pip commands without executing them.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    commands = build_install_commands(
        repo_root=repo_root,
        python_executable=args.python,
        extras=args.extra,
    )

    if args.dry_run:
        for command in commands:
            print(shlex.join(command))
        return 0

    run_install_commands(commands)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
