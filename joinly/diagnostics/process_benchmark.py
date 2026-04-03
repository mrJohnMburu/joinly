from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ProcessEntry:
    """A single process entry discovered from `/proc/<pid>/status`."""

    pid: int
    ppid: int
    rss_bytes: int


@dataclass(frozen=True, slots=True)
class MemorySample:
    """One point-in-time aggregate memory sample for a process tree."""

    timestamp_s: float
    rss_bytes: int
    process_count: int


def parse_status_entry(status_text: str) -> ProcessEntry:
    """Parse a Linux `/proc/<pid>/status` file into a process entry."""
    fields: dict[str, str] = {}
    for line in status_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key] = value.strip()

    return ProcessEntry(
        pid=int(fields["Pid"]),
        ppid=int(fields["PPid"]),
        rss_bytes=_parse_vm_rss(fields.get("VmRSS")),
    )


def _parse_vm_rss(value: str | None) -> int:
    if not value:
        return 0
    amount, *_unit = value.split()
    return int(amount) * 1024


def tree_pids(entries: list[ProcessEntry], root_pid: int) -> set[int]:
    """Return the root process plus all discovered descendants."""
    children: dict[int, list[int]] = {}
    for entry in entries:
        children.setdefault(entry.ppid, []).append(entry.pid)

    discovered = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child in discovered:
                continue
            discovered.add(child)
            queue.append(child)
    return discovered


def summarize_samples(samples: list[MemorySample]) -> dict[str, Any]:
    """Compute a compact summary for a list of memory samples."""
    if not samples:
        return {
            "sample_count": 0,
            "peak_rss_bytes": 0,
            "peak_rss_mb": 0.0,
            "average_rss_mb": 0.0,
            "max_process_count": 0,
        }

    peak_rss_bytes = max(sample.rss_bytes for sample in samples)
    average_rss_mb = round(
        sum(sample.rss_bytes for sample in samples) / len(samples) / 1024 / 1024,
        2,
    )
    return {
        "sample_count": len(samples),
        "peak_rss_bytes": peak_rss_bytes,
        "peak_rss_mb": round(peak_rss_bytes / 1024 / 1024, 2),
        "average_rss_mb": average_rss_mb,
        "max_process_count": max(sample.process_count for sample in samples),
    }


def discover_process_entries(proc_root: Path = Path("/proc")) -> list[ProcessEntry]:
    """Read process entries from a Linux procfs tree."""
    entries: list[ProcessEntry] = []
    for child in proc_root.iterdir():
        if not child.name.isdigit():
            continue
        status_path = child / "status"
        try:
            entries.append(parse_status_entry(status_path.read_text()))
        except (FileNotFoundError, KeyError, PermissionError, ProcessLookupError, ValueError):
            continue
    return entries


def sample_process_tree(
    root_pid: int,
    *,
    proc_root: Path = Path("/proc"),
    timestamp_s: float | None = None,
) -> MemorySample | None:
    """Sample RSS across the root process and all descendants."""
    entries = discover_process_entries(proc_root)
    entry_by_pid = {entry.pid: entry for entry in entries}
    if root_pid not in entry_by_pid:
        return None

    pids = tree_pids(entries, root_pid=root_pid)
    rss_bytes = sum(entry_by_pid[pid].rss_bytes for pid in pids if pid in entry_by_pid)
    return MemorySample(
        timestamp_s=time.monotonic() if timestamp_s is None else timestamp_s,
        rss_bytes=rss_bytes,
        process_count=len(pids),
    )


def probe_http(url: str, *, timeout: float = 0.5) -> bool:
    """Return whether an HTTP endpoint is currently reachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return 200 <= response.status < 500  # type: ignore[attr-defined]
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def benchmark_process(
    *,
    pid: int,
    duration_seconds: float,
    sample_interval_seconds: float = 0.5,
    probe_url: str | None = None,
    ready_timeout_seconds: float = 60.0,
    post_ready_seconds: float = 10.0,
) -> dict[str, Any]:
    """Collect memory samples for a running process tree."""
    started_at = time.monotonic()
    ready_at: float | None = None
    samples: list[MemorySample] = []

    while True:
        now = time.monotonic()
        sample = sample_process_tree(pid, timestamp_s=now)
        if sample is None:
            break
        samples.append(sample)

        if probe_url and ready_at is None and probe_http(probe_url):
            ready_at = now

        if ready_at is not None and now - ready_at >= post_ready_seconds:
            break

        if ready_at is None and probe_url and now - started_at >= ready_timeout_seconds:
            break

        if probe_url is None and now - started_at >= duration_seconds:
            break

        time.sleep(sample_interval_seconds)

    summary = summarize_samples(samples)
    summary["root_pid"] = pid
    summary["startup_seconds"] = (
        None if ready_at is None else round(ready_at - started_at, 2)
    )
    summary["duration_seconds"] = round(
        0.0 if not samples else samples[-1].timestamp_s - samples[0].timestamp_s, 2
    )
    summary["samples"] = [asdict(sample) for sample in samples]
    return summary


def benchmark_command(
    command: list[str],
    *,
    duration_seconds: float,
    sample_interval_seconds: float = 0.5,
    probe_url: str | None = None,
    ready_timeout_seconds: float = 60.0,
    post_ready_seconds: float = 10.0,
    inherit_io: bool = False,
) -> dict[str, Any]:
    """Launch a command, sample its process tree, and terminate it when done."""
    proc = subprocess.Popen(  # noqa: S603
        command,
        stdout=None if inherit_io else subprocess.DEVNULL,
        stderr=None if inherit_io else subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        summary = benchmark_process(
            pid=proc.pid,
            duration_seconds=duration_seconds,
            sample_interval_seconds=sample_interval_seconds,
            probe_url=probe_url,
            ready_timeout_seconds=ready_timeout_seconds,
            post_ready_seconds=post_ready_seconds,
        )
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        summary["exit_code"] = proc.returncode
        return summary
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark RSS for a Linux process tree or a launched command. "
            "Useful for measuring Joinly startup and idle memory on a Pi."
        )
    )
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--probe-url", type=str, default=None)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.5)
    parser.add_argument("--ready-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--post-ready-seconds", type=float, default=10.0)
    parser.add_argument("--inherit-io", action="store_true")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to launch, introduced by `--`, e.g. -- joinly ...",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]

    if args.pid is None and not command:
        parser.error("Provide either --pid or a command after --.")
    if args.pid is not None and command:
        parser.error("Use either --pid or a command after --, not both.")

    if args.pid is not None:
        result = benchmark_process(
            pid=args.pid,
            duration_seconds=args.duration_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            probe_url=args.probe_url,
            ready_timeout_seconds=args.ready_timeout_seconds,
            post_ready_seconds=args.post_ready_seconds,
        )
    else:
        result = benchmark_command(
            command,
            duration_seconds=args.duration_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            probe_url=args.probe_url,
            ready_timeout_seconds=args.ready_timeout_seconds,
            post_ready_seconds=args.post_ready_seconds,
            inherit_io=args.inherit_io,
        )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
