# 指标记录器：线程安全收集每次请求的延迟与业务结果，输出 CSV 和汇总 JSON。
"""Thread-safe request recorder and percentile summaries."""

import csv
import json
import math
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path


def percentile(values, point):
    """Nearest-rank percentile; returns 0.0 for an empty sample."""

    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(point / 100 * len(ordered)))
    return float(ordered[rank - 1])


def _latency_stats(values):
    if not values:
        return {}
    return {
        "count": len(values),
        "min_ms": round(min(values), 2),
        "mean_ms": round(sum(values) / len(values), 2),
        "p50_ms": round(percentile(values, 50), 2),
        "p90_ms": round(percentile(values, 90), 2),
        "p95_ms": round(percentile(values, 95), 2),
        "p99_ms": round(percentile(values, 99), 2),
        "max_ms": round(max(values), 2),
    }


class Recorder:
    """Collect samples from worker threads and persist them for the report."""

    def __init__(self, scenario, users, label=None):
        self.scenario = scenario
        self.users = users
        self.label = label or scenario
        self.started_at = None
        self.finished_at = None
        self._samples = []
        self._lock = threading.Lock()

    def start(self):
        self.started_at = time.time()

    def stop(self):
        self.finished_at = time.time()

    def record(self, action, vu, status_code, latency_ms, outcome, detail="", extra=None):
        with self._lock:
            self._samples.append({
                "ts": round(time.time(), 3),
                "scenario": self.scenario,
                "label": self.label,
                "users": self.users,
                "action": action,
                "vu": vu,
                "status_code": int(status_code or 0),
                "latency_ms": round(float(latency_ms), 2),
                "outcome": outcome,
                "detail": (detail or "")[:240],
                **(extra or {}),
            })

    def snapshot(self):
        with self._lock:
            return list(self._samples)

    def summary(self):
        samples = self.snapshot()
        duration = max(0.001, (self.finished_at or time.time()) - (self.started_at or time.time()))
        outcomes = Counter(sample["outcome"] for sample in samples)
        latencies = [sample["latency_ms"] for sample in samples]
        latencies_ok = [
            sample["latency_ms"] for sample in samples
            if sample["outcome"] in ("success", "ok")
        ]
        by_action = {}
        grouped = defaultdict(list)
        for sample in samples:
            grouped[sample["action"]].append(sample)
        for action, items in sorted(grouped.items()):
            action_outcomes = Counter(item["outcome"] for item in items)
            by_action[action] = {
                "requests": len(items),
                "outcomes": dict(action_outcomes),
                **_latency_stats([item["latency_ms"] for item in items]),
            }
        return {
            "scenario": self.scenario,
            "label": self.label,
            "users": self.users,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(duration, 2),
            "requests": len(samples),
            "rps": round(len(samples) / duration, 2),
            "outcomes": dict(outcomes),
            "success_rate": round(
                sum(1 for item in samples if item["outcome"] in ("success", "ok"))
                / len(samples), 4
            ) if samples else 0.0,
            "latency": _latency_stats(latencies),
            "latency_success_only": _latency_stats(latencies_ok),
            "by_action": by_action,
        }

    def write(self, output_dir):
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        csv_path = output / f"{self.label}.csv"
        json_path = output / f"{self.label}.json"
        samples = self.snapshot()
        fieldnames = [
            "ts", "scenario", "label", "users", "action", "vu",
            "status_code", "latency_ms", "outcome", "detail",
        ]
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(samples)
        payload = {
            "summary": self.summary(),
            "samples": samples,
            "extra": getattr(self, "extra", {}),
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return csv_path, json_path
