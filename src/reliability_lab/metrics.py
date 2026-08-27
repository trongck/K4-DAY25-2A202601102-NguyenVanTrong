from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from statistics import median
from typing import cast

from pydantic import BaseModel, Field


class RunMetrics(BaseModel):
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    fallback_successes: int = 0
    static_fallbacks: int = 0
    cache_hits: int = 0
    false_hit_count: int = 0
    cache_entry_count: int = 0
    circuit_open_count: int = 0
    recovery_time_ms: float | None = None
    estimated_cost: float = 0.0
    estimated_cost_saved: float = 0.0
    billed_input_tokens: int = 0
    billed_output_tokens: int = 0
    estimated_tokens_saved: int = 0
    concurrency_level: int = 1
    max_in_flight: int = 1
    cache_keys: list[str] = Field(default_factory=list)
    latencies_ms: list[float] = Field(default_factory=list)
    route_counts: dict[str, int] = Field(default_factory=dict)
    scenarios: dict[str, str] = Field(default_factory=dict)
    scenario_metrics: dict[str, dict[str, object]] = Field(default_factory=dict)
    cache_comparison: dict[str, dict[str, float | int]] = Field(default_factory=dict)

    @property
    def availability(self) -> float:
        return self.successful_requests / self.total_requests if self.total_requests else 0.0

    @property
    def error_rate(self) -> float:
        return self.failed_requests / self.total_requests if self.total_requests else 0.0

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.total_requests if self.total_requests else 0.0

    @property
    def fallback_success_rate(self) -> float:
        denom = self.fallback_successes + self.static_fallbacks
        return self.fallback_successes / denom if denom else 0.0

    def percentile(self, q: float) -> float:
        return percentile(self.latencies_ms, q)

    def to_report_dict(self) -> dict[str, object]:
        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "availability": round(self.availability, 4),
            "error_rate": round(self.error_rate, 4),
            "latency_p50_ms": round(self.percentile(50), 2),
            "latency_p95_ms": round(self.percentile(95), 2),
            "latency_p99_ms": round(self.percentile(99), 2),
            "fallback_success_rate": round(self.fallback_success_rate, 4),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "false_hit_count": self.false_hit_count,
            "cache_entry_count": self.cache_entry_count,
            "circuit_open_count": self.circuit_open_count,
            "recovery_time_ms": (
                round(self.recovery_time_ms, 2) if self.recovery_time_ms is not None else None
            ),
            "estimated_cost": round(self.estimated_cost, 6),
            "estimated_cost_saved": round(self.estimated_cost_saved, 6),
            "billed_input_tokens": self.billed_input_tokens,
            "billed_output_tokens": self.billed_output_tokens,
            "estimated_tokens_saved": self.estimated_tokens_saved,
            "concurrency_level": self.concurrency_level,
            "max_in_flight": self.max_in_flight,
            "cache_keys": self.cache_keys,
            "route_counts": self.route_counts,
            "scenarios": self.scenarios,
            "scenario_metrics": self.scenario_metrics,
            "cache_comparison": self.cache_comparison,
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_report_dict(), indent=2, ensure_ascii=False))

    def write_csv(self, path: str | Path) -> None:
        """Export metrics to CSV format."""
        report = self.to_report_dict()
        scenarios = cast(dict[str, str], report.pop("scenarios", {}))
        report["route_counts"] = json.dumps(report["route_counts"], ensure_ascii=False)
        report["cache_keys"] = json.dumps(report["cache_keys"], ensure_ascii=False)
        report["scenario_metrics"] = json.dumps(report["scenario_metrics"], ensure_ascii=False)
        report["cache_comparison"] = json.dumps(report["cache_comparison"], ensure_ascii=False)

        for name, result in scenarios.items():
            report[f"scenario_{name}"] = result

        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(report.keys()))
            writer.writeheader()
            writer.writerow(report)


def percentile(values: Iterable[float], q: float) -> float:
    values_sorted = sorted(values)
    if not values_sorted:
        return 0.0
    if q == 50:
        return float(median(values_sorted))
    k = (len(values_sorted) - 1) * q / 100
    lower = int(k)
    upper = min(lower + 1, len(values_sorted) - 1)
    weight = k - lower
    return values_sorted[lower] * (1 - weight) + values_sorted[upper] * weight
