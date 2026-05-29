#!/usr/bin/env python3
import math
import urllib.request
import json
import os
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo
import boto3
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BRUSSELS = ZoneInfo("Europe/Brussels")

NORDPOOL_URL = (
    "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
    "?date={date}&market=DayAhead&deliveryArea=BE&currency=EUR"
)

HEADERS = {
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/000000000 Safari/537.36"
    ),
}

MAIL_ADDRESS = "we@kielbaey-oliveros.eu"

FORECAST_SOLAR_URL = "https://api.forecast.solar/{key}/estimate/{lat}/{lon}/{dec}/{az}/{kwp}"


class _SessionExpired(Exception):
    pass


def _fusionsolar_login():
    host = os.getenv("FUSIONSOLAR_HOST", "eu5.fusionsolar.huawei.com")
    user = os.getenv("FUSIONSOLAR_USER")
    pwd = os.getenv("FUSIONSOLAR_PASSWORD")
    body = json.dumps({"userName": user, "systemCode": pwd}).encode()
    req = urllib.request.Request(
        f"https://{host}/thirdData/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        if not data.get("success"):
            return None
        return resp.headers.get("xsrf-token")


def _fusionsolar_get_soc(token):
    host = os.getenv("FUSIONSOLAR_HOST", "eu5.fusionsolar.huawei.com")
    device_id = os.getenv("FUSIONSOLAR_DEVICE_ID")
    body = json.dumps({"devIds": device_id, "devTypeId": 39}).encode()
    req = urllib.request.Request(
        f"https://{host}/thirdData/getDevRealKpi",
        data=body,
        headers={"Content-Type": "application/json", "XSRF-TOKEN": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if not data.get("success"):
        if data.get("failCode") in (305, 306, 307):
            raise _SessionExpired()
        return None
    entries = data.get("data") or []
    if not entries:
        return None
    return entries[0].get("dataItemMap", {}).get("battery_soc")


def _fetch_fusionsolar_data(days=7):
    if not all([os.getenv("FUSIONSOLAR_USER"), os.getenv("FUSIONSOLAR_PASSWORD")]):
        return {"soc": None, "avg_consumption": None}
    has_soc = bool(os.getenv("FUSIONSOLAR_DEVICE_ID"))
    has_cons = bool(os.getenv("FUSIONSOLAR_PLANT_ID"))
    def _fetch(token):
        return {
            "soc": _fusionsolar_get_soc(token) if has_soc else None,
            "avg_consumption": _fusionsolar_get_avg_consumption(token, days) if has_cons else None,
        }
    try:
        token = _fusionsolar_login()
        if not token:
            return {"soc": None, "avg_consumption": None}
        try:
            return _fetch(token)
        except _SessionExpired:
            token = _fusionsolar_login()
            return _fetch(token) if token else {"soc": None, "avg_consumption": None}
    except Exception:
        return {"soc": None, "avg_consumption": None}


def fetch_current_soc():
    if not all([os.getenv("FUSIONSOLAR_USER"), os.getenv("FUSIONSOLAR_PASSWORD"), os.getenv("FUSIONSOLAR_DEVICE_ID")]):
        return None
    try:
        token = _fusionsolar_login()
        if not token:
            return None
        try:
            return _fusionsolar_get_soc(token)
        except _SessionExpired:
            token = _fusionsolar_login()
            if not token:
                return None
            return _fusionsolar_get_soc(token)
    except Exception:
        return None


def _fusionsolar_get_avg_consumption(token, days=7):
    host = os.getenv("FUSIONSOLAR_HOST", "eu5.fusionsolar.huawei.com")
    plant_id = os.getenv("FUSIONSOLAR_PLANT_ID")
    today_utc = datetime.now(ZoneInfo("UTC")).replace(hour=0, minute=0, second=0, microsecond=0)
    values = []
    for i in range(1, days + 1):
        collect_time = int((today_utc - timedelta(days=i)).timestamp() * 1000)
        body = json.dumps({"stationCodes": plant_id, "collectTime": collect_time}).encode()
        req = urllib.request.Request(
            f"https://{host}/thirdData/getKpiStationDay",
            data=body,
            headers={"Content-Type": "application/json", "XSRF-TOKEN": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if not data.get("success"):
            if data.get("failCode") in (305, 306, 307):
                raise _SessionExpired()
            continue
        entries = data.get("data") or []
        if not entries:
            continue
        use_power = entries[0].get("dataItemMap", {}).get("use_power")
        if use_power is not None and use_power > 0:
            values.append(use_power)
    return sum(values) / len(values) if values else None


def fetch_avg_daily_consumption(days=7):
    if not all([os.getenv("FUSIONSOLAR_USER"), os.getenv("FUSIONSOLAR_PASSWORD"), os.getenv("FUSIONSOLAR_PLANT_ID")]):
        return None
    try:
        token = _fusionsolar_login()
        if not token:
            return None
        try:
            return _fusionsolar_get_avg_consumption(token, days)
        except _SessionExpired:
            token = _fusionsolar_login()
            if not token:
                return None
            return _fusionsolar_get_avg_consumption(token, days)
    except Exception:
        return None


BATTERY_CAPACITY_KWH = 15
MAX_CHARGE_RATE_KW = 5
MIN_SOC_PCT = 15
TARGET_CHARGE_SOC_PCT = 90
MIN_SAVING_EUR = 1.00

_OVERNIGHT_HOURS = set(range(8)) | {22, 23}


def classify_scenario(forecast_kwh, current_soc, avg_consumption):
    current_stored = (current_soc / 100) * BATTERY_CAPACITY_KWH
    solar_surplus = max(0.0, forecast_kwh - avg_consumption)
    if current_stored + solar_surplus > BATTERY_CAPACITY_KWH:
        return "EXCESS"
    if current_stored + forecast_kwh < avg_consumption:
        return "DEFICIT"
    return "BALANCED"


def find_negative_price_window(prices):
    block_start = None
    last_h = None
    for h, p in prices:
        if p < 0:
            if block_start is None:
                block_start = h
        else:
            if block_start is not None:
                return (block_start, h)
        last_h = h
    if block_start is not None:
        return (block_start, last_h + timedelta(hours=1))
    return None


def find_cheapest_overnight_window(prices, charge_kwh):
    overnight = [(h, p) for h, p in prices if h.hour in _OVERNIGHT_HOURS]
    late = [(h, p) for h, p in overnight if h.hour >= 22]
    early = [(h, p) for h, p in overnight if h.hour < 8]
    ordered = late + early

    window_size = max(1, math.ceil(charge_kwh / MAX_CHARGE_RATE_KW))
    best_idx = 0
    best_avg = float("inf")
    for i in range(len(ordered) - window_size + 1):
        avg = sum(p for _, p in ordered[i:i + window_size]) / window_size
        if avg < best_avg:
            best_avg = avg
            best_idx = i

    start = ordered[best_idx][0]
    return (start, start + timedelta(hours=window_size))


def recommend_action(scenario, prices, current_soc, forecast_kwh):
    _no_action = {
        "action": "NO_ACTION",
        "start_time": None,
        "end_time": None,
        "target_soc": None,
        "rationale": "No action required.",
        "estimated_saving": 0.0,
    }

    if prices is None or current_soc is None or forecast_kwh is None:
        return {**_no_action, "rationale": "Insufficient data."}

    if scenario == "EXCESS":
        neg = find_negative_price_window(prices)
        if neg:
            discharge_kwh = (current_soc / 100 - MIN_SOC_PCT / 100) * BATTERY_CAPACITY_KWH
            duration_h = discharge_kwh / MAX_CHARGE_RATE_KW
            start_time = neg[0] - timedelta(hours=duration_h)
            return {
                "action": "FORCE_DISCHARGE",
                "start_time": start_time,
                "end_time": neg[1],
                "target_soc": MIN_SOC_PCT,
                "rationale": (
                    f"Battery overflow likely. Discharge {discharge_kwh:.1f} kWh "
                    f"before negative price window at {neg[0].strftime('%H:%M')}."
                ),
                "estimated_saving": 0.0,
            }
        return {**_no_action, "action": "MONITOR", "rationale": "Battery likely to overflow; no negative prices detected."}

    if scenario == "DEFICIT":
        charge_kwh = (TARGET_CHARGE_SOC_PCT / 100 - current_soc / 100) * BATTERY_CAPACITY_KWH
        if charge_kwh <= 0:
            return _no_action

        win_start, win_end = find_cheapest_overnight_window(prices, charge_kwh)

        price_by_hour = {h.hour: p for h, p in prices}
        win_prices = []
        h = win_start
        while h < win_end:
            if h.hour in price_by_hour:
                win_prices.append(price_by_hour[h.hour])
            h += timedelta(hours=1)
        avg_win = sum(win_prices) / len(win_prices) if win_prices else 0.0

        daytime = [p for h, p in prices if h.hour not in _OVERNIGHT_HOURS]
        avg_day = sum(daytime) / len(daytime) if daytime else 0.0

        saving = round(charge_kwh * (avg_day - avg_win) / 1000, 2)
        if saving > MIN_SAVING_EUR:
            return {
                "action": "FORCE_CHARGE",
                "start_time": win_start,
                "end_time": win_end,
                "target_soc": TARGET_CHARGE_SOC_PCT,
                "rationale": (
                    f"Deficit scenario. Charge {charge_kwh:.1f} kWh overnight "
                    f"({win_start.strftime('%H:%M')}–{win_end.strftime('%H:%M')}). "
                    f"Window avg {avg_win:.0f} vs daytime avg {avg_day:.0f} €/MWh."
                ),
                "estimated_saving": saving,
            }

    return _no_action


def format_recommendation(action_result, scenario, forecast_kwh, soc, avg_consumption):
    action = action_result["action"]
    lines = ["--- Charging recommendation ---"]
    if scenario is not None:
        lines.append(f"Scenario:  {scenario}")
    lines.append(f"Action:    {action}")
    if action_result["start_time"] is not None:
        start = action_result["start_time"].strftime("%H:%M")
        end = action_result["end_time"].strftime("%H:%M")
        lines.append(f"Window:    {start}–{end} CET")
    if action_result["target_soc"] is not None:
        lines.append(f"Target:    {action_result['target_soc']}% SOC")
    if action_result["estimated_saving"] > 0:
        lines.append(f"Saving:    ~€{action_result['estimated_saving']:.2f}")
    lines.append(f"Rationale: {action_result['rationale']}")
    lines.append("")
    forecast_str = f"{forecast_kwh:.1f} kWh" if forecast_kwh is not None else "unavailable"
    soc_str = f"{soc:.0f}%" if soc is not None else "unavailable"
    consumption_str = f"{avg_consumption:.1f} kWh/day" if avg_consumption is not None else "unavailable"
    lines.append(f"Solar forecast:       {forecast_str}")
    lines.append(f"Battery SOC now:      {soc_str}")
    lines.append(f"Avg consumption (7d): {consumption_str}")
    lines.append("-------------------------------")
    return "\n".join(lines)


def fetch_solar_forecast(date_str):
    key = os.getenv("FORECAST_SOLAR_API_KEY")
    lat = os.getenv("SOLAR_LAT")
    lon = os.getenv("SOLAR_LON")
    dec = os.getenv("SOLAR_DECLINATION")
    az  = os.getenv("SOLAR_AZIMUTH")
    kwp = os.getenv("SOLAR_KWP")

    if not all([key, lat, lon, dec, az, kwp]):
        return None

    url = FORECAST_SOLAR_URL.format(key=key, lat=lat, lon=lon, dec=dec, az=az, kwp=kwp)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        wh = data["result"]["watt_hours_day"].get(date_str)
        return wh / 1000 if wh is not None else None
    except Exception:
        return None


def fetch_prices(date_str):
    url = NORDPOOL_URL.format(date=date_str)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
        return json.loads(data)

def parse_entries(data):
    entries = data["multiAreaEntries"]

    hourly = defaultdict(list)

    for e in entries:
        ts_utc = datetime.fromisoformat(e["deliveryStart"].replace("Z", "+00:00"))
        price = e["entryPerArea"]["BE"]

        ts_cet = ts_utc.astimezone(BRUSSELS)

        # Floor to hour
        hour = ts_cet.replace(minute=0, second=0, microsecond=0)

        hourly[hour].append(price)

    # Compute averages
    hourly_avg = []
    for hour, prices in hourly.items():
        avg_price = sum(prices) / len(prices)
        hourly_avg.append((hour, avg_price))

    # Sort by datetime
    hourly_avg.sort(key=lambda x: x[0])
    return hourly_avg

def summarize_hours(marked):
    # marked = list of (hour, price, mark)
    cheapest = [ (h, p) for h, p, m in marked if m == "*" ]
    cheapest_sorted = sorted(cheapest, key=lambda x: x[1])

    # Single absolute cheapest hour
    best_hour, best_price = cheapest_sorted[0]

    # Build a simple window suggestion:
    # consecutive cheap hours = block
    blocks = []
    block = [cheapest[0]]

    for (curr_h, curr_p), (next_h, next_p) in zip(cheapest, cheapest[1:]):
        if (next_h - curr_h).seconds == 3600:
            block.append((next_h, next_p))
        else:
            blocks.append(block)
            block = [(next_h, next_p)]
    blocks.append(block)

    # Pick the longest block
    best_block = max(blocks, key=len)
    start = best_block[0][0].strftime("%H:%M")
    end = (best_block[-1][0] + timedelta(hours=1)).strftime("%H:%M")

    text = []
    text.append("Summary:")
    text.append(f"- Cheapest hour: {best_hour.strftime('%H:%M')} at {best_price:.2f} €/MWh")
    text.append(f"- Cheapest continuous window: {start}–{end} CET")
    text.append("")
    text.append("Recommended charging window: " + f"{start}–{end} CET")
    text.append("")

    return "\n".join(text)

def mark_cheapest(hours, n=12):
    before_21 = [(h, p) for h, p in hours if h.hour < 21]
    sorted_by_price = sorted(before_21, key=lambda x: x[1])
    cutoff = set(sorted_by_price[:n])
    return [(h, p, "*" if (h, p) in cutoff else "") for h, p in hours]

def format_table(marked):
    lines = []
    lines.append("Hour (CET)          Price    Note")
    lines.append("----------------------------------")
    for h, p, mark in marked:
        lines.append(f"{h.strftime('%Y-%m-%d %H:%M')}   {p:7.2f}   {mark}")
    return "\n".join(lines)

def explanatory_text():
    return (
        "Hi,\n\n"
        "These are the electricity day-ahead prices for Belgium.\n"
        "Each row shows the average price for that hour (CET).\n"
        "Hours marked with an asterisk (*) are the cheapest ten hours of the day.\n"
        "Those are usually the best times to charge the home battery.\n\n"
    )

def prepare_email():
    now = datetime.now(BRUSSELS)

    if now.hour < 14:
        target_date = now.strftime("%Y-%m-%d")
    else:
        target_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    hourly = parse_entries(fetch_prices(target_date))
    marked = mark_cheapest(hourly)
    forecast_kwh = fetch_solar_forecast(target_date)
    fusionsolar = _fetch_fusionsolar_data()
    soc = fusionsolar["soc"]
    avg_consumption = fusionsolar["avg_consumption"]

    if forecast_kwh is not None and soc is not None and avg_consumption is not None:
        scenario = classify_scenario(forecast_kwh, soc, avg_consumption)
    else:
        scenario = None

    action_result = recommend_action(scenario, hourly, soc, forecast_kwh)
    action = action_result["action"]
    rec_block = format_recommendation(action_result, scenario, forecast_kwh, soc, avg_consumption)

    subject = f"{action} — Nordpool prices for {target_date}"
    body = explanatory_text() + rec_block + "\n\n" + summarize_hours(marked) + "\n" + format_table(marked)
    return target_date, subject, body


def lambda_handler(event, context):
    target_date, subject, body = prepare_email()

    boto3.client("ses").send_email(
        Source=MAIL_ADDRESS,
        Destination={"ToAddresses": [MAIL_ADDRESS]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"date": target_date, "status": "ok"})
    }


if __name__ == "__main__":
    _, subject, body = prepare_email()
    print(f"Subject: {subject}")
    print(f"To: {MAIL_ADDRESS}")
    print()
    print(body)