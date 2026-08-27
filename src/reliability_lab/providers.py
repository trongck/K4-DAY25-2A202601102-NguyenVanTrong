from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


class ProviderError(RuntimeError):
    """Raised when a fake provider fails."""

    def __init__(self, message: str, *, latency_ms: float = 0.0):
        super().__init__(message)
        self.latency_ms = latency_ms


@dataclass(slots=True)
class ProviderResponse:
    provider: str
    text: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost: float


class FakeLLMProvider:
    """Deterministic-enough fake provider for local chaos tests.

    This avoids real API keys while still simulating latency, failures, and cost.
    """

    def __init__(
        self,
        name: str,
        fail_rate: float,
        base_latency_ms: int,
        cost_per_1k_tokens: float,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.name = name
        self.fail_rate = fail_rate
        self.base_latency_ms = base_latency_ms
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self.rng = rng or random.Random()
        self.sleep = sleep
        self._rng_lock = threading.Lock()

    def complete(self, prompt: str) -> ProviderResponse:
        with self._rng_lock:
            jitter_ms = self.rng.randint(0, 60)
            should_fail = self.rng.random() < self.fail_rate
        latency_ms = float(self.base_latency_ms + jitter_ms)
        self.sleep(latency_ms / 1000.0)
        if should_fail:
            raise ProviderError(f"{self.name} simulated failure", latency_ms=latency_ms)
        input_tokens = max(1, len(prompt.split()))
        with self._rng_lock:
            output_tokens = self.rng.randint(20, 80)
        cost = (input_tokens + output_tokens) / 1000.0 * self.cost_per_1k_tokens
        return ProviderResponse(
            provider=self.name,
            text=f"[{self.name}] reliable answer for: {prompt[:60]}",
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=cost,
        )
