"""Aggregate the existing trace fields and apply the supplied monitoring rules."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def summarize_traces(traces: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(traces)
    errors = 0
    tool_calls = 0
    latencies = []
    for trace in traces:
        if trace["error"] is not None:
            errors += 1
        tool_calls += len(trace["tool_calls"])
        latencies.append(trace["latency_ms"])
    latencies.sort()
    # Nearest-rank p95 matches PostgreSQL percentile_disc, including small samples.
    p95 = latencies[math.ceil(0.95 * count) - 1] if count else 0
    return {"run_count": count, "error_rate": errors / count if count else 0.0,
            "p95_latency_ms": p95, "tool_calls": tool_calls}


def active_alerts(summary: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parents[1] / "data" / "monitoring_rules.json"
    rules = json.loads(path.read_text(encoding="utf-8"))
    alerts = []
    for rule in rules:
        if summary["run_count"] < rule["minimum_samples"]:
            continue
        if rule["operator"] != "gt":
            raise ValueError("Unsupported monitoring rule operator")
        value = summary[rule["metric"]]
        if value > rule["threshold"]:
            alerts.append({**rule, "value": value})
    return alerts
