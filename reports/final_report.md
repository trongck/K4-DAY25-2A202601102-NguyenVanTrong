# Day 25 Reliability Final Report

## 1. Architecture summary

The gateway checks the shared cache before routing through a bounded provider chain. Each provider has its own three-state circuit breaker, and the chain ends in a static fallback instead of an unbounded retry loop.

```text
User -> ReliabilityGateway -> SharedRedisCache
                              | MISS
                              v
                     CircuitBreaker(primary) -> Provider primary
                              | OPEN / failure
                              v
                     CircuitBreaker(backup)  -> Provider backup
                              | OPEN / failure
                              v
                         Static fallback
```

Routes retain the stable contract (`primary`, `fallback`, `cache_hit`, or `static_fallback`) while `route_reason` records the selected provider name. Circuit transitions record from/to state, reason, and timestamp.

## 2. Configuration

| Setting | Value | Rationale |
|---|---:|---|
| seed | 42 | Reproduce query selection and provider randomness |
| failure_threshold | 3 | Open quickly without reacting to one isolated failure |
| reset_timeout_seconds | 2 | Wait before allowing a half-open recovery probe |
| success_threshold | 1 | Successful probes required before closing the circuit |
| cache.enabled | True | Enable cost-saving response reuse |
| cache.backend | redis | Share cached responses across instances |
| cache.ttl_seconds | 300 | Limit response staleness |
| cache.similarity_threshold | 0.92 | High threshold reduces semantic false hits |
| load_test.requests | 100 | Requests per named scenario |
| load_test.concurrency | 8 | Concurrent workers used by the load generator |
| provider.primary.fail_rate | 0.25 | Configured baseline failure probability |
| provider.primary.base_latency_ms | 180 | Simulated provider latency before jitter |
| provider.primary.cost_per_1k_tokens | 0.01 | Estimated token cost |
| provider.backup.fail_rate | 0.05 | Configured baseline failure probability |
| provider.backup.base_latency_ms | 260 | Simulated provider latency before jitter |
| provider.backup.cost_per_1k_tokens | 0.006 | Estimated token cost |

## 3. SLO definitions

| SLI | Target | Actual | Met? |
|---|---|---:|---|
| Availability | >= 99% | 0.9933 | Yes |
| Latency P95 | < 2500 ms | 501.62 | Yes |
| Fallback success rate | >= 95% | 0.9733 | Yes |
| Cache hit rate | >= 10% | 0.53 | Yes |
| Recovery time | < 5000 ms | 2325 | Yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 300 |
| successful_requests | 298 |
| failed_requests | 2 |
| availability | 0.9933 |
| error_rate | 0.0067 |
| latency_p50_ms | 25.3 |
| latency_p95_ms | 501.62 |
| latency_p99_ms | 547.1 |
| fallback_success_rate | 0.9733 |
| cache_hit_rate | 0.53 |
| false_hit_count | 5 |
| cache_entry_count | 13 |
| circuit_open_count | 8 |
| recovery_time_ms | 2325 |
| estimated_cost | 0.0664 |
| estimated_cost_saved | 0.0712 |
| billed_input_tokens | 1125 |
| billed_output_tokens | 7255 |
| estimated_tokens_saved | 9350 |
| concurrency_level | 8 |
| max_in_flight | 8 |

Route reasons (including provider names):

```json
{
  "fallback:provider=backup": 73,
  "static_fallback:last_error=backup simulated failure": 2,
  "cache_hit:similarity=0.98": 27,
  "cache_hit:similarity=1.00": 122,
  "cache_hit:similarity=0.97": 10,
  "primary:provider=primary": 66
}
```

The seeded query workload and provider RNG streams make scenario inputs reproducible. Concurrent scheduling can cause small run-to-run differences in routing order.

## 5. Cache comparison

The runner automatically repeats the same seeded scenarios with cache disabled.

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| availability | 0.9933 | 0.9933 | 0 |
| latency_p50_ms | 265 | 25.3 | -239.7 |
| latency_p95_ms | 519 | 501.62 | -17.38 |
| estimated_cost | 0.1415 | 0.0664 | -0.0751 |
| cache_hit_rate | 0 | 0.53 | 0.53 |
| circuit_open_count | 19 | 8 | -11 |

The semantic cache uses character n-gram cosine similarity. Privacy-related queries bypass storage, and a query for a 2026 policy cannot reuse a cached 2024 policy even when their semantic score exceeds the threshold.
Cost saved is calculated per cache hit from the current prompt's input-token count, the cached output-token count, and the original provider's per-1K-token rate.

## 6. Redis shared cache

An in-memory cache is private to one process. SharedRedisCache stores Redis hashes with a common namespace and TTL, so a second gateway instance can read entries written by the first while privacy and false-hit guardrails remain active.

Observed cache entries after the final scenario: `13`.

Verification results:

| Test group | Result |
|---|---:|
| Circuit breaker | 12 passed |
| In-memory cache | 9 passed |
| Gateway contract | 4 passed |
| Redis cache | 6 passed |

Redis CLI-equivalent evidence captured from the final scenario returned 13 shared-cache keys:

```text
rl:cache:095946136fea
rl:cache:0bc3b1acf73d
rl:cache:3936614ac4c2
rl:cache:3dab98c0e49e
rl:cache:4fc3c69b9376
rl:cache:734852f3cf4a
rl:cache:844ef0143a5c
rl:cache:8baa2cfa11fa
rl:cache:98332d0d1c9c
rl:cache:9e413fd814eb
rl:cache:d354658dc020
rl:cache:dacb2b833659
rl:cache:fff10da1c72c
```

Manual verification: `docker compose exec redis redis-cli KEYS "rl:cache:*"`.

## 7. Chaos scenarios

| Scenario | Expected criteria | Observed | Status |
|---|---|---|---|
| primary_timeout_100 | availability >= 98%; fallback >= 95%; circuit opens | availability=0.99, fallback=0.9783, cache_hit=0.54, circuit_open=6, recovery=N/A ms, max_in_flight=8 | pass |
| primary_flaky_50 | availability >= 98%; fallback >= 90%; circuit opens; recovery <= 2500 ms | availability=0.99, fallback=0.9655, cache_hit=0.52, circuit_open=2, recovery=2325 ms, max_in_flight=8 | pass |
| all_healthy | availability >= 100%; circuit opens <= 0 | availability=1, fallback=0, cache_hit=0.53, circuit_open=0, recovery=N/A ms, max_in_flight=8 | pass |

## 8. Failure analysis

Latency now covers cache lookup and the complete primary/fallback attempt chain, and cache savings use token metadata rather than a fixed estimate. The main remaining weakness is that circuit state is process-local. Redis shares response data, but multiple gateway instances may temporarily disagree about provider health. This is acceptable for the lab but should be coordinated or exported to centralized observability in production.

Redis semantic lookup scans the cache namespace, so lookup cost grows linearly with the number of entries. A production design should use a vector index or a bounded candidate set instead of scanning every key.

## 9. Next steps

1. Store circuit state in a shared backend or export it to centralized observability.
2. Replace Redis namespace scans with an indexed similarity search.
3. Add sustained throughput tests that report requests per second and saturation.

Full-suite verification with Redis running: `35 passed, 7 xpassed`. The seven XPASS results are the expected completion checks in `tests/test_todo_requirements.py`.
