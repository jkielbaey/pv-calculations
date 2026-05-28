import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lambda_function import parse_entries, mark_cheapest, summarize_hours, format_table

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
