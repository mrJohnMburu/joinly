from joinly.diagnostics.process_benchmark import (
    MemorySample,
    ProcessEntry,
    parse_status_entry,
    summarize_samples,
    tree_pids,
)


def test_parse_status_entry_extracts_pid_ppid_and_rss() -> None:
    entry = parse_status_entry(
        """
Name:\tchromium
Pid:\t4242
PPid:\t4000
VmRSS:\t  123456 kB
"""
    )

    assert entry == ProcessEntry(pid=4242, ppid=4000, rss_bytes=123456 * 1024)


def test_tree_pids_returns_descendants_for_root_process() -> None:
    entries = [
        ProcessEntry(pid=100, ppid=1, rss_bytes=10),
        ProcessEntry(pid=101, ppid=100, rss_bytes=10),
        ProcessEntry(pid=102, ppid=101, rss_bytes=10),
        ProcessEntry(pid=200, ppid=1, rss_bytes=10),
    ]

    assert tree_pids(entries, root_pid=100) == {100, 101, 102}


def test_summarize_samples_reports_peak_average_and_process_count() -> None:
    summary = summarize_samples(
        [
            MemorySample(timestamp_s=0.0, rss_bytes=100 * 1024 * 1024, process_count=1),
            MemorySample(timestamp_s=1.0, rss_bytes=160 * 1024 * 1024, process_count=3),
            MemorySample(timestamp_s=2.0, rss_bytes=140 * 1024 * 1024, process_count=2),
        ]
    )

    assert summary["sample_count"] == 3
    assert summary["peak_rss_bytes"] == 160 * 1024 * 1024
    assert summary["peak_rss_mb"] == 160.0
    assert summary["average_rss_mb"] == 133.33
    assert summary["max_process_count"] == 3
