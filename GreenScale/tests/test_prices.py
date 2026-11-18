import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.prices import AREAS, BASE_URL, PriceSlot, build_url, fetch_day, parse_rows

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_prices.json"
DAY = date(2026, 10, 9)
EXPECTED_URL = f"{BASE_URL}/2026/10-09_NO2.json"


def load_fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_client(handler) -> httpx.Client:
    """httpx client whose transport is a plain Python function; no network."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_client(payload, status_code: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, request=request)

    return make_client(handler)


# --- URL construction -------------------------------------------------------


def test_build_url_matches_documented_format():
    assert build_url(DAY, "NO2") == EXPECTED_URL


def test_build_url_zero_pads_month_and_day():
    assert build_url(date(2026, 1, 5), "NO1").endswith("/2026/01-05_NO1.json")


def test_build_url_rejects_unknown_area():
    with pytest.raises(ValueError):
        build_url(DAY, "SE3")


def test_all_norwegian_areas_are_accepted():
    assert AREAS == {"NO1", "NO2", "NO3", "NO4", "NO5"}


# --- Happy path --------------------------------------------------------------


def test_fetch_day_parses_fixture_into_price_slots():
    rows = load_fixture()
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json=rows, request=request)

    slots = fetch_day(DAY, "NO2", client=make_client(handler))

    assert len(requested) == 1
    assert str(requested[0].url) == EXPECTED_URL
    assert len(slots) == len(rows)
    assert all(isinstance(slot, PriceSlot) for slot in slots)
    assert all(isinstance(slot.nok_per_kwh, float) for slot in slots)
    assert all(slot.start.tzinfo is not None and slot.end.tzinfo is not None for slot in slots)


def test_fetch_day_returns_slots_sorted_by_start():
    slots = fetch_day(DAY, client=json_client(load_fixture()))
    starts = [slot.start for slot in slots]
    assert starts == sorted(starts)
    assert slots[0].start == datetime.fromisoformat("2026-10-09T10:00:00+02:00")
    assert slots[-1].end == datetime.fromisoformat("2026-10-09T18:00:00+02:00")


def test_fixture_intervals_are_contiguous_hourly():
    slots = fetch_day(DAY, client=json_client(load_fixture()))
    for earlier, later in zip(slots, slots[1:]):
        assert earlier.end == later.start
        assert later.end - later.start == timedelta(hours=1)


def test_fixture_contains_a_clear_cheap_window():
    slots = fetch_day(DAY, client=json_client(load_fixture()))
    cheapest = min(slots, key=lambda slot: slot.nok_per_kwh)
    assert cheapest.start == datetime.fromisoformat("2026-10-09T14:00:00+02:00")
    assert cheapest.nok_per_kwh == 0.4


def test_slots_are_comparable_as_aware_datetimes():
    slots = fetch_day(DAY, client=json_client(load_fixture()))
    now_utc = datetime(2026, 10, 9, 11, 30, tzinfo=timezone.utc)  # 13:30 Oslo
    future = [slot for slot in slots if slot.start >= now_utc]
    assert future[0].start == datetime.fromisoformat("2026-10-09T14:00:00+02:00")


def test_fetch_day_uses_default_area_no2():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[], request=request)

    assert fetch_day(DAY, client=make_client(handler)) == []
    assert seen == [EXPECTED_URL]


# --- Error handling ----------------------------------------------------------


def test_unknown_area_raises_before_any_http_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[], request=request)

    with pytest.raises(ValueError):
        fetch_day(DAY, "SE3", client=make_client(handler))
    assert calls == []


def test_404_for_unpublished_day_raises_http_status_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found", request=request)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        fetch_day(DAY + timedelta(days=1), client=make_client(handler))
    assert exc_info.value.response.status_code == 404


def test_server_error_raises_http_status_error():
    with pytest.raises(httpx.HTTPStatusError):
        fetch_day(DAY, client=json_client({"error": "boom"}, status_code=503))


def test_timeout_propagates_as_httpx_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=request)

    with pytest.raises(httpx.TimeoutException):
        fetch_day(DAY, client=make_client(handler))


@pytest.mark.parametrize(
    "bad_row",
    [
        pytest.param(
            {
                "NOK_per_kWh": 0.5,
                "time_start": "2026-10-09T13:00:00+02:00",
                "time_end": "2026-10-09T13:00:00+02:00",
            },
            id="end-equals-start",
        ),
        pytest.param(
            {
                "NOK_per_kWh": 0.5,
                "time_start": "2026-10-09T14:00:00+02:00",
                "time_end": "2026-10-09T13:00:00+02:00",
            },
            id="end-before-start",
        ),
        pytest.param(
            {
                "NOK_per_kWh": 0.5,
                "time_start": "2026-10-09T13:00:00",
                "time_end": "2026-10-09T14:00:00+02:00",
            },
            id="naive-start",
        ),
        pytest.param(
            {
                "NOK_per_kWh": 0.5,
                "time_start": "2026-10-09T13:00:00+02:00",
                "time_end": "2026-10-09T14:00:00",
            },
            id="naive-end",
        ),
        pytest.param(
            {
                "NOK_per_kWh": "not-a-number",
                "time_start": "2026-10-09T13:00:00+02:00",
                "time_end": "2026-10-09T14:00:00+02:00",
            },
            id="non-numeric-price",
        ),
        pytest.param(
            {
                "NOK_per_kWh": 0.5,
                "time_start": "yesterday",
                "time_end": "2026-10-09T14:00:00+02:00",
            },
            id="unparseable-timestamp",
        ),
    ],
)
def test_malformed_row_raises_value_error(bad_row):
    with pytest.raises(ValueError):
        fetch_day(DAY, client=json_client([bad_row]))


def test_missing_key_raises_key_error():
    row = {"time_start": "2026-10-09T13:00:00+02:00", "time_end": "2026-10-09T14:00:00+02:00"}
    with pytest.raises(KeyError):
        fetch_day(DAY, client=json_client([row]))


def test_non_array_payload_raises_type_error():
    with pytest.raises(TypeError):
        fetch_day(DAY, client=json_client({"prices": []}))


def test_parse_rows_is_pure_and_reusable_for_fixtures():
    slots = parse_rows(load_fixture())
    assert len(slots) == 8
    assert slots[0].nok_per_kwh == 0.95
