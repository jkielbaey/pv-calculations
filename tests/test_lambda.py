import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from lambda_function import parse_entries, mark_cheapest, summarize_hours, format_table, fetch_solar_forecast, fetch_current_soc

BRUSSELS = ZoneInfo("Europe/Brussels")
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def winter_data():
    return load_fixture("winter_2025_01_15.json")


@pytest.fixture
def summer_data():
    return load_fixture("summer_2025_07_15.json")


@pytest.fixture
def winter_hours(winter_data):
    return parse_entries(winter_data)


@pytest.fixture
def summer_hours(summer_data):
    return parse_entries(summer_data)


# ── parse_entries ────────────────────────────────────────────────────────────

class TestParseEntries:
    def test_winter_count(self, winter_hours):
        assert len(winter_hours) == 24

    def test_winter_first_hour_midnight_cet(self, winter_hours):
        h, _ = winter_hours[0]
        # UTC 23:00 previous day → CET 00:00 (UTC+1)
        assert h == datetime(2025, 1, 15, 0, 0, tzinfo=BRUSSELS)

    def test_winter_last_hour(self, winter_hours):
        h, _ = winter_hours[-1]
        assert h == datetime(2025, 1, 15, 23, 0, tzinfo=BRUSSELS)

    def test_sorted_ascending(self, winter_hours):
        times = [h for h, _ in winter_hours]
        assert times == sorted(times)

    def test_prices_preserved(self, winter_hours):
        # Hour 04:00 CET should have price 30.0
        hour_04 = next(p for h, p in winter_hours if h.hour == 4)
        assert hour_04 == pytest.approx(30.0)

    def test_summer_dst_first_hour(self, summer_hours):
        """UTC 22:00 previous day → CEST 00:00 (UTC+2), not 23:00."""
        h, _ = summer_hours[0]
        assert h == datetime(2025, 7, 15, 0, 0, tzinfo=BRUSSELS)
        assert h.utcoffset().seconds // 3600 == 2

    def test_summer_dst_offset(self, summer_hours):
        for h, _ in summer_hours:
            assert h.utcoffset() == timedelta(hours=2), f"{h} has wrong offset"


# ── mark_cheapest ────────────────────────────────────────────────────────────

class TestMarkCheapest:
    def test_exactly_n_marked(self, winter_hours):
        marked = mark_cheapest(winter_hours, n=12)
        assert sum(1 for _, _, m in marked if m == "*") == 12

    def test_all_marked_before_21(self, winter_hours):
        marked = mark_cheapest(winter_hours, n=12)
        for h, _, m in marked:
            if m == "*":
                assert h.hour < 21, f"Hour {h.hour} was marked but is >= 21:00"

    def test_correct_hours_marked(self, winter_hours):
        marked = mark_cheapest(winter_hours, n=12)
        marked_hours = {h.hour for h, _, m in marked if m == "*"}
        # 12 cheapest before 21h: 00,01,02,03,04,05,06,07,08 and 12,13,14
        assert marked_hours == {0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14}

    def test_hours_at_and_after_21_never_marked(self, winter_hours):
        # Insert an artificially cheap price at 21:00 — should still not be marked
        cheap_21 = [(h, 1.0 if h.hour == 21 else p) for h, p in winter_hours]
        marked = mark_cheapest(cheap_21, n=12)
        for h, _, m in marked:
            if h.hour >= 21:
                assert m == "", f"Hour {h.hour}:00 should never be marked"

    def test_output_length_unchanged(self, winter_hours):
        marked = mark_cheapest(winter_hours, n=12)
        assert len(marked) == len(winter_hours)

    def test_summer_negative_prices_can_be_marked(self, summer_hours):
        marked = mark_cheapest(summer_hours, n=12)
        marked_hours = {h.hour for h, _, m in marked if m == "*"}
        # Negative-price hours 14,15,16 (CEST) are all before 21h and should be marked
        assert {14, 15, 16}.issubset(marked_hours)


# ── summarize_hours ──────────────────────────────────────────────────────────

class TestSummarizeHours:
    def test_cheapest_hour(self, winter_hours):
        marked = mark_cheapest(winter_hours, n=12)
        summary = summarize_hours(marked)
        assert "04:00" in summary
        assert "30.00" in summary

    def test_longest_contiguous_window(self, winter_hours):
        marked = mark_cheapest(winter_hours, n=12)
        summary = summarize_hours(marked)
        # Longest block is 00:00–09:00 (hours 00–08 inclusive)
        assert "00:00" in summary
        assert "09:00" in summary

    def test_summary_contains_required_sections(self, winter_hours):
        marked = mark_cheapest(winter_hours, n=12)
        summary = summarize_hours(marked)
        assert "Summary:" in summary
        assert "Cheapest hour:" in summary
        assert "Cheapest continuous window:" in summary
        assert "Recommended charging window:" in summary


# ── format_table ─────────────────────────────────────────────────────────────

