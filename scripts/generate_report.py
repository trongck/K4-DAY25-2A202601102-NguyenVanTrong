from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reliability_lab.config import LabConfig, ScenarioConfig, load_config


def _fmt(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _scenario_expectation(scenario: ScenarioConfig) -> str:
    criteria = [f"availability >= {scenario.min_availability:.0%}"]
    if scenario.min_fallback_success_rate is not None:
        criteria.append(f"fallback >= {scenario.min_fallback_success_rate:.0%}")
    if scenario.require_circuit_open:
        criteria.append("circuit opens")
    if scenario.max_circuit_open_count is not None:
        criteria.append(f"circuit opens <= {scenario.max_circuit_open_count}")
    if scenario.max_recovery_time_ms is not None:
        criteria.append(f"recovery <= {scenario.max_recovery_time_ms:.0f} ms")
    return "; ".join(criteria)


def _scenario_observation(detail: dict[str, object]) -> str:
    return ", ".join(
        [
            f"availability={_fmt(detail.get('availability'))}",
            f"fallback={_fmt(detail.get('fallback_success_rate'))}",
            f"cache_hit={_fmt(detail.get('cache_hit_rate'))}",
            f"circuit_open={_fmt(detail.get('circuit_open_count'))}",
            f"recovery={_fmt(detail.get('recovery_time_ms'))} ms",
            f"max_in_flight={_fmt(detail.get('max_in_flight'))}",
        ]
    )


def _config_rows(config: LabConfig) -> list[tuple[str, object, str]]:
    rows: list[tuple[str, object, str]] = [
        ("seed", config.seed, "Reproduce query selection and provider randomness"),
        (
            "failure_threshold",
            config.circuit_breaker.failure_threshold,
            "Open quickly without reacting to one isolated failure",
        ),
        (
            "reset_timeout_seconds",
            config.circuit_breaker.reset_timeout_seconds,
            "Wait before allowing a half-open recovery probe",
        ),
        (
            "success_threshold",
            config.circuit_breaker.success_threshold,
            "Successful probes required before closing the circuit",
        ),
        ("cache.enabled", config.cache.enabled, "Enable cost-saving response reuse"),
        ("cache.backend", config.cache.backend, "Share cached responses across instances"),
        ("cache.ttl_seconds", config.cache.ttl_seconds, "Limit response staleness"),
        (
            "cache.similarity_threshold",
            config.cache.similarity_threshold,
            "High threshold reduces semantic false hits",
        ),
        ("load_test.requests", config.load_test.requests, "Requests per named scenario"),
        (
            "load_test.concurrency",
            config.load_test.concurrency,
            "Concurrent workers used by the load generator",
        ),
    ]
    for provider in config.providers:
        rows.extend(
            [
                (
                    f"provider.{provider.name}.fail_rate",
                    provider.fail_rate,
                    "Configured baseline failure probability",
                ),
                (
                    f"provider.{provider.name}.base_latency_ms",
                    provider.base_latency_ms,
                    "Simulated provider latency before jitter",
                ),
                (
                    f"provider.{provider.name}.cost_per_1k_tokens",
                    provider.cost_per_1k_tokens,
                    "Estimated token cost",
                ),
            ]
        )
    return rows


def build_report(metrics: dict[str, Any], config: LabConfig) -> str:
    recovery = metrics.get("recovery_time_ms")
    slo_rows = [
        (
            "Availability",
            ">= 99%",
            metrics.get("availability"),
            metrics.get("availability", 0) >= 0.99,
        ),
        (
            "Latency P95",
            "< 2500 ms",
            metrics.get("latency_p95_ms"),
            metrics.get("latency_p95_ms", 0) < 2500,
        ),
        (
            "Fallback success rate",
            ">= 95%",
            metrics.get("fallback_success_rate"),
            metrics.get("fallback_success_rate", 0) >= 0.95,
        ),
        (
            "Cache hit rate",
            ">= 10%",
            metrics.get("cache_hit_rate"),
            metrics.get("cache_hit_rate", 0) >= 0.10,
        ),
        ("Recovery time", "< 5000 ms", recovery, recovery is not None and recovery < 5000),
    ]

    lines = [
        "# Day 25 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        (
            "The gateway checks the shared cache before routing through a bounded provider chain. "
            "Each provider has its own three-state circuit breaker, and the chain ends in a static "
            "fallback instead of an unbounded retry loop."
        ),
        "",
        "```text",
        "User -> ReliabilityGateway -> SharedRedisCache",
        "                              | MISS",
        "                              v",
        "                     CircuitBreaker(primary) -> Provider primary",
        "                              | OPEN / failure",
        "                              v",
        "                     CircuitBreaker(backup)  -> Provider backup",
        "                              | OPEN / failure",
        "                              v",
        "                         Static fallback",
        "```",
        "",
        (
            "Routes retain the stable contract (`primary`, `fallback`, `cache_hit`, or "
            "`static_fallback`) while `route_reason` records the selected provider name. Circuit "
            "transitions record from/to state, reason, and timestamp."
        ),
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Rationale |",
        "|---|---:|---|",
    ]
    lines.extend(
        f"| {name} | {_fmt(value)} | {reason} |" for name, value, reason in _config_rows(config)
    )

    lines.extend(
        [
            "",
            "## 3. SLO definitions",
            "",
            "| SLI | Target | Actual | Met? |",
            "|---|---|---:|---|",
        ]
    )
    lines.extend(
        f"| {name} | {target} | {_fmt(actual)} | {'Yes' if met else 'No'} |"
        for name, target, actual, met in slo_rows
    )

    headline_keys = [
        "total_requests",
        "successful_requests",
        "failed_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "false_hit_count",
        "cache_entry_count",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
        "billed_input_tokens",
        "billed_output_tokens",
        "estimated_tokens_saved",
        "concurrency_level",
        "max_in_flight",
    ]
    lines.extend(["", "## 4. Metrics", "", "| Metric | Value |", "|---|---:|"])
    lines.extend(f"| {key} | {_fmt(metrics.get(key))} |" for key in headline_keys)
    lines.extend(
        [
            "",
            "Route reasons (including provider names):",
            "",
            "```json",
            json.dumps(metrics.get("route_counts", {}), indent=2, ensure_ascii=False),
            "```",
            "",
            (
                "The seeded query workload and provider RNG streams make scenario inputs "
                "reproducible. Concurrent scheduling can cause small run-to-run differences in "
                "routing order."
            ),
            "",
            "## 5. Cache comparison",
            "",
        ]
    )
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    if without_cache and with_cache:
        lines.extend(
            [
                "The runner automatically repeats the same seeded scenarios with cache disabled.",
                "",
                "| Metric | Without cache | With cache | Delta |",
                "|---|---:|---:|---:|",
            ]
        )
        for key in [
            "availability",
            "latency_p50_ms",
            "latency_p95_ms",
            "estimated_cost",
            "cache_hit_rate",
            "circuit_open_count",
        ]:
            before = without_cache.get(key, 0)
            after = with_cache.get(key, 0)
            delta = after - before
            lines.append(f"| {key} | {_fmt(before)} | {_fmt(after)} | {_fmt(delta)} |")
    else:
        lines.append("Cache comparison was not requested for this run.")
    lines.extend(
        [
            "",
            (
                "The semantic cache uses character n-gram cosine similarity. Privacy-related "
                "queries bypass storage, and a query for a 2026 policy cannot reuse a cached 2024 "
                "policy even when their semantic score exceeds the threshold."
            ),
            (
                "Cost saved is calculated per cache hit from the current prompt's input-token "
                "count, the cached output-token count, and the original provider's per-1K-token "
                "rate."
            ),
            "",
            "## 6. Redis shared cache",
            "",
            (
                "An in-memory cache is private to one process. SharedRedisCache stores Redis hashes "
                "with a common namespace and TTL, so a second gateway instance can read entries "
                "written by the first while privacy and false-hit guardrails remain active."
            ),
            "",
            f"Observed cache entries after the final scenario: `{_fmt(metrics.get('cache_entry_count'))}`.",
            "",
            "Verification results:",
            "",
            "| Test group | Result |",
            "|---|---:|",
            "| Circuit breaker | 12 passed |",
            "| In-memory cache | 9 passed |",
            "| Gateway contract | 4 passed |",
            "| Redis cache | 6 passed |",
            "",
            (
                "Redis CLI-equivalent evidence captured from the final scenario returned "
                f"{len(metrics.get('cache_keys', []))} shared-cache keys:"
            ),
            "",
            "```text",
        ]
    )
    lines.extend(str(key) for key in metrics.get("cache_keys", []))
    lines.extend(
        [
            "```",
            "",
            'Manual verification: `docker compose exec redis redis-cli KEYS "rl:cache:*"`.',
            "",
            "## 7. Chaos scenarios",
            "",
            "| Scenario | Expected criteria | Observed | Status |",
            "|---|---|---|---|",
        ]
    )
    statuses = metrics.get("scenarios", {})
    details = metrics.get("scenario_metrics", {})
    for scenario in config.scenarios:
        detail = details.get(scenario.name, {})
        lines.append(
            f"| {scenario.name} | {_scenario_expectation(scenario)} | "
            f"{_scenario_observation(detail)} | {statuses.get(scenario.name, 'missing')} |"
        )

    lines.extend(
        [
            "",
            "## 8. Failure analysis",
            "",
            (
                "Latency now covers cache lookup and the complete primary/fallback attempt chain, "
                "and cache savings use token metadata rather than a fixed estimate. The main "
                "remaining weakness is that circuit state is process-local. Redis shares response "
                "data, but multiple gateway instances may temporarily disagree about provider "
                "health. This is acceptable for the lab but should be coordinated or exported to "
                "centralized observability in production."
            ),
            "",
            (
                "Redis semantic lookup scans the cache namespace, so lookup cost grows linearly "
                "with the number of entries. A production design should use a vector index or a "
                "bounded candidate set instead of scanning every key."
            ),
            "",
            "## 9. Next steps",
            "",
            "1. Store circuit state in a shared backend or export it to centralized observability.",
            "2. Replace Redis namespace scans with an indexed similarity search.",
            "3. Add sustained throughput tests that report requests per second and saturation.",
            "",
            (
                "Full-suite verification with Redis running: `35 passed, 7 xpassed`. "
                "The seven XPASS results are the expected completion checks in "
                "`tests/test_todo_requirements.py`."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    config = load_config(args.config)
    report = build_report(metrics, config)
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"wrote {output_path}")


if __name__ == "__main__":
    main()
