# Reliability Engineering Lab

A production-style reliability layer for an LLM gateway, implemented locally with deterministic fake providers and a Redis-backed shared cache.

## Current status

- Three-state circuit breakers (`CLOSED`, `OPEN`, `HALF_OPEN`) with transition logs and one guarded half-open probe.
- Bounded primary/backup provider chain with a static fallback and explicit route reasons.
- In-memory and Redis semantic caches with TTL, privacy bypasses, and false-hit protection.
- End-to-end latency measurement across cache lookup and the complete provider fallback chain.
- Token-based avoided-cost calculation for cache hits.
- Concurrent chaos/load execution using a configurable thread pool.
- JSON and CSV metrics plus a generated final report.
- Test suite: `35 passed, 7 xpassed` with Redis running.

## Architecture

```text
Request
  |
  v
ReliabilityGateway --> SharedRedisCache -- HIT --> cached response
  |                       |
  |                       +-- MISS
  v
CircuitBreaker(primary) --> primary provider
  | OPEN / failure
  v
CircuitBreaker(backup)  --> backup provider
  | OPEN / failure
  v
static fallback
```

The provider chain is bounded, so a request cannot create an unbounded retry storm. Each response records a stable route and a detailed reason such as `primary:provider=primary`, `fallback:provider=backup`, or `cache_hit:similarity=1.00`.

## Quickstart

Python 3.11+ and Docker are recommended.

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Linux/macOS
source .venv/bin/activate

pip install -e ".[dev]"
docker compose up -d
python -m pytest -v
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/generate_report.py --metrics reports/metrics.json --config configs/default.yaml --out reports/final_report.md
```

The same workflow is available through `make docker-up`, `make test`, `make run-chaos`, and `make report`.

## Configuration

The default configuration is in `configs/default.yaml`:

| Setting | Default | Purpose |
|---|---:|---|
| `seed` | 42 | Reproducible query workload and provider RNG streams |
| `failure_threshold` | 3 | Open a circuit after three consecutive failures |
| `reset_timeout_seconds` | 2 | Delay before the guarded half-open probe |
| `cache.backend` | `redis` | Share cached responses across gateway instances |
| `cache.ttl_seconds` | 300 | Bound response staleness |
| `cache.similarity_threshold` | 0.92 | Reduce low-quality semantic matches |
| `load_test.requests` | 100 | Requests per chaos scenario |
| `load_test.concurrency` | 8 | Maximum concurrent requests |

The three default scenarios are `primary_timeout_100`, `primary_flaky_50`, and `all_healthy`. Each scenario has explicit availability, fallback, circuit, or recovery acceptance criteria.

## Measurement methodology

### End-to-end latency

Timing starts before cache lookup and ends immediately before the gateway response is returned. Provider latency is accumulated across failed attempts, so a successful backup route includes both the failed-primary and backup latency. Cache-hit and static-fallback requests are also included in P50/P95/P99 instead of being recorded as zero or omitted.

### Token-based cache savings

Successful provider responses store these fields in cache metadata:

- input and output token counts;
- provider cost per 1,000 tokens;
- original estimated request cost.

For each cache hit, avoided cost is calculated as:

```text
(current_input_tokens + cached_output_tokens) / 1000 * provider_cost_per_1k_tokens
```

`estimated_tokens_saved` and `estimated_cost_saved` are aggregated in the output metrics.

### Concurrent load

`run_scenario()` submits the seeded request list through `ThreadPoolExecutor`. `concurrency_level` records the configured worker count and `max_in_flight` records observed overlap. The first worker batch is synchronized so the output proves requests were actually in flight concurrently.

The input workload remains seeded. Thread scheduling and shared-cache timing can cause small route-count differences between concurrent runs; scenario acceptance criteria are used instead of requiring byte-for-byte identical output.

## Outputs

Running the chaos command writes:

- `reports/metrics.json` — nested aggregate and per-scenario metrics;
- `reports/metrics.csv` — one-row flattened export for analysis;
- `reports/final_report.md` — generated architecture, SLO, cache comparison, test, Redis, and failure-analysis evidence.

Headline metrics include availability, error rate, P50/P95/P99 end-to-end latency, fallback success rate, cache hit rate, false-hit count, circuit openings, recovery time, billed tokens, estimated tokens/cost saved, concurrency level, and maximum in-flight requests.

## Test suite

With Redis running, `python -m pytest -v` collects 42 tests:

| Test group | Result |
|---|---:|
| Circuit breaker | 12 passed |
| Cache | 9 passed |
| Gateway contract | 4 passed |
| Configuration | 2 passed |
| Metrics | 2 passed |
| Redis cache | 6 passed |
| Completion checks | 7 xpassed |

The XPASS results are intentional: `tests/test_todo_requirements.py` marks the original completion requirements as expected failures, so an XPASS confirms the implementation is present.

## Remaining production considerations

- Circuit state is process-local even though response cache state is shared through Redis.
- Redis semantic lookup scans the namespace and should be replaced by indexed candidate retrieval at larger scale.
- The fake providers estimate tokens using words; a real gateway should use each model's tokenizer and actual billed usage.
- Sustained-load testing should additionally track throughput, queue time, and resource saturation.
