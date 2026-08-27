from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict, TypeVar

T = TypeVar("T")


TransitionEntry = TypedDict(
    "TransitionEntry",
    {"from": str, "to": str, "reason": str, "ts": float},
)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Three-state circuit breaker with an auditable transition log.

    - CLOSED: calls pass through; count failures.
    - OPEN: fail fast until reset timeout elapses.
    - HALF_OPEN: allow a probe; close on success or re-open on failure.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[TransitionEntry] = field(default_factory=list)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    timestamp: Callable[[], float] = field(default=time.time, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _half_open_probe_in_flight: bool = field(default=False, init=False, repr=False)

    def allow_request(self) -> bool:
        """Allow closed/half-open calls and fail fast while an open timeout is active."""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.HALF_OPEN:
                if self._half_open_probe_in_flight:
                    return False
                self._half_open_probe_in_flight = True
                return True
            if self.state == CircuitState.OPEN:
                if (
                    self.opened_at is not None
                    and self.clock() - self.opened_at >= self.reset_timeout_seconds
                ):
                    self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                    self._half_open_probe_in_flight = True
                    return True
                return False
            return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function and record its success or failure."""
        if not self.allow_request():
            raise CircuitOpenError("Circuit is OPEN")

        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def record_success(self) -> None:
        """Reset failures and close a half-open circuit after enough probe successes."""
        with self._lock:
            self.failure_count = 0
            self.success_count += 1

            if self.state == CircuitState.HALF_OPEN:
                self._half_open_probe_in_flight = False
                if self.success_count >= self.success_threshold:
                    self._transition(CircuitState.CLOSED, "probe_success")
                    self.success_count = 0

    def record_failure(self) -> None:
        """Open after the threshold or immediately reopen after a failed probe."""
        with self._lock:
            self.failure_count += 1
            self.success_count = 0

            if self.state == CircuitState.HALF_OPEN:
                self._half_open_probe_in_flight = False
                self._transition(CircuitState.OPEN, "probe_failure")
                self.opened_at = self.clock()
            elif self.failure_count >= self.failure_threshold:
                self._transition(CircuitState.OPEN, "failure_threshold_reached")
                self.opened_at = self.clock()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        with self._lock:
            if self.state == new_state:
                return
            entry: TransitionEntry = {
                "from": self.state.value,
                "to": new_state.value,
                "reason": reason,
                "ts": self.timestamp(),
            }
            self.transition_log.append(entry)
            self.state = new_state
