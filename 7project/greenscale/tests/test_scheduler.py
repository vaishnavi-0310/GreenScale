
from datetime import datetime, timedelta, timezone

from app.scheduler import PriceSlot, choose_start


def example_data():
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    prices = [1.20, 0.50, 0.40, 1.10, 1.70]

    slots = [
        PriceSlot(
            start=now + timedelta(hours=i),
            end=now + timedelta(hours=i + 1),
            nok_per_kwh=price,
        )
        for i, price in enumerate(prices)
    ]

    return now, slots


def test_medium_priority_chooses_cheapest():
    now, slots = example_data()

    decision = choose_start(
        job_id="job-101",
        job_type="etl",
        priority="medium",
        deadline=now + timedelta(hours=4),
        predicted_seconds=7200,
        prices=slots,
        now=now,
    )

    assert decision.status == "scheduled"
    assert decision.scheduled_start == now + timedelta(hours=1)


def test_high_priority_starts_now():
    now, slots = example_data()

    decision = choose_start(
        "job-102", "etl", "high",
        now + timedelta(hours=4),
        7200, slots, now,
    )

    assert decision.scheduled_start == now


def test_missing_llm_prediction_uses_fallback():
    now, slots = example_data()

    decision = choose_start(
        "job-103", "etl", "medium",
        now + timedelta(hours=4),
        None, slots, now,
    )

    assert decision.estimated_seconds == 1800
    assert decision.runtime_source == "fallback"


def test_missing_prices_waits():
    now, _ = example_data()

    decision = choose_start(
        "job-104", "etl", "medium",
        now + timedelta(hours=4),
        7200, None, now,
    )

    assert decision.status == "waiting_prices"
    assert decision.scheduled_start is None


def test_impossible_deadline():
    now, slots = example_data()

    decision = choose_start(
        "job-105", "etl", "medium",
        now + timedelta(hours=1),
        7200, slots, now,
    )

    assert decision.status == "at_risk"
    assert decision.scheduled_start == now
