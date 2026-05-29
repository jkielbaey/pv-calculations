#!/usr/bin/env python3
import math
import statistics
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
        return {"soc": None, "median_consumption": None}
    has_soc = bool(os.getenv("FUSIONSOLAR_DEVICE_ID"))
    has_cons = bool(os.getenv("FUSIONSOLAR_PLANT_ID"))
    def _fetch(token):
        return {
            "soc": _fusionsolar_get_soc(token) if has_soc else None,
            "median_consumption": _fusionsolar_get_median_consumption(token, days) if has_cons else None,
        }
    try:
        token = _fusionsolar_login()
        if not token:
            return {"soc": None, "median_consumption": None}
        try:
            return _fetch(token)
        except _SessionExpired:
            token = _fusionsolar_login()
            return _fetch(token) if token else {"soc": None, "median_consumption": None}
    except Exception:
        return {"soc": None, "median_consumption": None}


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


def _fusionsolar_get_median_consumption(token, days=7):
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
    return statistics.median(values) if values else None


def fetch_median_daily_consumption(days=7):
    if not all([os.getenv("FUSIONSOLAR_USER"), os.getenv("FUSIONSOLAR_PASSWORD"), os.getenv("FUSIONSOLAR_PLANT_ID")]):
        return None
    try:
        token = _fusionsolar_login()
        if not token:
            return None
        try:
            return _fusionsolar_get_median_consumption(token, days)
        except _SessionExpired:
            token = _fusionsolar_login()
            if not token:
                return None
            return _fusionsolar_get_median_consumption(token, days)
    except Exception:
        return None


BATTERY_CAPACITY_KWH = 15
MAX_CHARGE_RATE_KW = 5
MIN_SOC_PCT = 15
MAX_SOC_PCT = 90
INJECTION_RATE = 0.5
PHEV_CHARGE_RATE_KW = 6
PHEV_FULL_CHARGE_KWH = 18
MIN_NET_BENEFIT_EUR = 5.00


def simulate_day(prices_hourly, solar_hourly, baseline_hourly_kwh, start_soc_pct,
                 phev_hourly=None, force_discharge_hours=None):
    phev_hourly = phev_hourly or {}
    force_discharge_hours = force_discharge_hours or set()

    current_kwh = (start_soc_pct / 100.0) * BATTERY_CAPACITY_KWH
    max_kwh = (MAX_SOC_PCT / 100.0) * BATTERY_CAPACITY_KWH
    min_kwh = (MIN_SOC_PCT / 100.0) * BATTERY_CAPACITY_KWH

    feeds_per_hour = {}
    total_feed = 0.0
    neg_feed_kwh = 0.0
    neg_feed_cost = 0.0
    pos_feed_revenue = 0.0

    for h in sorted(prices_hourly.keys()):
        solar = solar_hourly.get(h, 0.0)
        cons = baseline_hourly_kwh + phev_hourly.get(h, 0.0)
        price = prices_hourly[h]

        if h in force_discharge_hours:
            available = max(0.0, current_kwh - min_kwh)
            battery_out = min(MAX_CHARGE_RATE_KW, available)
            current_kwh -= battery_out
            feed = max(0.0, solar + battery_out - cons)
        else:
            net = solar - cons
            if net > 0:
                room = max(0.0, max_kwh - current_kwh)
                absorbed = min(net, MAX_CHARGE_RATE_KW, room)
                current_kwh += absorbed
                feed = net - absorbed
            else:
                need = -net
                available = max(0.0, current_kwh - min_kwh)
                drawn = min(need, MAX_CHARGE_RATE_KW, available)
                current_kwh -= drawn
                feed = 0.0

        feeds_per_hour[h] = feed
        total_feed += feed
        if price < 0:
            neg_feed_kwh += feed
            neg_feed_cost += feed * abs(price) * INJECTION_RATE / 1000.0
        else:
            pos_feed_revenue += feed * price * INJECTION_RATE / 1000.0

    return {
        "feeds_per_hour": feeds_per_hour,
        "total_feed_kwh": total_feed,
        "neg_feed_kwh": neg_feed_kwh,
        "neg_feed_cost_eur": neg_feed_cost,
        "pos_feed_revenue_eur": pos_feed_revenue,
        "end_soc_pct": current_kwh / BATTERY_CAPACITY_KWH * 100.0,
    }


