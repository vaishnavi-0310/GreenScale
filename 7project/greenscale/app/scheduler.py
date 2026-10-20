from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

# Temporary estimates
FALLBACK_SECONDS = {
    "etl": 1800,
    "backup": 1200,
    "report": 900,
}

@dataclass(frozen=True)
class PriceSlot:
    start: datetime
    end: datetime
    nok_per_kwh: float

@dataclass(frozen=True)
class Decision:
    status: str
    scheduled_start: datetime | None
    reason: str
    estimated_seconds: int
    runtime_source: str
    price_score: float | None = None

def _aware(value):
    return(
        value is not None
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )

def _price_score(start, end, slots):
    cursor = start
    score = 0.0

    for slot in slots:
        if slot.end <= cursor:
            continue

        # missing interval between two prices
        if slot.start > cursor:
            return None

        covered_until = min((end, slot.end))
        hours = (covered_until - cursor).total_seconds() / 3600

        score += slot.nok_per_kwh * hours
        cursor = covered_until

        if cursor >= end:
            return score

    return None


def choose_start(job_id, job_type, priority, deadline,
                 predicted_seconds, prices, now,
                 runtime_source="stored_estimate"):

    # 1. check mandatory information
    if not job_id:
        raise ValueError("Job ID is required")

    if job_type not in FALLBACK_SECONDS:
        raise ValueError("Unsupported or missing job_type")

    priority = "medium" if priority is None else priority

    if priority not in {"high", "medium", "low"}:
        raise ValueError("Invalid priority")

    if not _aware(now) or not _aware(deadline):
        raise ValueError("now and dealine must include timezone")

    # 2. check LLM prediction or fallback
    valid_prediction = (
        isinstance(predicted_seconds, int)
        and not isinstance(predicted_seconds, bool)
        and 0 < predicted_seconds <= 86400
    )

    if valid_prediction:
        seconds = predicted_seconds
        source = runtime_source
    else:
        seconds = FALLBACK_SECONDS[job_type]
        source = "fallback"

    # 3. reserve am safety margin for runtime uncertainty
    buffer_seconds = max(300, int(seconds * 0.2))
    reserved_seconds = seconds + buffer_seconds

    latest_start = deadline - timedelta(
        seconds=reserved_seconds
    )

    # 4. handle deadline that cannot be met
    if latest_start < now:
        return Decision(
            "at_risk",
            now,
            "insufficient time before deadline; immidiate attempt",
            seconds,
            source,
        )

    # 5. high-priority jobs do not wait for cheap electricity
    if priority == "high":
        return Decision(
            "scheduled",
            now,
            "High priority: start immidiately",
            seconds,
            source,
        )

    # 6. validate and order electricity prices
    slots = sorted(prices or [], key=lambda slot: slot.start)

    for i, slot in enumerate(slots):
        if (
            not _aware(slot.start)
            or not _aware(slot.end)
            or slot.end <= slot.start
        ):
            raise ValueError("Invalid price interval")

        if not isfinite(slot.nok_per_kwh):
            raise ValueError("Invalid electricity price")

        if i and slot.start < slots[i - 1].end:
            raise ValueError("overlapping price intervals")

    # 7. consider now and every available price boundary
    duration = timedelta(seconds=seconds)

    starts = {now}
    starts.update(
        slot.start for slot in slots
        if now <= slot.start <= latest_start
    )

    options = []

    for start in sorted(starts):
        if start > latest_start:
            continue

        score = _price_score(
            start,
            start + duration,
            slots
        )

        if score is not None:
            options.append((score, start))

    # 8. choose the lowest score; ealiest wins a tie
    if options:
        score, start = min(options)

        return Decision(
            "scheduled",
            start,
            "Lowest available price score",
            seconds,
            source,
            round(score, 4),
        )

    # 9. handle unavailable or incomplete electricity data
    if latest_start - now > timedelta(minutes=30):
        return Decision(
            "waiting_prices",
            None,
            "No complete price coverage; retry later",
            seconds,
            source,
        )

    return Decision(
        "scheduled",
        now,
        "Price data missing and deadline is close; start now",
        seconds,
        source,
    )