from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    name: str
    fail_rate: float = Field(ge=0.0, le=1.0)
    base_latency_ms: int = Field(gt=0)
    cost_per_1k_tokens: float = Field(ge=0.0)


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(gt=0)
    reset_timeout_seconds: float = Field(gt=0)
    success_threshold: int = Field(gt=0)


class CacheConfig(BaseModel):
    enabled: bool = True
    backend: Literal["memory", "redis"] = "memory"
    ttl_seconds: int = Field(gt=0)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    redis_url: str = "redis://localhost:6379/0"


class LoadTestConfig(BaseModel):
    requests: int = Field(gt=0)
    concurrency: int = Field(default=1, gt=0)


class ScenarioConfig(BaseModel):
    name: str
    description: str = ""
    provider_overrides: dict[str, float] = Field(default_factory=dict)
    min_availability: float = Field(default=0.98, ge=0.0, le=1.0)
    min_fallback_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    require_circuit_open: bool = False
    max_circuit_open_count: int | None = Field(default=None, ge=0)
    max_recovery_time_ms: float | None = Field(default=None, gt=0)


class LabConfig(BaseModel):
    seed: int
    providers: list[ProviderConfig]
    circuit_breaker: CircuitBreakerConfig
    cache: CacheConfig
    load_test: LoadTestConfig
    scenarios: list[ScenarioConfig] = Field(default_factory=list)


def load_config(path: str | Path) -> LabConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    return LabConfig.model_validate(raw)