def recommend(prices_hourly, solar_hourly, baseline_consumption, current_soc):
    no_action = {
        "action": "NO_ACTION",
        "baseline_cost_eur": 0.0,
        "negative_hours": [],
        "avg_negative_price_eur_per_mwh": 0.0,
        "recommendations": [],
        "combined_benefit_eur": 0.0,
        "rationale": "",
    }

    if prices_hourly is None or solar_hourly is None or baseline_consumption is None or current_soc is None:
        return {**no_action, "rationale": "Insufficient data."}

    neg_hours = sorted(h for h, p in prices_hourly.items() if p < 0)
    if not neg_hours:
        return {**no_action, "rationale": "No negative prices forecast."}

    avg_neg_price = sum(prices_hourly[h] for h in neg_hours) / len(neg_hours)

    baseline_hourly = baseline_consumption / 24.0

    baseline = simulate_day(prices_hourly, solar_hourly, baseline_hourly, current_soc)
    baseline_cost = baseline["neg_feed_cost_eur"]
    baseline_net = baseline["neg_feed_cost_eur"] - baseline["pos_feed_revenue_eur"]

    if baseline_cost < MIN_NET_BENEFIT_EUR:
        return {**no_action,
                "baseline_cost_eur": baseline_cost,
                "negative_hours": neg_hours,
                "avg_negative_price_eur_per_mwh": avg_neg_price,
                "rationale": f"Expected feed cost €{baseline_cost:.2f} below €{MIN_NET_BENEFIT_EUR:.0f} threshold."}

    discharge_kwh = max(0.0, (current_soc - MIN_SOC_PCT) / 100.0 * BATTERY_CAPACITY_KWH)
    discharge_duration_h = math.ceil(discharge_kwh / MAX_CHARGE_RATE_KW) if discharge_kwh > 0 else 0
    neg_start = neg_hours[0]
    fd_hours = {h for h in (neg_start - i for i in range(1, discharge_duration_h + 1)) if 0 <= h < 24}

    sim_pd = simulate_day(prices_hourly, solar_hourly, baseline_hourly, current_soc,
                          force_discharge_hours=fd_hours)
    pd_net = sim_pd["neg_feed_cost_eur"] - sim_pd["pos_feed_revenue_eur"]
    pd_benefit = baseline_net - pd_net

    phev_hourly = {}
    remaining = PHEV_FULL_CHARGE_KWH
    for h in neg_hours:
        if remaining <= 0:
            break
        draw = min(PHEV_CHARGE_RATE_KW, remaining)
        phev_hourly[h] = draw
        remaining -= draw

    sim_phev = simulate_day(prices_hourly, solar_hourly, baseline_hourly, current_soc,
                            phev_hourly=phev_hourly)
    phev_net = sim_phev["neg_feed_cost_eur"] - sim_phev["pos_feed_revenue_eur"]
    phev_benefit = baseline_net - phev_net

    sim_both = simulate_day(prices_hourly, solar_hourly, baseline_hourly, current_soc,
                            phev_hourly=phev_hourly, force_discharge_hours=fd_hours)
    both_net = sim_both["neg_feed_cost_eur"] - sim_both["pos_feed_revenue_eur"]
    combined_benefit = baseline_net - both_net

    if combined_benefit < MIN_NET_BENEFIT_EUR:
        return {**no_action,
                "baseline_cost_eur": baseline_cost,
                "negative_hours": neg_hours,
                "avg_negative_price_eur_per_mwh": avg_neg_price,
                "rationale": f"No action combination clears €{MIN_NET_BENEFIT_EUR:.0f} benefit threshold."}

    recommendations = []
    if fd_hours and pd_benefit > 0:
        recommendations.append({
            "type": "FORCE_DISCHARGE",
            "start_hour": min(fd_hours),
            "end_hour": max(fd_hours) + 1,
            "target_soc_pct": MIN_SOC_PCT,
            "kwh": discharge_kwh,
            "benefit_eur": pd_benefit,
        })
    if phev_hourly and phev_benefit > 0:
        recommendations.append({
            "type": "PHEV_PLUGIN",
            "start_hour": min(phev_hourly.keys()),
            "end_hour": max(phev_hourly.keys()) + 1,
            "kwh": sum(phev_hourly.values()),
            "benefit_eur": phev_benefit,
        })

    return {
        "action": "RECOMMEND",
        "baseline_cost_eur": baseline_cost,
        "negative_hours": neg_hours,
        "avg_negative_price_eur_per_mwh": avg_neg_price,
        "recommendations": recommendations,
        "combined_benefit_eur": combined_benefit,
        "rationale": "",
    }


