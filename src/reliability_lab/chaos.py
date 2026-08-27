from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


@dataclass
class SimulationClock:
    """Deterministic clock advanced by fake provider latency."""

    now: float = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    clock: SimulationClock | None = None,
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = (
            provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        )
        providers.append(
            FakeLLMProvider(
                p.name,
                fail_rate,
                p.base_latency_ms,
                p.cost_per_1k_tokens,
                rng=random.Random(f"{config.seed}:{p.name}"),
                sleep=clock.sleep if clock is not None else time.sleep,
            )
        )
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
            clock=clock.time if clock is not None else time.monotonic,
            timestamp=clock.time if clock is not None else time.time,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs."""
    recovery_times = []

    for breaker in gateway.breakers.values():
        open_ts = None
        for entry in breaker.transition_log:
            if entry["to"] == "open":
                open_ts = entry["ts"]
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((entry["ts"] - open_ts) * 1000.0)
                open_ts = None

    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    if not queries:
        raise ValueError("queries must not be empty")

    query_rng = random.Random(config.seed)
    clock = SimulationClock()
    gateway = build_gateway(config, scenario.provider_overrides or None, clock=clock)
    if isinstance(gateway.cache, SharedRedisCache):
        # Isolate scenarios and repeated seeded runs while leaving the final
        # scenario's entries available for Redis shared-state evidence.
        gateway.cache.flush()
    metrics = RunMetrics()
    metrics.concurrency_level = min(config.load_test.concurrency, config.load_test.requests)
    prompts = [query_rng.choice(queries) for _ in range(config.load_test.requests)]
    active_requests = 0
    max_in_flight = 0
    active_lock = threading.Lock()
    start_barrier = (
        threading.Barrier(metrics.concurrency_level) if metrics.concurrency_level > 1 else None
    )

    def execute(index_and_prompt: tuple[int, str]) -> GatewayResponse:
        nonlocal active_requests, max_in_flight
        index, prompt = index_and_prompt
        with active_lock:
            active_requests += 1
            max_in_flight = max(max_in_flight, active_requests)
        try:
            if start_barrier is not None and index < metrics.concurrency_level:
                start_barrier.wait()
            return gateway.complete(prompt)
        finally:
            with active_lock:
                active_requests -= 1

    indexed_prompts = list(enumerate(prompts))
    if metrics.concurrency_level > 1:
        with ThreadPoolExecutor(max_workers=metrics.concurrency_level) as executor:
            results = list(executor.map(execute, indexed_prompts))
    else:
        results = [execute(item) for item in indexed_prompts]
    metrics.max_in_flight = max_in_flight

    for result in results:
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        metrics.route_counts[result.route_reason] = (
            metrics.route_counts.get(result.route_reason, 0) + 1
        )

        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += result.estimated_cost_saved
            metrics.estimated_tokens_saved += result.input_tokens + result.output_tokens
            metrics.successful_requests += 1
        elif result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1

        if not result.cache_hit and result.route != "static_fallback":
            metrics.billed_input_tokens += result.input_tokens
            metrics.billed_output_tokens += result.output_tokens
        metrics.latencies_ms.append(result.latency_ms)

    for breaker in gateway.breakers.values():
        for entry in breaker.transition_log:
            if entry["to"] == "open":
                metrics.circuit_open_count += 1

    if gateway.cache is not None:
        metrics.false_hit_count = len(gateway.cache.false_hit_log)
        metrics.cache_entry_count = gateway.cache.entry_count()
        if isinstance(gateway.cache, SharedRedisCache):
            metrics.cache_keys = gateway.cache.keys()

    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def scenario_passed(scenario: ScenarioConfig, metrics: RunMetrics) -> bool:
    """Evaluate a scenario against its explicit SLO-style acceptance criteria."""
    if metrics.availability < scenario.min_availability:
        return False
    if (
        scenario.min_fallback_success_rate is not None
        and metrics.fallback_success_rate < scenario.min_fallback_success_rate
    ):
        return False
    if scenario.require_circuit_open and metrics.circuit_open_count == 0:
        return False
    if (
        scenario.max_circuit_open_count is not None
        and metrics.circuit_open_count > scenario.max_circuit_open_count
    ):
        return False
    return not (
        scenario.max_recovery_time_ms is not None
        and (
            metrics.recovery_time_ms is None
            or metrics.recovery_time_ms > scenario.max_recovery_time_ms
        )
    )


def _comparison_summary(metrics: RunMetrics) -> dict[str, float | int]:
    """Select stable headline metrics for cache-on/cache-off evidence."""
    return {
        "availability": round(metrics.availability, 4),
        "latency_p50_ms": round(metrics.percentile(50), 2),
        "latency_p95_ms": round(metrics.percentile(95), 2),
        "estimated_cost": round(metrics.estimated_cost, 6),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "circuit_open_count": metrics.circuit_open_count,
    }


def run_simulation(
    config: LabConfig,
    queries: list[str],
    *,
    include_cache_comparison: bool = True,
) -> RunMetrics:
    """Run named scenarios and optionally record a seeded cache-off comparison.

    Scenario status is evaluated from acceptance criteria in the YAML configuration.
    """
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {
            "default": "pass" if scenario_passed(default_scenario, metrics) else "fail"
        }
        metrics.scenario_metrics = {"default": metrics.to_report_dict()}
        return metrics

    combined = RunMetrics()
    recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed = scenario_passed(scenario, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        detail = result.to_report_dict()
        detail.pop("scenarios", None)
        detail.pop("scenario_metrics", None)
        detail.pop("cache_comparison", None)
        combined.scenario_metrics[scenario.name] = detail

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.false_hit_count += result.false_hit_count
        combined.cache_entry_count = result.cache_entry_count
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.billed_input_tokens += result.billed_input_tokens
        combined.billed_output_tokens += result.billed_output_tokens
        combined.estimated_tokens_saved += result.estimated_tokens_saved
        combined.concurrency_level = max(combined.concurrency_level, result.concurrency_level)
        combined.max_in_flight = max(combined.max_in_flight, result.max_in_flight)
        combined.cache_keys = result.cache_keys
        combined.latencies_ms.extend(result.latencies_ms)
        for route_reason, count in result.route_counts.items():
            combined.route_counts[route_reason] = combined.route_counts.get(route_reason, 0) + count
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    if recovery_times:
        combined.recovery_time_ms = sum(recovery_times) / len(recovery_times)

    if include_cache_comparison and config.cache.enabled:
        no_cache_config = config.model_copy(
            update={"cache": config.cache.model_copy(update={"enabled": False})}
        )
        without_cache = run_simulation(
            no_cache_config,
            queries,
            include_cache_comparison=False,
        )
        combined.cache_comparison = {
            "without_cache": _comparison_summary(without_cache),
            "with_cache": _comparison_summary(combined),
        }

    return combined
