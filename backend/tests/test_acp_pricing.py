"""Tests for shadow-price accounting with an injectable token meter."""

from app.core.acp import AcpEngine, CompressResult
from app.core.acp.pricing import AcpLedger, RealUsageMeter, TokenMeter


class FixedMeter:
    """Mock meter returning a fixed token count per call."""

    def __init__(self, tokens: int) -> None:
        self._tokens = tokens
        self.calls = 0

    def estimate_tokens(self, text: str) -> int:
        self.calls += 1
        return self._tokens


def test_credit_increases_balance() -> None:
    ledger = AcpLedger()
    entry = ledger.credit("compress", 100)
    assert entry.balance == 100
    assert ledger.balance == 100


def test_debit_decreases_balance() -> None:
    ledger = AcpLedger(initial_balance=100)
    entry = ledger.debit("decompress", 40)
    assert entry.balance == 60
    assert ledger.balance == 60


def test_debit_never_drives_balance_negative() -> None:
    ledger = AcpLedger(initial_balance=10)
    entry = ledger.debit("decompress", 500)
    assert entry.balance == 0
    assert ledger.balance == 0
    assert entry.source == "clamped"
    assert entry.warning is not None
    assert ledger.warnings


def test_never_negative_across_mixed_operations() -> None:
    ledger = AcpLedger(initial_balance=50)
    ledger.credit("compress", 200)
    ledger.debit("decompress", 1000)
    ledger.credit("compress", 30)
    ledger.debit("decompress", 9999)
    assert ledger.never_negative
    assert ledger.balance == 0
    assert len(ledger.warnings) == 2


def test_injected_meter_values_are_used() -> None:
    meter: TokenMeter = FixedMeter(42)
    ledger = AcpLedger(meter=meter)
    assert ledger.estimate("any text") == 42
    assert meter.calls == 1


def test_fallback_estimate_without_meter() -> None:
    ledger = AcpLedger()
    assert ledger.estimate("") == 1
    assert ledger.estimate("abcd") == 1
    assert ledger.estimate("abcdefgh") == 2


def test_entries_are_recorded_in_order() -> None:
    ledger = AcpLedger(initial_balance=10)
    ledger.credit("compress", 5)
    ledger.debit("decompress", 3)
    entries = ledger.entries
    assert [entry.operation for entry in entries] == ["compress", "decompress"]
    assert [entry.balance for entry in entries] == [15, 12]


def test_negative_charges_are_clamped_to_zero() -> None:
    ledger = AcpLedger(initial_balance=10)
    ledger.credit("compress", -5)
    assert ledger.balance == 10
    ledger.debit("decompress", -5)
    assert ledger.balance == 10


def test_real_usage_meter_calibrates_to_host_tokenizer() -> None:
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=100, source_chars=400)
    assert meter.latest_usage_tokens == 100
    assert meter.estimate_tokens("x" * 40) == 10
    assert meter.last_estimate_estimated is False


def test_real_usage_meter_falls_back_to_estimate_with_flag() -> None:
    meter = RealUsageMeter()
    assert meter.latest_usage_tokens is None
    assert meter.estimate_tokens("abcdefgh") == 2
    assert meter.last_estimate_estimated is True


def test_real_usage_meter_reads_usage_source_callable() -> None:
    source = {"input_tokens": 200, "source_chars": 800}
    meter = RealUsageMeter(usage_source=lambda: source)
    assert meter.latest_usage_tokens == 200
    assert meter.estimate_tokens("x" * 80) == 20
    assert meter.last_estimate_estimated is False
    source = None
    assert meter.latest_usage_tokens is None
    assert meter.estimate_tokens("abcdefgh") == 2
    assert meter.last_estimate_estimated is True


def test_real_usage_meter_absent_usage_clears_recorded_observation() -> None:
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=100, source_chars=400)
    meter.record_usage(input_tokens=None)
    assert meter.latest_usage_tokens is None
    assert meter.estimate_tokens("abcdefgh") == 2
    assert meter.last_estimate_estimated is True


def test_ledger_entries_flagged_estimated_when_meter_falls_back() -> None:
    meter = RealUsageMeter()
    ledger = AcpLedger(meter=meter, initial_balance=100)
    ledger.credit("compress", ledger.estimate("removed text"))
    ledger.debit("compress", ledger.estimate("summary"))
    assert all(entry.estimated for entry in ledger.entries)


def test_ledger_entries_not_flagged_when_real_usage_exists() -> None:
    meter = RealUsageMeter()
    meter.record_usage(input_tokens=100, source_chars=400)
    ledger = AcpLedger(meter=meter, initial_balance=100)
    ledger.credit("compress", ledger.estimate("removed text"))
    ledger.debit("compress", ledger.estimate("summary"))
    assert not any(entry.estimated for entry in ledger.entries)


def test_ledger_never_negative_with_real_usage_meter_across_rounds() -> None:
    """Issue #54 regression: real-usage accounting never drives the ledger negative."""
    meter = RealUsageMeter()
    ledger = AcpLedger(meter=meter, initial_balance=50)
    for round_index in range(5):
        meter.record_usage(input_tokens=1000 + round_index, source_chars=4000)
        ledger.credit("compress", ledger.estimate("removed " * 100))
        ledger.debit("compress", ledger.estimate("summary"))
        ledger.debit("decompress", ledger.estimate("restored " * 100))
    assert ledger.never_negative
    assert ledger.balance >= 0


def test_ledger_never_negative_when_usage_absent() -> None:
    """Issue #54 regression: estimate fallback still never drives the ledger negative."""
    meter = RealUsageMeter()
    ledger = AcpLedger(meter=meter, initial_balance=10)
    for _ in range(5):
        ledger.credit("compress", ledger.estimate("removed " * 200))
        ledger.debit("compress", ledger.estimate("summary"))
        ledger.debit("decompress", ledger.estimate("restored " * 500))
    assert ledger.never_negative
    assert ledger.balance >= 0


def test_engine_compress_rounds_never_negative_with_real_usage_meter() -> None:
    """Issue #54 regression at the engine level with the production meter."""
    meter = RealUsageMeter()
    engine = AcpEngine(meter=meter)
    engine.add_messages([(f"m{i}", f"message content {i} " + "中" * 200) for i in range(6)])
    for round_index in range(3):
        meter.record_usage(input_tokens=2000 + round_index, source_chars=8000)
        result = engine.compress(0, 1, "summary")
        assert isinstance(result, CompressResult)
        assert result.ledger_balance >= 0
        restored = engine.decompress(result.summary_block_id)
        assert restored.ledger_balance >= 0
    assert engine.ledger.never_negative