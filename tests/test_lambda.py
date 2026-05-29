import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from lambda_function import (
    parse_entries, mark_cheapest, summarize_hours, format_table,
    fetch_solar_forecast, fetch_solar_hourly,
    fetch_current_soc, fetch_median_daily_consumption,
    _fetch_fusionsolar_data,
    classify_scenario, find_negative_price_window,
    find_cheapest_overnight_window, recommend_action,
    format_recommendation, prepare_email,
    simulate_day, recommend, MIN_NET_BENEFIT_EUR,
)

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
    "FUSIONSOLAR_PLANT_ID": "NE=TestPlant001",
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


# ── fetch_solar_hourly ───────────────────────────────────────────────────────

HOURLY_RESPONSE = json.dumps({
    "result": {
        "watt_hours_period": {
            "2025-07-15 06:00:00": 500,
            "2025-07-15 07:00:00": 1500,
            "2025-07-15 08:00:00": 3000,
            "2025-07-15 12:00:00": 5000,
            "2025-07-16 06:00:00": 600,
        }
    }
}).encode()


class TestFetchSolarHourly:
    def _mock_urlopen(self, payload=HOURLY_RESPONSE):
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_returns_hourly_kwh_for_date(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with self._mock_urlopen():
                result = fetch_solar_hourly("2025-07-15")
        assert result == {6: pytest.approx(0.5), 7: pytest.approx(1.5),
                          8: pytest.approx(3.0), 12: pytest.approx(5.0)}

    def test_aggregates_subhour_periods(self):
        payload = json.dumps({
            "result": {
                "watt_hours_period": {
                    "2025-07-15 07:00:00": 200,
                    "2025-07-15 07:15:00": 300,
                    "2025-07-15 07:30:00": 400,
                    "2025-07-15 07:45:00": 500,
                }
            }
        }).encode()
        with patch.dict("os.environ", SOLAR_ENV):
            with self._mock_urlopen(payload):
                result = fetch_solar_hourly("2025-07-15")
        assert result == {7: pytest.approx(1.4)}

    def test_returns_none_when_no_data_for_date(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with self._mock_urlopen():
                result = fetch_solar_hourly("2025-07-20")
        assert result is None

    def test_returns_none_when_env_vars_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = fetch_solar_hourly("2025-07-15")
        assert result is None

    def test_returns_none_on_network_error(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                result = fetch_solar_hourly("2025-07-15")
        assert result is None

    def test_returns_none_on_malformed_response(self):
        with patch.dict("os.environ", SOLAR_ENV):
            with self._mock_urlopen(b"not-json"):
                result = fetch_solar_hourly("2025-07-15")
        assert result is None


# ── fetch_median_daily_consumption ───────────────────────────────────────────

def _consumption_day_response(use_power=None):
    item_map = {} if use_power is None else {"use_power": use_power}
    return json.dumps({
        "success": True, "failCode": 0,
        "data": [{"stationCode": "NE=TestPlant001", "dataItemMap": item_map}],
    }).encode()


class TestFetchMedianDailyConsumption:
    def _mock_resp(self, payload, token=None):
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

    def _day_calls(self, use_powers):
        """Build (payload, None) tuples for day-query responses."""
        return [(_consumption_day_response(v), None) for v in use_powers]

    def test_returns_median_of_7_days(self):
        values = [18.0 + i for i in range(7)]  # 18..24, median = 21
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok")] + self._day_calls(values)):
                result = fetch_median_daily_consumption()
        assert result == pytest.approx(21.0)

    def test_median_ignores_phev_outliers(self):
        # 5 baseline days + 2 PHEV charging days (much higher)
        # Sorted: [8.5, 8.8, 9.0, 9.2, 9.5, 27.0, 28.5] → median = 9.2
        values = [9.0, 9.5, 8.5, 9.2, 8.8, 27.0, 28.5]
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok")] + self._day_calls(values)):
                result = fetch_median_daily_consumption()
        assert result == pytest.approx(9.2)

    def test_skips_missing_day(self):
        # 6 days with data, 1 missing (None key in dataItemMap)
        values = [20.0] * 6 + [None]
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok")] + self._day_calls(values)):
                result = fetch_median_daily_consumption()
        assert result == pytest.approx(20.0)

    def test_returns_none_when_all_missing(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok")] + self._day_calls([None] * 7)):
                result = fetch_median_daily_consumption()
        assert result is None

    def test_returns_none_when_env_vars_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = fetch_median_daily_consumption()
        assert result is None

    def test_returns_none_on_network_error(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                result = fetch_median_daily_consumption()
        assert result is None

    def test_retries_on_session_expired(self):
        expired = json.dumps({"success": False, "failCode": 305}).encode()
        values = [20.0] * 7
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen(
                [(LOGIN_RESPONSE, "tok1"), (expired, None),   # login + day1 → expired
                 (LOGIN_RESPONSE, "tok2")] + self._day_calls(values)  # retry from day1
            ):
                result = fetch_median_daily_consumption()
        assert result == pytest.approx(20.0)


# ── _fetch_fusionsolar_data ──────────────────────────────────────────────────

class TestFetchFusionsolarData:
    def _mock_resp(self, payload, token=None):
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.headers.get = lambda key, default=None: token if key == "xsrf-token" else default
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def _mock_urlopen(self, responses):
        mocks = [self._mock_resp(p, t) for p, t in responses]
        return patch("urllib.request.urlopen", side_effect=mocks)

    def _day_calls(self, use_powers):
        return [(_consumption_day_response(v), None) for v in use_powers]

    def test_returns_both_values_with_single_login(self):
        day_responses = self._day_calls([20.0] * 7)
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen(
                [(LOGIN_RESPONSE, "tok"), (SOC_RESPONSE, None)] + day_responses
            ) as mock_urlopen:
                result = _fetch_fusionsolar_data()
        assert result["soc"] == pytest.approx(39.0)
        assert result["median_consumption"] == pytest.approx(20.0)
        assert mock_urlopen.call_count == 9  # 1 login + 1 SOC + 7 days

    def test_returns_null_dict_when_env_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = _fetch_fusionsolar_data()
        assert result == {"soc": None, "median_consumption": None}

    def test_retries_both_on_session_expired(self):
        expired = json.dumps({"success": False, "failCode": 305}).encode()
        day_responses = self._day_calls([20.0] * 7)
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen(
                [(LOGIN_RESPONSE, "tok1"), (expired, None),          # login + SOC → expired
                 (LOGIN_RESPONSE, "tok2"), (SOC_RESPONSE, None)] + day_responses  # retry both
            ):
                result = _fetch_fusionsolar_data()
        assert result["soc"] == pytest.approx(39.0)
        assert result["median_consumption"] == pytest.approx(20.0)


# ── M4 helpers ───────────────────────────────────────────────────────────────

def _make_prices(hour_to_price, base_date=None):
    base = base_date or datetime(2025, 1, 15, tzinfo=BRUSSELS)
    if isinstance(hour_to_price, dict):
        return [(base.replace(hour=h), hour_to_price.get(h, 100.0)) for h in range(24)]
    return [(base.replace(hour=h), p) for h, p in enumerate(hour_to_price)]


# ── classify_scenario ────────────────────────────────────────────────────────

class TestClassifyScenario:
    def test_excess(self):
        # stored=13.5, surplus=15, total=28.5 > 15
        assert classify_scenario(forecast_kwh=20.0, current_soc=90.0, avg_consumption=5.0) == "EXCESS"

    def test_deficit(self):
        # stored=1.5, stored+forecast=6.5 < 20
        assert classify_scenario(forecast_kwh=5.0, current_soc=10.0, avg_consumption=20.0) == "DEFICIT"

    def test_balanced(self):
        # stored=7.5, surplus=0; 7.5+15 >= 15 and 7.5 <= 15
        assert classify_scenario(forecast_kwh=15.0, current_soc=50.0, avg_consumption=15.0) == "BALANCED"

    def test_excess_boundary_is_balanced(self):
        # stored=15 (soc=100%), surplus=0 (forecast<=consumption), total=15 → not > 15
        assert classify_scenario(forecast_kwh=10.0, current_soc=100.0, avg_consumption=15.0) == "BALANCED"

    def test_just_above_excess_boundary(self):
        # stored=15, surplus=1, total=16 > 15 → EXCESS
        assert classify_scenario(forecast_kwh=16.0, current_soc=100.0, avg_consumption=15.0) == "EXCESS"

    def test_deficit_boundary_is_balanced(self):
        # stored=0, forecast=10, consumption=10 → 0+10 = 10, not < 10
        assert classify_scenario(forecast_kwh=10.0, current_soc=0.0, avg_consumption=10.0) == "BALANCED"

    def test_just_below_deficit_boundary(self):
        # stored=0, forecast=9.9, consumption=10 → 9.9 < 10 → DEFICIT
        assert classify_scenario(forecast_kwh=9.9, current_soc=0.0, avg_consumption=10.0) == "DEFICIT"


# ── find_negative_price_window ───────────────────────────────────────────────

class TestFindNegativePriceWindow:
    def test_no_negatives_returns_none(self):
        prices = _make_prices([10.0] * 24)
        assert find_negative_price_window(prices) is None

    def test_zero_prices_returns_none(self):
        prices = _make_prices([0.0] * 24)
        assert find_negative_price_window(prices) is None

    def test_single_negative_hour(self):
        vals = [10.0] * 24
        vals[14] = -5.0
        prices = _make_prices(vals)
        start, end = find_negative_price_window(prices)
        assert start.hour == 14
        assert end == start + timedelta(hours=1)

    def test_contiguous_negative_block(self):
        vals = [10.0] * 24
        vals[13] = -10.0
        vals[14] = -20.0
        vals[15] = -5.0
        prices = _make_prices(vals)
        start, end = find_negative_price_window(prices)
        assert start.hour == 13
        assert end.hour == 16

    def test_returns_first_of_multiple_blocks(self):
        vals = [10.0] * 24
        vals[5] = -1.0
        vals[15] = -2.0
        vals[16] = -3.0
        prices = _make_prices(vals)
        start, end = find_negative_price_window(prices)
        assert start.hour == 5


# ── find_cheapest_overnight_window ───────────────────────────────────────────

class TestFindCheapestOvernightWindow:
    def test_single_hour_window(self):
        # charge_kwh=5 → window_size=1 (ceil(5/5)=1)
        vals = {h: 50.0 for h in range(24)}
        vals[3] = 10.0
        prices = _make_prices(vals)
        start, end = find_cheapest_overnight_window(prices, charge_kwh=5.0)
        assert start.hour == 3
        assert end == start + timedelta(hours=1)

    def test_two_hour_window(self):
        # charge_kwh=8 → window_size=2 (ceil(8/5)=2)
        vals = {h: 50.0 for h in range(24)}
        vals[2] = 10.0
        vals[3] = 11.0
        prices = _make_prices(vals)
        start, end = find_cheapest_overnight_window(prices, charge_kwh=8.0)
        assert start.hour == 2
        assert end.hour == 4

    def test_only_overnight_hours(self):
        # Daytime hour 10 is very cheap — result must still be an overnight hour
        vals = {h: 100.0 for h in range(24)}
        for h in list(range(8)) + [22, 23]:
            vals[h] = 50.0
        vals[10] = 1.0
        prices = _make_prices(vals)
        start, end = find_cheapest_overnight_window(prices, charge_kwh=5.0)
        assert start.hour in set(range(8)) | {22, 23}


# ── recommend_action ─────────────────────────────────────────────────────────

class TestRecommendAction:
    def _flat(self, price=100.0):
        return _make_prices([price] * 24)

    def _cheap_overnight(self):
        # Overnight (00-07, 22-23): 20 €/MWh; daytime (08-21): 200 €/MWh
        vals = {h: 200.0 for h in range(24)}
        for h in list(range(8)) + [22, 23]:
            vals[h] = 20.0
        return _make_prices(vals)

    def _with_negative_midday(self):
        vals = [100.0] * 24
        vals[14] = -20.0
        vals[15] = -10.0
        return _make_prices(vals)

    def test_none_forecast_gives_no_action(self):
        assert recommend_action("BALANCED", self._flat(), 50.0, None)["action"] == "NO_ACTION"

    def test_none_soc_gives_no_action(self):
        assert recommend_action("BALANCED", self._flat(), None, 15.0)["action"] == "NO_ACTION"

    def test_none_prices_gives_no_action(self):
        assert recommend_action("BALANCED", None, 50.0, 15.0)["action"] == "NO_ACTION"

    def test_result_has_required_keys(self):
        result = recommend_action("BALANCED", self._flat(), 50.0, 15.0)
        assert {"action", "start_time", "end_time", "target_soc", "rationale", "estimated_saving"} <= result.keys()

    def test_balanced_returns_no_action(self):
        assert recommend_action("BALANCED", self._flat(), 50.0, 15.0)["action"] == "NO_ACTION"

    def test_excess_with_negative_prices_returns_force_discharge(self):
        result = recommend_action("EXCESS", self._with_negative_midday(), 80.0, 20.0)
        assert result["action"] == "FORCE_DISCHARGE"

    def test_force_discharge_has_start_and_end(self):
        result = recommend_action("EXCESS", self._with_negative_midday(), 80.0, 20.0)
        assert result["start_time"] is not None
        assert result["end_time"] is not None

    def test_excess_no_negative_prices_returns_monitor(self):
        result = recommend_action("EXCESS", self._flat(), 80.0, 20.0)
        assert result["action"] == "MONITOR"

    def test_deficit_with_sufficient_saving_returns_force_charge(self):
        # soc=10%, charge_kwh=(0.9-0.1)*15=12 kWh
        # saving = 12 * (200-20)/1000 = 2.16 > 1.00
        result = recommend_action("DEFICIT", self._cheap_overnight(), 10.0, 5.0)
        assert result["action"] == "FORCE_CHARGE"
        assert result["estimated_saving"] > 1.00

    def test_force_charge_target_soc_is_90(self):
        result = recommend_action("DEFICIT", self._cheap_overnight(), 10.0, 5.0)
        assert result["target_soc"] == 90

    def test_deficit_flat_prices_returns_no_action(self):
        # No price differential → saving = 0 ≤ 1.00
        result = recommend_action("DEFICIT", self._flat(), 10.0, 5.0)
        assert result["action"] == "NO_ACTION"

    def test_rationale_is_nonempty_string(self):
        result = recommend_action("DEFICIT", self._cheap_overnight(), 10.0, 5.0)
        assert isinstance(result["rationale"], str) and len(result["rationale"]) > 0


# ── format_recommendation ────────────────────────────────────────────────────

class TestFormatRecommendation:
    def _force_charge_result(self):
        base = datetime(2025, 1, 15, tzinfo=BRUSSELS)
        return {
            "action": "FORCE_CHARGE",
            "start_time": base.replace(hour=1),
            "end_time": base.replace(hour=4),
            "target_soc": 90,
            "rationale": "Deficit scenario. Charge 12.0 kWh overnight.",
            "estimated_saving": 1.40,
        }

    def _no_action_result(self):
        return {
            "action": "NO_ACTION",
            "start_time": None,
            "end_time": None,
            "target_soc": None,
            "rationale": "No action required.",
            "estimated_saving": 0.0,
        }

    def test_contains_header_and_footer(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, 32.0, 20.3)
        assert "--- Charging recommendation ---" in text
        assert "-------------------------------" in text

    def test_contains_scenario_and_action(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, 32.0, 20.3)
        assert "DEFICIT" in text
        assert "FORCE_CHARGE" in text

    def test_contains_window(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, 32.0, 20.3)
        assert "01:00" in text
        assert "04:00" in text

    def test_contains_target_soc(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, 32.0, 20.3)
        assert "90%" in text

    def test_contains_saving(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, 32.0, 20.3)
        assert "1.40" in text

    def test_contains_data_lines(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, 32.0, 20.3)
        assert "12.5 kWh" in text
        assert "32%" in text
        assert "20.3 kWh" in text

    def test_none_forecast_shows_unavailable(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", None, 32.0, 20.3)
        assert "unavailable" in text

    def test_none_soc_shows_unavailable(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, None, 20.3)
        assert "unavailable" in text

    def test_none_consumption_shows_unavailable(self):
        text = format_recommendation(self._force_charge_result(), "DEFICIT", 12.5, 32.0, None)
        assert "unavailable" in text

    def test_no_action_no_window_lines(self):
        text = format_recommendation(self._no_action_result(), None, None, None, None)
        assert "NO_ACTION" in text
        assert "Window:" not in text
        assert "Target:" not in text

    def test_no_action_no_scenario_line_when_none(self):
        text = format_recommendation(self._no_action_result(), None, None, None, None)
        assert "Scenario:" not in text


# ── prepare_email ────────────────────────────────────────────────────────────

class TestPrepareEmail:
    def _patch_all(self, fixture, forecast=12.5, soc=32.0, avg_consumption=20.3):
        return (
            patch("lambda_function.fetch_prices", return_value=fixture),
            patch("lambda_function.fetch_solar_forecast", return_value=forecast),
            patch("lambda_function._fetch_fusionsolar_data", return_value={
                "soc": soc, "median_consumption": avg_consumption
            }),
        )

    def test_subject_has_action_prefix(self, winter_data):
        p1, p2, p3 = self._patch_all(winter_data)
        with p1, p2, p3:
            _, subject, _ = prepare_email()
        valid = ("NO_ACTION", "FORCE_CHARGE", "FORCE_DISCHARGE", "MONITOR")
        assert any(subject.startswith(a) for a in valid)

    def test_subject_contains_date(self, winter_data):
        p1, p2, p3 = self._patch_all(winter_data)
        with p1, p2, p3:
            date, subject, _ = prepare_email()
        assert date in subject

    def test_body_contains_recommendation_block(self, winter_data):
        p1, p2, p3 = self._patch_all(winter_data)
        with p1, p2, p3:
            _, _, body = prepare_email()
        assert "--- Charging recommendation ---" in body

    def test_recommendation_before_price_table(self, winter_data):
        p1, p2, p3 = self._patch_all(winter_data)
        with p1, p2, p3:
            _, _, body = prepare_email()
        rec_pos = body.find("--- Charging recommendation ---")
        table_pos = body.find("Hour (CET)")
        assert rec_pos < table_pos

    def test_body_contains_price_table(self, winter_data):
        p1, p2, p3 = self._patch_all(winter_data)
        with p1, p2, p3:
            _, _, body = prepare_email()
        assert "Hour (CET)" in body
        assert "2025-01-15" in body

    def test_fallback_all_data_missing_no_crash(self, winter_data):
        p1, p2, p3 = self._patch_all(winter_data, forecast=None, soc=None, avg_consumption=None)
        with p1, p2, p3:
            _, subject, body = prepare_email()
        assert subject.startswith("NO_ACTION")
        assert "Hour (CET)" in body
        assert "--- Charging recommendation ---" in body

    def test_fallback_partial_data_no_crash(self, winter_data):
        p1, p2, p3 = self._patch_all(winter_data, forecast=12.5, soc=None, avg_consumption=None)
        with p1, p2, p3:
            _, subject, body = prepare_email()
        assert subject.startswith("NO_ACTION")
        assert "Hour (CET)" in body


# ── simulate_day ─────────────────────────────────────────────────────────────

class TestSimulateDay:
    def _flat_prices(self, value, neg_hours=None, neg_value=-100.0):
        prices = {h: value for h in range(24)}
        for h in (neg_hours or []):
            prices[h] = neg_value
        return prices

    def test_no_solar_no_feed(self):
        prices = self._flat_prices(100.0, neg_hours=[12])
        solar = {h: 0.0 for h in range(24)}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.5, start_soc_pct=50.0)
        assert result["total_feed_kwh"] == pytest.approx(0.0)
        assert result["neg_feed_cost_eur"] == pytest.approx(0.0)

    def test_full_battery_surplus_feeds(self):
        # Battery at 90% (full), surplus 4 kWh at hour 12 (price -200).
        # No baseline draw to keep battery at full until hour 12.
        prices = self._flat_prices(50.0, neg_hours=[12], neg_value=-200.0)
        solar = {12: 4.0}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.0, start_soc_pct=90.0)
        assert result["feeds_per_hour"][12] == pytest.approx(4.0)
        assert result["neg_feed_cost_eur"] == pytest.approx(0.40)

    def test_empty_battery_absorbs_surplus(self):
        # Battery at 15% (min), surplus 3 kWh at hour 12, capped by MAX_CHARGE_RATE=5
        # Battery has 11.25 kWh of room → absorbs all 3, no feed
        prices = self._flat_prices(50.0, neg_hours=[12], neg_value=-200.0)
        solar = {12: 3.5}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.5, start_soc_pct=15.0)
        assert result["feeds_per_hour"][12] == pytest.approx(0.0)
        assert result["neg_feed_cost_eur"] == pytest.approx(0.0)

    def test_charge_rate_caps_absorption(self):
        # 10 kWh surplus in one hour, but battery can only absorb 5 → 5 feeds
        prices = self._flat_prices(50.0, neg_hours=[12], neg_value=-200.0)
        solar = {12: 10.5}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.5, start_soc_pct=15.0)
        assert result["feeds_per_hour"][12] == pytest.approx(5.0)
        assert result["neg_feed_cost_eur"] == pytest.approx(5.0 * 200 * 0.5 / 1000)

    def test_battery_fills_then_overflows(self):
        # Battery at 80% (12 kWh), max 90% (13.5 kWh) → 1.5 kWh room
        # Surplus 3 kWh/hr at hours 11+12. Hour 11: absorb 1.5, feed 1.5. Hour 12: feed 3.
        prices = self._flat_prices(50.0, neg_hours=[11, 12], neg_value=-100.0)
        solar = {11: 3.0, 12: 3.0}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.0, start_soc_pct=80.0)
        assert result["feeds_per_hour"][11] == pytest.approx(1.5)
        assert result["feeds_per_hour"][12] == pytest.approx(3.0)

    def test_phev_consumption_reduces_feed(self):
        # 4 kWh surplus + 6 kWh PHEV draw → net -2 kWh need, no feed
        prices = self._flat_prices(50.0, neg_hours=[12], neg_value=-200.0)
        solar = {12: 4.5}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.5,
                              start_soc_pct=90.0, phev_hourly={12: 6.0})
        assert result["feeds_per_hour"][12] == pytest.approx(0.0)

    def test_force_discharge_creates_headroom(self):
        # Battery 90%, surplus 4 kWh at hour 12 (price -200).
        # Without force-discharge: feed 4 kWh → cost €0.40
        # With force-discharge at 9: battery drops, has room to absorb hour-12 solar.
        prices = self._flat_prices(80.0, neg_hours=[12], neg_value=-200.0)
        solar = {12: 4.0}
        baseline = simulate_day(prices, solar, baseline_hourly_kwh=0.0, start_soc_pct=90.0)
        with_fd = simulate_day(prices, solar, baseline_hourly_kwh=0.0, start_soc_pct=90.0,
                                force_discharge_hours={9})
        assert with_fd["neg_feed_cost_eur"] < baseline["neg_feed_cost_eur"]

    def test_pos_feed_revenue(self):
        # Surplus 4 kWh at hour 12 (positive 80 €/MWh) → revenue 4 × 80 × 0.5 / 1000 = €0.16
        prices = self._flat_prices(80.0)
        solar = {12: 4.0}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.0, start_soc_pct=90.0)
        assert result["pos_feed_revenue_eur"] == pytest.approx(0.16)

    def test_returns_required_keys(self):
        prices = self._flat_prices(50.0)
        solar = {h: 0.0 for h in range(24)}
        result = simulate_day(prices, solar, baseline_hourly_kwh=0.5, start_soc_pct=50.0)
        for key in ("feeds_per_hour", "total_feed_kwh", "neg_feed_kwh",
                    "neg_feed_cost_eur", "pos_feed_revenue_eur", "end_soc_pct"):
            assert key in result


