"""Shadow-price accounting with an injectable token meter.

The kernel never assumes it knows the host's token pricing: callers inject
a meter backed by real usage accounting (upstream issue #54). The ledger
defensively clamps debits so the balance can never go negative, and flags
a warning whenever clamping occurs.

``RealUsageMeter`` is the production meter: it is fed by the host LLM
client's real ``input_tokens`` observations and calibrates per-text token
counts to the host tokenizer's ratio. When no real usage is available it
falls back to the crude char/4 estimate and flags the entry as estimated so
estimates never masquerade as real accounting.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class TokenMeter(Protocol):
    """Injected token meter; the caller provides real usage accounting."""

    def estimate_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class LedgerEntry:
    """One accounting operation on the ledger."""

    operation: str
    tokens: int
    balance: int
    source: str
    warning: str | None = None
    estimated: bool = False


class RealUsageMeter:
    """TokenMeter fed by real LLM usage; falls back to estimation with a flag.

    ``usage_source`` is an optional callable returning the latest real usage
    observation as ``{"input_tokens": int, "source_chars": int}`` (or None);
    ``record_usage`` feeds the meter directly. Both paths are equivalent.
    When a real observation exists, ``estimate_tokens`` calibrates the host
    tokenizer's tokens-per-char ratio; otherwise it falls back to the char/4
    estimate and marks the call as estimated (issue #54: estimates must never
    masquerade as real accounting).
    """

    def __init__(
        self,
        usage_source: Callable[[], dict[str, int] | None] | None = None,
    ) -> None:
        self._usage_source = usage_source
        self._recorded_tokens: int | None = None
        self._recorded_chars: int | None = None
        self._last_estimated = False

    def record_usage(self, input_tokens: int | None, source_chars: int | None = None) -> None:
        """Feed one real usage observation; absent usage clears the meter."""
        if input_tokens is None or input_tokens < 0:
            self._recorded_tokens = None
            self._recorded_chars = None
            return
        self._recorded_tokens = int(input_tokens)
        self._recorded_chars = max(1, int(source_chars or 0))

    def _observation(self) -> dict[str, int] | None:
        if self._usage_source is not None:
            observation = self._usage_source()
            if isinstance(observation, dict) and observation.get("input_tokens") is not None:
                return observation
        recorded_tokens = self._recorded_tokens
        if recorded_tokens is not None:
            return {
                "input_tokens": recorded_tokens,
                "source_chars": self._recorded_chars or 1,
            }
        return None

    def estimate_tokens(self, text: str) -> int:
        observation = self._observation()
        if observation is not None:
            ratio = observation["input_tokens"] / max(1, observation.get("source_chars") or 1)
            self._last_estimated = False
            return max(1, round(len(text) * ratio))
        self._last_estimated = True
        return max(1, (len(text) + 3) // 4)

    @property
    def last_estimate_estimated(self) -> bool:
        """Whether the most recent ``estimate_tokens`` call fell back to estimation."""
        return self._last_estimated

    @property
    def latest_usage_tokens(self) -> int | None:
        """Latest real input_tokens observation, or None when absent."""
        observation = self._observation()
        return observation["input_tokens"] if observation is not None else None


class AcpLedger:
    """Token ledger whose balance is guaranteed never to go negative."""

    def __init__(self, meter: TokenMeter | None = None, initial_balance: int = 0) -> None:
        self._meter = meter
        self._balance = max(0, initial_balance)
        self._entries: list[LedgerEntry] = []
        self._warnings: list[str] = []
        self._last_estimate_estimated = False

    def estimate(self, text: str) -> int:
        """Estimate tokens for ``text`` via the injected meter, or a fallback."""
        if self._meter is not None:
            tokens = max(0, self._meter.estimate_tokens(text))
            self._last_estimate_estimated = bool(
                getattr(self._meter, "last_estimate_estimated", False)
            )
            return tokens
        self._last_estimate_estimated = True
        return max(1, (len(text) + 3) // 4)

    def credit(
        self,
        operation: str,
        tokens: int,
        source: str = "meter",
        estimated: bool | None = None,
    ) -> LedgerEntry:
        tokens = max(0, tokens)
        if estimated is None:
            estimated = self._last_estimate_estimated
        self._balance += tokens
        entry = LedgerEntry(
            operation=operation,
            tokens=tokens,
            balance=self._balance,
            source=source,
            estimated=estimated,
        )
        self._entries.append(entry)
        return entry

    def debit(
        self,
        operation: str,
        tokens: int,
        source: str = "meter",
        estimated: bool | None = None,
    ) -> LedgerEntry:
        tokens = max(0, tokens)
        if estimated is None:
            estimated = self._last_estimate_estimated
        if tokens > self._balance:
            warning = f"debit of {tokens} exceeds balance {self._balance}; clamped to 0"
            self._warnings.append(warning)
            entry = LedgerEntry(
                operation=operation,
                tokens=self._balance,
                balance=0,
                source="clamped",
                warning=warning,
                estimated=estimated,
            )
            self._balance = 0
        else:
            self._balance -= tokens
            entry = LedgerEntry(
                operation=operation,
                tokens=tokens,
                balance=self._balance,
                source=source,
                estimated=estimated,
            )
        self._entries.append(entry)
        return entry

    @property
    def balance(self) -> int:
        return self._balance

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    @property
    def never_negative(self) -> bool:
        return all(entry.balance >= 0 for entry in self._entries)