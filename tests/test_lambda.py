import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from lambda_function import (
    parse_entries, mark_cheapest, summarize_hours, format_table,
    fetch_solar_forecast, fetch_current_soc, fetch_avg_daily_consumption,
    _fetch_fusionsolar_data,
    classify_scenario, find_negative_price_window,
    find_cheapest_overnight_window, recommend_action,
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


# ── fetch_avg_daily_consumption ──────────────────────────────────────────────

def _consumption_day_response(use_power=None):
    item_map = {} if use_power is None else {"use_power": use_power}
    return json.dumps({
        "success": True, "failCode": 0,
        "data": [{"stationCode": "NE=TestPlant001", "dataItemMap": item_map}],
    }).encode()


class TestFetchAvgDailyConsumption:
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

    def test_returns_avg_of_7_days(self):
        values = [18.0 + i for i in range(7)]  # 18..24
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok")] + self._day_calls(values)):
                result = fetch_avg_daily_consumption()
        assert result == pytest.approx(sum(values) / 7)

    def test_skips_missing_day(self):
        # 6 days with data, 1 missing (None key in dataItemMap)
        values = [20.0] * 6 + [None]
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok")] + self._day_calls(values)):
                result = fetch_avg_daily_consumption()
        assert result == pytest.approx(20.0)

    def test_returns_none_when_all_missing(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen([(LOGIN_RESPONSE, "tok")] + self._day_calls([None] * 7)):
                result = fetch_avg_daily_consumption()
        assert result is None

    def test_returns_none_when_env_vars_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = fetch_avg_daily_consumption()
        assert result is None

    def test_returns_none_on_network_error(self):
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                result = fetch_avg_daily_consumption()
        assert result is None

    def test_retries_on_session_expired(self):
        expired = json.dumps({"success": False, "failCode": 305}).encode()
        values = [20.0] * 7
        with patch.dict("os.environ", FUSIONSOLAR_ENV):
            with self._mock_urlopen(
                [(LOGIN_RESPONSE, "tok1"), (expired, None),   # login + day1 → expired
                 (LOGIN_RESPONSE, "tok2")] + self._day_calls(values)  # retry from day1
            ):
                result = fetch_avg_daily_consumption()
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
        assert result["avg_consumption"] == pytest.approx(20.0)
        assert mock_urlopen.call_count == 9  # 1 login + 1 SOC + 7 days

    def test_returns_null_dict_when_env_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = _fetch_fusionsolar_data()
        assert result == {"soc": None, "avg_consumption": None}

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
        assert result["avg_consumption"] == pytest.approx(20.0)


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