class TestFormatTable:
    def test_header_present(self, winter_hours):
        marked = mark_cheapest(winter_hours)
        table = format_table(marked)
        assert "Hour (CET)" in table
        assert "Price" in table

    def test_row_count(self, winter_hours):
        marked = mark_cheapest(winter_hours)
        table = format_table(marked)
        # Header + separator + 24 data rows
        lines = table.strip().splitlines()
        assert len(lines) == 26

    def test_marked_rows_contain_asterisk(self, winter_hours):
        marked = mark_cheapest(winter_hours)
        table = format_table(marked)
        asterisk_rows = [l for l in table.splitlines() if "*" in l]
        assert len(asterisk_rows) == 12

    def test_date_appears_in_rows(self, winter_hours):
        marked = mark_cheapest(winter_hours)
        table = format_table(marked)
        assert "2025-01-15" in table


# ── fetch_solar_forecast ─────────────────────────────────────────────────────

SOLAR_ENV = {
    "FORECAST_SOLAR_API_KEY": "testkey",
    "SOLAR_LAT": "51.0",
    "SOLAR_LON": "4.0",
    "SOLAR_DECLINATION": "35",
    "SOLAR_AZIMUTH": "0",
    "SOLAR_KWP": "10.0",
}

FORECAST_RESPONSE = json.dumps({
    "result": {
        "watt_hours_day": {
            "2025-07-15": 18500,
            "2025-07-16": 21000,
        }
    }
}).encode()


class TestFetchSolarForecast:
    def _mock_urlopen(self, payload=FORECAST_RESPONSE):
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_returns_kwh_for_date(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with self._mock_urlopen():
                result = fetch_solar_forecast("2025-07-15")
        assert result == pytest.approx(18.5)

    def test_returns_none_when_date_missing_from_response(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with self._mock_urlopen():
                result = fetch_solar_forecast("2025-07-17")
        assert result is None

    def test_returns_none_when_env_vars_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = fetch_solar_forecast("2025-07-15")
        assert result is None

    def test_returns_none_on_network_error(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                result = fetch_solar_forecast("2025-07-15")
        assert result is None

    def test_returns_none_on_malformed_response(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with self._mock_urlopen(b"not-json"):
                result = fetch_solar_forecast("2025-07-15")
        assert result is None


# ── fetch_current_soc ─────────────────────────────────────────────────────────

FUSIONSOLAR_ENV = {
    "FUSIONSOLAR_USER": "testuser",
    "FUSIONSOLAR_PASSWORD": "testpass",
    "FUSIONSOLAR_DEVICE_ID": "1000000165855596",
    "FUSIONSOLAR_HOST": "eu5.fusionsolar.huawei.com",
}

LOGIN_RESPONSE = json.dumps({"success": True, "failCode": 0, "data": None}).encode()
SOC_RESPONSE = json.dumps({
    "success": True,
    "failCode": 0,
    "data": [{
        "devId": 1000000165855596,
        "dataItemMap": {"battery_soc": 39.0, "rated_capacity": 15.0},
    }],
}).encode()
SESSION_EXPIRED_RESPONSE = json.dumps({"success": False, "failCode": 305}).encode()


class TestFetchCurrentSoc:
    def _mock_resp(self, payload, token="test-token"):
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.headers.get = lambda key, default=None: token if key == "xsrf-token" else default
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def _mock_urlopen(self, responses):
        """responses: list of (payload, token) tuples, one per urlopen call."""
        mocks = [self._mock_resp(p, t) for p, t in responses]
        return patch("urllib.request.urlopen", side_effect=mocks)

    def test_returns_soc_value(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok"), (SOC_RESPONSE, None)]):
                result = fetch_current_soc()
        assert result == pytest.approx(39.0)

    def test_returns_none_when_env_vars_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = fetch_current_soc()
        assert result is None

    def test_returns_none_on_network_error(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                result = fetch_current_soc()
        assert result is None

    def test_returns_none_on_login_failure(self):
        failed_login = json.dumps({"success": False, "failCode": 1}).encode()
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(failed_login, None)]):
                result = fetch_current_soc()
        assert result is None

    def test_retries_on_session_expired(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([
                (LOGIN_RESPONSE, "tok1"),       # first login
                (SESSION_EXPIRED_RESPONSE, None),  # first KPI → expired
                (LOGIN_RESPONSE, "tok2"),       # retry login
                (SOC_RESPONSE, None),           # retry KPI → success
            ]):
                result = fetch_current_soc()
        assert result == pytest.approx(39.0)

    def test_returns_none_when_soc_absent(self):
        no_soc = json.dumps({
            "success": True, "failCode": 0,
            "data": [{"devId": 123, "dataItemMap": {}}],
        }).encode()
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok"), (no_soc, None)]):
                result = fetch_current_soc()
        assert result is None

    def test_returns_none_on_malformed_response(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok"), (b"not-json", None)]):
                result = fetch_current_soc()
        assert result is None
