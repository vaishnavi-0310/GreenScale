"""Adapter for Norwegian day-ahead spot prices (hvakosterstrommen.no).

The provider publishes one JSON file per calendar day and price area, e.g.
``.../api/v1/prices/2026/10-09_NO2.json``. Each row carries an explicit ISO 8601
``time_start``/``time_end`` interval and a ``NOK_per_kWh`` value. We never
assume "array index == hour"; the interval boundaries are the source of truth.

Tomorrow's file is published no earlier than ~13:00 local time, so an HTTP 404
for tomorrow is a normal situation. Callers must treat it as "prices not yet
known" rather than inventing values.
"""

from dataclasses import dataclass
from datetime import date, datetime

import httpx

BASE_URL = "https://www.hvakosterstrommen.no/api/v1/prices"
AREAS = frozenset({"NO1", "NO2", "NO3", "NO4", "NO5"})
DEFAULT_AREA = "NO2"
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class PriceSlot:
    """One contiguous price interval, timezone-aware."""

    start: datetime
    end: datetime
    nok_per_kwh: float


def build_url(day: date, area: str = DEFAULT_AREA) -> str:
    """Return the provider URL for ``day`` and ``area``."""
    if area not in AREAS:
        raise ValueError(f"Unknown Norwegian price area: {area!r}")
    return f"{BASE_URL}/{day:%Y}/{day:%m-%d}_{area}.json"


def parse_rows(rows: list[dict]) -> list[PriceSlot]:
    """Convert raw provider rows into sorted, validated ``PriceSlot`` objects.

    Raises ``ValueError`` for naive timestamps or non-positive intervals and
    ``KeyError``/``TypeError`` when the payload shape is wrong.
    """
    if not isinstance(rows, list):
        raise TypeError("Price payload must be a JSON array")

    slots: list[PriceSlot] = []
    for row in rows:
        start = datetime.fromisoformat(row["time_start"])
        end = datetime.fromisoformat(row["time_end"])
        value = float(row["NOK_per_kWh"])
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("time_start must include a timezone offset")
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("time_end must include a timezone offset")
        if end <= start:
            raise ValueError("Invalid price interval: time_end must be after time_start")
        slots.append(PriceSlot(start=start, end=end, nok_per_kwh=value))

    return sorted(slots, key=lambda slot: slot.start)


def fetch_day(
    day: date,
    area: str = DEFAULT_AREA,
    client: httpx.Client | None = None,
) -> list[PriceSlot]:
    """Download and parse all price intervals for one day in one area.

    ``client`` is injectable so tests can use ``httpx.MockTransport``.
    HTTP errors (including 404 for not-yet-published days) are raised as
    ``httpx.HTTPStatusError``; network errors as ``httpx.HTTPError`` subclasses.
    """
    url = build_url(day, area)

    if client is None:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as own_client:
            response = own_client.get(url)
    else:
        response = client.get(url, timeout=TIMEOUT_SECONDS)

    response.raise_for_status()
    return parse_rows(response.json())


def recommend_for_price(slot: PriceSlot, client=None):
    """Return an LLM run/defer recommendation for one fetched price interval.

    The API data remains the source of truth; this recommendation is advisory
    and does not schedule or start a job.
    """
    from app.llm import decide_price

    return decide_price(slot.nok_per_kwh, client=client)
