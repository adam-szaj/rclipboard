"""
Profiling / metrics helpers for soak tests.

Collects per-call latencies, throughput, and memory (RSS of both the test
process and a watched subprocess), then emits a human-readable table to stdout
and optionally writes JSON + CSV files.

Memory model
------------
OperationStats uses two O(1)-space algorithms:
  - Welford online algorithm  — exact mean + variance, no list needed
  - Vitter reservoir sampling — fixed-size array (RESERVOIR_SIZE floats) for
    accurate percentile estimation regardless of sample count

This keeps the collector's footprint constant at ~32 KB per operation bucket
no matter how many millions of calls are recorded.

_per_test stores per-test *summaries* (count + mean only), not raw samples.

Usage
-----
    from tests.metrics import MetricsCollector, print_report, dump_json, dump_csv

    mc = MetricsCollector("MyClass", server_pid=proc.pid)

    with mc.measure("clip.put"):
        _put(port, "c", "hello")

    mc.snapshot_rss()
    mc.finalize()
    print_report([mc])
    dump_json([mc], "out.json")
    dump_csv([mc],  "out.csv")
"""

from __future__ import annotations

import contextlib
import csv
import json
import math
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Number of samples kept in the reservoir for percentile estimation.
# 4096 floats = 32 KB — accurate to ~1% error for p99 at any sample count.
RESERVOIR_SIZE = int(os.environ.get("SOAK_RESERVOIR_SIZE", "4096"))


# ── RSS helpers ───────────────────────────────────────────────────────────────

def rss_kb_self() -> int:
    """RSS of the current (test-runner) process in KiB."""
    try:
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            kb //= 1024
        return int(kb)
    except Exception:
        return _rss_kb_proc(os.getpid())


def rss_kb_proc(pid: int) -> int:
    """RSS of an arbitrary process by PID, in KiB (Linux /proc or ps fallback)."""
    return _rss_kb_proc(pid)


def _rss_kb_proc(pid: int) -> int:
    try:
        data = Path(f"/proc/{pid}/status").read_text()
        for line in data.splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
        )
        return int(out.strip())
    except Exception:
        return 0


# ── online stats: Welford + reservoir sampling ────────────────────────────────

