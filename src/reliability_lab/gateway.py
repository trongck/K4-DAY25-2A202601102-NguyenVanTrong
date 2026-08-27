from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_saved: float = 0.0
    error: str | None = None
    route_reason: str = ""


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        started_at = time.perf_counter()
        cache_lookup_ms = 0.0
        attempted_provider_latency_ms = 0.0

        if self.cache is not None:
            cache_started_at = time.perf_counter()
            cached_text, score, metadata = self.cache.get_with_metadata(prompt)
            cache_lookup_ms = (time.perf_counter() - cache_started_at) * 1000.0
            if cached_text is not None:
                input_tokens = max(1, len(prompt.split()))
                output_tokens = _as_int(metadata.get("output_tokens"))
                cost_per_1k_tokens = _as_float(metadata.get("cost_per_1k_tokens"))
                cost_saved = (input_tokens + output_tokens) / 1000.0 * cost_per_1k_tokens
                if cost_saved == 0.0:
                    cost_saved = _as_float(metadata.get("estimated_cost"))
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=max((time.perf_counter() - started_at) * 1000.0, 0.001),
                    estimated_cost=0.0,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_saved=cost_saved,
                    route_reason=f"cache_hit:similarity={score:.2f}",
                )

        last_error = None

        for i, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)

                if self.cache is not None:
                    self.cache.set(
                        prompt,
                        response.text,
                        {
                            "provider": provider.name,
                            "input_tokens": response.input_tokens,
                            "output_tokens": response.output_tokens,
                            "cost_per_1k_tokens": provider.cost_per_1k_tokens,
                            "estimated_cost": response.estimated_cost,
                        },
                    )

                route = "primary" if i == 0 else "fallback"

                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=max(
                        cache_lookup_ms + attempted_provider_latency_ms + response.latency_ms,
                        (time.perf_counter() - started_at) * 1000.0,
                    ),
                    estimated_cost=response.estimated_cost,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    route_reason=f"{route}:provider={provider.name}",
                )
            except (ProviderError, CircuitOpenError) as e:
                last_error = str(e)
                attempted_provider_latency_ms += getattr(e, "latency_ms", 0.0)
                continue

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=max(
                cache_lookup_ms + attempted_provider_latency_ms,
                (time.perf_counter() - started_at) * 1000.0,
            ),
            estimated_cost=0.0,
            error=last_error,
            route_reason=f"static_fallback:last_error={last_error}",
        )


def _as_float(value: object) -> float:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            pass
    return 0.0


def _as_int(value: object) -> int:
    return int(_as_float(value))
