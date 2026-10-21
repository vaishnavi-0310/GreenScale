
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


def test_missing_middle_price_interval():
    now, _ = example_data()

    # Prices are available from 12-13 and 14-15.
    # The 13-14 interval is missing.
    slots = [
        PriceSlot(
            start=now,
            end=now + timedelta(hours=1),
            nok_per_kwh=1.20,
        ),
        PriceSlot(
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=3),
            nok_per_kwh=0.40,
        ),
    ]

    decision = choose_start(
        job_id="job-gap-test",
        job_type="etl",
        priority="medium",
        deadline=now + timedelta(hours=4),
        predicted_seconds=5400,
        prices=slots,
        now=now,
    )

    assert decision.status == "waiting_prices"
    assert decision.scheduled_start is None


def test_equal_prices_choose_earliest_start():
    now, _ = example_data()

    # Both hours have identical prices.
    slots = [
        PriceSlot(
            start=now,
            end=now + timedelta(hours=1),
            nok_per_kwh=1.00,
        ),
        PriceSlot(
            start=now + timedelta(hours=1),
            end=now + timedelta(hours=2),
            nok_per_kwh=1.00,
        ),
    ]

    decision = choose_start(
        job_id="job-equal-price",
        job_type="etl",
        priority="medium",
        deadline=now + timedelta(hours=3),
        predicted_seconds=1800,
        prices=slots,
        now=now,
    )

    assert decision.status == "scheduled"
    assert decision.scheduled_start == now


def test_thirty_minute_job():
    now, slots = example_data()

    decision = choose_start(
        job_id="job-30-minutes",
        job_type="etl",
        priority="medium",
        deadline=now + timedelta(hours=4),
        predicted_seconds=1800,
        prices=slots,
        now=now,
    )

    assert decision.status == "scheduled"
    assert decision.estimated_seconds == 1800
    assert decision.scheduled_start == now + timedelta(hours=2)


def test_exact_deadline_boundary():
    now, slots = example_data()

    # 10-minute job + 5-minute safety buffer
    # Exactly 15 minutes are available.
    deadline = now + timedelta(minutes=15)

    decision = choose_start(
        job_id="job-exact-deadline",
        job_type="etl",
        priority="medium",
        deadline=deadline,
        predicted_seconds=600,
        prices=slots,
        now=now,
    )

    assert decision.status == "scheduled"
    assert decision.scheduled_start == now
    assert decision.estimated_seconds == 600


def test_daylight_saving_time_transition():
    # Norway moves clocks backward on 25 October 2026.
    # 02:00 occurs twice, but with different UTC offsets.

    now = datetime.fromisoformat(
        "2026-10-25T00:30:00+00:00"
    )

    slots = [
        PriceSlot(
            start=datetime.fromisoformat(
                "2026-10-25T02:00:00+02:00"
            ),
            end=datetime.fromisoformat(
                "2026-10-25T02:00:00+01:00"
            ),
            nok_per_kwh=1.50,
        ),
        PriceSlot(
            start=datetime.fromisoformat(
                "2026-10-25T02:00:00+01:00"
            ),
            end=datetime.fromisoformat(
                "2026-10-25T03:00:00+01:00"
            ),
            nok_per_kwh=0.30,
        ),
    ]

    decision = choose_start(
        job_id="job-dst-test",
        job_type="etl",
        priority="medium",
        deadline=now + timedelta(hours=3),
        predicted_seconds=1800,
        prices=slots,
        now=now,
    )

    # The cheaper interval is the second 02:00.
    expected_start = datetime.fromisoformat(
        "2026-10-25T02:00:00+01:00"
    )

    assert decision.status == "scheduled"
    assert decision.scheduled_start == expected_start
    assert decision.scheduled_start.astimezone(
        timezone.utc
    ) == datetime(
        2026, 10, 25, 1, 0,
        tzinfo=timezone.utc
    )