class OperationStats:
    """
    O(1)-space streaming statistics for a single operation type.

    - exact n, min, max, mean, variance (Welford)
    - approximate percentiles via Vitter Algorithm R reservoir sampling
    """

    __slots__ = (
        "name", "_n", "_mean", "_M2", "_min", "_max",
        "_reservoir", "_reservoir_full",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self._n: int = 0
        self._mean: float = 0.0
        self._M2: float = 0.0          # sum of squared deviations (Welford)
        self._min: float = float("inf")
        self._max: float = float("-inf")
        # Fixed-size reservoir for percentile estimation (ms values)
        self._reservoir: list[float] = []
        self._reservoir_full: bool = False

    def record(self, elapsed_s: float) -> None:
        ms = elapsed_s * 1000.0
        self._n += 1
        n = self._n

        # Welford online mean + variance
        delta = ms - self._mean
        self._mean += delta / n
        self._M2  += delta * (ms - self._mean)

        if ms < self._min:
            self._min = ms
        if ms > self._max:
            self._max = ms

        # Vitter Algorithm R reservoir sampling
        if not self._reservoir_full:
            self._reservoir.append(ms)
            if len(self._reservoir) >= RESERVOIR_SIZE:
                self._reservoir_full = True
        else:
            # Replace a random element with probability RESERVOIR_SIZE / n
            j = random.randrange(n)
            if j < RESERVOIR_SIZE:
                self._reservoir[j] = ms

    def count(self) -> int:
        return self._n

    def mean_ms(self) -> float:
        return self._mean

    def min_ms(self) -> float:
        return self._min if self._n else 0.0

    def max_ms(self) -> float:
        return self._max if self._n else 0.0

    def stddev_ms(self) -> float:
        if self._n < 2:
            return 0.0
        return math.sqrt(self._M2 / (self._n - 1))

    def percentile_ms(self, p: float) -> float:
        if not self._reservoir:
            return 0.0
        s = sorted(self._reservoir)
        idx = (len(s) - 1) * p / 100.0
        lo = int(idx)
        hi = lo + 1
        frac = idx - lo
        if hi >= len(s):
            return s[-1]
        return s[lo] * (1 - frac) + s[hi] * frac

    def throughput(self, duration_s: float) -> float:
        if duration_s <= 0:
            return 0.0
        return self._n / duration_s

    def to_dict(self) -> dict:
        return {
            "name":      self.name,
            "count":     self._n,
            "mean_ms":   round(self.mean_ms(),          3),
            "min_ms":    round(self.min_ms(),           3),
            "max_ms":    round(self.max_ms(),           3),
            "stddev_ms": round(self.stddev_ms(),        3),
            "p50_ms":    round(self.percentile_ms(50),  3),
            "p90_ms":    round(self.percentile_ms(90),  3),
            "p95_ms":    round(self.percentile_ms(95),  3),
            "p99_ms":    round(self.percentile_ms(99),  3),
            "reservoir_size": len(self._reservoir),
        }


# ── per-test summary (no raw samples) ────────────────────────────────────────

@dataclass
class PerTestSummary:
    test: str
    op: str
    count: int
    mean_ms: float
    total_ms: float


@dataclass
class RssSnapshot:
    ts: float         # seconds since collector start (monotonic)
    self_kb: int
    server_kb: int    # 0 when PID unknown


# ── collector ─────────────────────────────────────────────────────────────────

class MetricsCollector:
    """Accumulates latency and RSS data for one test class."""

    def __init__(self, label: str, server_pid: int | None = None):
        self.label = label
        self.server_pid = server_pid
        self._ops: dict[str, OperationStats] = {}
        self._rss_series: list[RssSnapshot] = []
        self._started_at: float = time.monotonic()
        self._finished_at: float | None = None
        # Per-test: only aggregated summaries, not raw samples
        self._per_test: list[PerTestSummary] = []
        self._current_test: str = ""
        # Temporary accumulator for the *current* test + op
        self._test_op_buf: dict[tuple[str, str], list[float]] = {}

    def set_test(self, name: str) -> None:
        self._flush_test_buf()
        self._current_test = name

    def _flush_test_buf(self) -> None:
        """Convert raw per-test-op samples into PerTestSummary and discard."""
        for (test, op), samples in self._test_op_buf.items():
            if samples:
                n = len(samples)
                total = sum(samples)
                self._per_test.append(PerTestSummary(
                    test=test, op=op,
                    count=n,
                    mean_ms=round(total / n * 1000, 3),
                    total_ms=round(total * 1000, 3),
                ))
        self._test_op_buf.clear()

    @contextlib.contextmanager
    def measure(self, op: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - t0
            if op not in self._ops:
                self._ops[op] = OperationStats(op)
            self._ops[op].record(elapsed)
            # Accumulate raw elapsed_s only for current-test summary (small buffer)
            if self._current_test:
                key = (self._current_test, op)
                if key not in self._test_op_buf:
                    self._test_op_buf[key] = []
                self._test_op_buf[key].append(elapsed)

    def snapshot_rss(self) -> RssSnapshot:
        server_kb = rss_kb_proc(self.server_pid) if self.server_pid else 0
        snap = RssSnapshot(
            ts=time.monotonic() - self._started_at,
            self_kb=rss_kb_self(),
            server_kb=server_kb,
        )
        self._rss_series.append(snap)
        return snap

    def finalize(self) -> None:
        self._flush_test_buf()
        self._finished_at = time.monotonic()

    def duration_s(self) -> float:
        end = self._finished_at or time.monotonic()
        return end - self._started_at

    def rss_stats(self) -> dict:
        if not self._rss_series:
            return {}
        self_s   = [s.self_kb   for s in self._rss_series]
        server_s = [s.server_kb for s in self._rss_series]
        return {
            "snapshots": len(self._rss_series),
            "self_rss_kb": {
                "start": self_s[0],
                "end":   self_s[-1],
                "min":   min(self_s),
                "max":   max(self_s),
                "delta": self_s[-1] - self_s[0],
            },
            "server_rss_kb": {
                "start": server_s[0],
                "end":   server_s[-1],
                "min":   min(server_s),
                "max":   max(server_s),
                "delta": server_s[-1] - server_s[0],
            } if any(server_s) else None,
        }

    def to_dict(self) -> dict:
        return {
            "label":      self.label,
            "duration_s": round(self.duration_s(), 3),
            "ops":        [s.to_dict() for s in self._ops.values()],
            "rss":        self.rss_stats(),
            "per_test": [
                {
                    "test":     pt.test,
                    "op":       pt.op,
                    "count":    pt.count,
                    "mean_ms":  pt.mean_ms,
                    "total_ms": pt.total_ms,
                }
                for pt in self._per_test
            ],
        }


# ── reporting ─────────────────────────────────────────────────────────────────

_COL = {
    "op":   22, "n":  7, "mean": 9, "min": 9,
    "p50":   9, "p90": 9, "p95":  9, "p99": 9,
    "max":   9, "std": 9, "tps":  8,
}

def _hdr() -> str:
    return (
        f"{'operation':<{_COL['op']}} "
        f"{'n':>{_COL['n']}} "
        f"{'mean':>{_COL['mean']}} "
        f"{'min':>{_COL['min']}} "
        f"{'p50':>{_COL['p50']}} "
        f"{'p90':>{_COL['p90']}} "
        f"{'p95':>{_COL['p95']}} "
        f"{'p99':>{_COL['p99']}} "
        f"{'max':>{_COL['max']}} "
        f"{'std':>{_COL['std']}} "
        f"{'ops/s':>{_COL['tps']}}"
        f"  (all ms)"
    )

def _row(stats: OperationStats, duration_s: float) -> str:
    return (
        f"{stats.name:<{_COL['op']}} "
        f"{stats.count():>{_COL['n']}} "
        f"{stats.mean_ms():>{_COL['mean']}.2f} "
        f"{stats.min_ms():>{_COL['min']}.2f} "
        f"{stats.percentile_ms(50):>{_COL['p50']}.2f} "
        f"{stats.percentile_ms(90):>{_COL['p90']}.2f} "
        f"{stats.percentile_ms(95):>{_COL['p95']}.2f} "
        f"{stats.percentile_ms(99):>{_COL['p99']}.2f} "
        f"{stats.max_ms():>{_COL['max']}.2f} "
        f"{stats.stddev_ms():>{_COL['std']}.2f} "
        f"{stats.throughput(duration_s):>{_COL['tps']}.1f}"
    )

def _rss_row(stats: dict) -> str:
    if not stats:
        return ""
    s   = stats["self_rss_kb"]
    srv = stats.get("server_rss_kb") or {}
    parts = [
        f"  self RSS    start={s['start']:>7} KB  end={s['end']:>7} KB  "
        f"delta={s['delta']:>+7} KB  max={s['max']:>7} KB",
    ]
    if srv:
        parts.append(
            f"  server RSS  start={srv['start']:>7} KB  end={srv['end']:>7} KB  "
            f"delta={srv['delta']:>+7} KB  max={srv['max']:>7} KB"
        )
    return "\n".join(parts)


def print_report(collectors: list[MetricsCollector], file=None) -> None:
    if file is None:
        file = sys.stdout
    sep = "─" * (sum(_COL.values()) + len(_COL) + 10)
    for c in collectors:
        print(f"\n{'═' * len(sep)}", file=file)
        print(f"  METRICS: {c.label}  (wall={c.duration_s():.1f}s  "
              f"reservoir={RESERVOIR_SIZE})", file=file)
        print(sep, file=file)
        if c._ops:
            print(_hdr(), file=file)
            print(sep, file=file)
            for stats in c._ops.values():
                print(_row(stats, c.duration_s()), file=file)
        else:
            print("  (no operation timings recorded)", file=file)
        rss_d = c.rss_stats()
        if rss_d:
            print(sep, file=file)
            print(_rss_row(rss_d), file=file)
        # per-test breakdown (summaries only)
        if c._per_test:
            print(sep, file=file)
            print("  per-test breakdown:", file=file)
            prev_test = ""
            for pt in c._per_test:
                if pt.test != prev_test:
                    print(f"    [{pt.test}]", file=file)
                    prev_test = pt.test
                print(f"      {pt.op:<24}  n={pt.count:<6}  "
                      f"mean={pt.mean_ms:.2f} ms  total={pt.total_ms:.1f} ms",
                      file=file)
        print(sep, file=file)


def dump_json(collectors: list[MetricsCollector], path: str | Path) -> None:
    Path(path).write_text(json.dumps([c.to_dict() for c in collectors], indent=2))


def dump_csv(collectors: list[MetricsCollector], path: str | Path) -> None:
    fieldnames = [
        "label", "duration_s", "op", "count",
        "mean_ms", "min_ms", "p50_ms", "p90_ms", "p95_ms", "p99_ms",
        "max_ms", "stddev_ms", "ops_per_s",
        "self_rss_start_kb", "self_rss_end_kb", "self_rss_delta_kb",
        "server_rss_start_kb", "server_rss_end_kb", "server_rss_delta_kb",
    ]
    rows = []
    for c in collectors:
        rss      = c.rss_stats()
        self_rss = rss.get("self_rss_kb",   {}) if rss else {}
        srv_rss  = rss.get("server_rss_kb", {}) if rss else {}
        for stats in c._ops.values():
            rows.append({
                "label":               c.label,
                "duration_s":          round(c.duration_s(), 3),
                "op":                  stats.name,
                "count":               stats.count(),
                "mean_ms":             round(stats.mean_ms(),          3),
                "min_ms":              round(stats.min_ms(),           3),
                "p50_ms":              round(stats.percentile_ms(50),  3),
                "p90_ms":              round(stats.percentile_ms(90),  3),
                "p95_ms":              round(stats.percentile_ms(95),  3),
                "p99_ms":              round(stats.percentile_ms(99),  3),
                "max_ms":              round(stats.max_ms(),           3),
                "stddev_ms":           round(stats.stddev_ms(),        3),
                "ops_per_s":           round(stats.throughput(c.duration_s()), 2),
                "self_rss_start_kb":   self_rss.get("start", ""),
                "self_rss_end_kb":     self_rss.get("end",   ""),
                "self_rss_delta_kb":   self_rss.get("delta", ""),
                "server_rss_start_kb": srv_rss.get("start", ""),
                "server_rss_end_kb":   srv_rss.get("end",   ""),
                "server_rss_delta_kb": srv_rss.get("delta", ""),
            })
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