# ── recommend ────────────────────────────────────────────────────────────────

class TestRecommend:
    def _flat_prices(self, value, neg_hours=None, neg_value=-100.0):
        prices = {h: value for h in range(24)}
        for h in (neg_hours or []):
            prices[h] = neg_value
        return prices

    def _no_solar(self):
        return {h: 0.0 for h in range(24)}

    def test_none_prices_returns_no_action(self):
        result = recommend(None, self._no_solar(), 9.0, 50.0)
        assert result["action"] == "NO_ACTION"

    def test_none_solar_returns_no_action(self):
        result = recommend(self._flat_prices(50.0), None, 9.0, 50.0)
        assert result["action"] == "NO_ACTION"

    def test_none_consumption_returns_no_action(self):
        result = recommend(self._flat_prices(50.0), self._no_solar(), None, 50.0)
        assert result["action"] == "NO_ACTION"

    def test_none_soc_returns_no_action(self):
        result = recommend(self._flat_prices(50.0), self._no_solar(), 9.0, None)
        assert result["action"] == "NO_ACTION"

    def test_no_negative_prices_returns_no_action(self):
        result = recommend(self._flat_prices(100.0), self._no_solar(), 9.0, 50.0)
        assert result["action"] == "NO_ACTION"

    def test_may25_class_small_loss_returns_no_action(self):
        # -12 €/MWh × ~4 kWh feed × 0.5 ≈ €0.024 — far below €5
        prices = self._flat_prices(50.0, neg_hours=[12], neg_value=-12.0)
        solar = {12: 4.5}
        result = recommend(prices, solar, 0.5 * 24, 90.0)
        assert result["action"] == "NO_ACTION"
        assert result["baseline_cost_eur"] < MIN_NET_BENEFIT_EUR

    def test_may1_class_big_loss_emits_recommendations(self):
        # -500 €/MWh × 30 kWh = €7.50 baseline cost
        prices = self._flat_prices(80.0, neg_hours=[10, 11, 12, 13, 14, 15], neg_value=-500.0)
        solar = {h: 5.0 for h in (10, 11, 12, 13, 14, 15)}
        result = recommend(prices, solar, baseline_consumption=0.0, current_soc=90.0)
        assert result["action"] == "RECOMMEND"
        assert result["baseline_cost_eur"] > MIN_NET_BENEFIT_EUR
        assert len(result["recommendations"]) >= 1
        assert result["combined_benefit_eur"] >= MIN_NET_BENEFIT_EUR

    def test_recommendation_includes_phev_lever(self):
        prices = self._flat_prices(80.0, neg_hours=[10, 11, 12, 13, 14, 15], neg_value=-500.0)
        solar = {h: 5.0 for h in (10, 11, 12, 13, 14, 15)}
        result = recommend(prices, solar, baseline_consumption=0.0, current_soc=90.0)
        types = {r["type"] for r in result["recommendations"]}
        assert "PHEV_PLUGIN" in types

    def test_recommendation_includes_force_discharge(self):
        prices = self._flat_prices(80.0, neg_hours=[10, 11, 12, 13, 14, 15], neg_value=-500.0)
        solar = {h: 5.0 for h in (10, 11, 12, 13, 14, 15)}
        result = recommend(prices, solar, baseline_consumption=0.0, current_soc=90.0)
        types = {r["type"] for r in result["recommendations"]}
        assert "FORCE_DISCHARGE" in types

    def test_returns_required_keys(self):
        result = recommend(self._flat_prices(50.0), self._no_solar(), 9.0, 50.0)
        for key in ("action", "baseline_cost_eur", "negative_hours",
                    "recommendations", "combined_benefit_eur"):
            assert key in result
