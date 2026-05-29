#!/usr/bin/env python3
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

    marked = mark_cheapest(parse_entries(fetch_prices(target_date)))
    forecast_kwh = fetch_solar_forecast(target_date)

    forecast_line = (
        f"Solar forecast: {forecast_kwh:.1f} kWh\n" if forecast_kwh is not None
        else "Solar forecast: unavailable\n"
    )

    body = explanatory_text() + forecast_line + "\n" + summarize_hours(marked) + "\n" + format_table(marked)
    return target_date, f"Nordpool prices for {target_date}", body


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