def format_recommendation(result, soc, baseline_consumption, solar_forecast_kwh):
    lines = ["--- Charging recommendation ---"]

    if result["action"] == "RECOMMEND":
        neg = result["negative_hours"]
        if neg:
            start = f"{min(neg):02d}:00"
            end = f"{max(neg) + 1:02d}:00"
            avg = result.get("avg_negative_price_eur_per_mwh", 0.0)
            lines.append(f"Negative-price window: {start}–{end} CET (avg {avg:.0f} €/MWh)")
        lines.append(f"Expected loss without action: €{result['baseline_cost_eur']:.2f}")
        lines.append("")
        lines.append("Recommended:")
        for rec in result["recommendations"]:
            if rec["type"] == "PHEV_PLUGIN":
                lines.append(
                    f"  • Plug in PHEV at {rec['start_hour']:02d}:00 "
                    f"(absorbs ~{rec['kwh']:.0f} kWh, saves ~€{rec['benefit_eur']:.2f})"
                )
            elif rec["type"] == "FORCE_DISCHARGE":
                lines.append(
                    f"  • Force-discharge battery at {rec['start_hour']:02d}:00, "
                    f"target SOC {rec['target_soc_pct']}%"
                )
                lines.append(
                    f"    (frees ~{rec['kwh']:.0f} kWh capacity, saves ~€{rec['benefit_eur']:.2f})"
                )
        lines.append(f"Total expected benefit: ~€{result['combined_benefit_eur']:.2f}")
    else:
        rationale = result.get("rationale") or "No action needed."
        lines.append(rationale)
        if result.get("baseline_cost_eur", 0) > 0:
            lines.append(f"Expected feed cost: €{result['baseline_cost_eur']:.2f}")

    lines.append("")
    forecast_str = f"{solar_forecast_kwh:.1f} kWh" if solar_forecast_kwh is not None else "unavailable"
    soc_str = f"{soc:.0f}%" if soc is not None else "unavailable"
    cons_str = f"{baseline_consumption:.1f} kWh/day" if baseline_consumption is not None else "unavailable"
    lines.append(f"Solar forecast:        {forecast_str}")
    lines.append(f"Battery SOC now:       {soc_str}")
    lines.append(f"Typical daily use (7d): {cons_str}")
    lines.append("-------------------------------")
    return "\n".join(lines)


def fetch_solar_hourly(date_str):
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
        period = data["result"]["watt_hours_period"]
        hourly_wh = defaultdict(float)
        for ts_str, wh in period.items():
            if not ts_str.startswith(date_str):
                continue
            try:
                hour = int(ts_str[11:13])
            except (ValueError, IndexError):
                continue
            hourly_wh[hour] += wh
        if not hourly_wh:
            return None
        return {h: wh / 1000 for h, wh in hourly_wh.items()}
    except Exception:
        return None


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
    # text.append("Recommended charging window: " + f"{start}–{end} CET")
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
        # "Those are usually the best times to charge the home battery.\n\n"
    )

def prepare_email():
    now = datetime.now(BRUSSELS)

    if now.hour < 14:
        target_date = now.strftime("%Y-%m-%d")
    else:
        target_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    hourly = parse_entries(fetch_prices(target_date))
    marked = mark_cheapest(hourly)

    prices_hourly = {h.hour: p for h, p in hourly}
    solar_hourly = fetch_solar_hourly(target_date)
    daily_solar = sum(solar_hourly.values()) if solar_hourly else None

    fusionsolar = _fetch_fusionsolar_data()
    soc = fusionsolar["soc"]
    median_consumption = fusionsolar["median_consumption"]

    result = recommend(prices_hourly, solar_hourly, median_consumption, soc)
    action = result["action"]
    rec_block = format_recommendation(result, soc, median_consumption, daily_solar)

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