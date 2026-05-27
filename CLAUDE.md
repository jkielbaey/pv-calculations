# CLAUDE.md

## What this project does

AWS Lambda function that fetches Nordpool day-ahead electricity prices for Belgium, identifies the cheapest hours for home battery charging, and sends a daily summary email via AWS SES.

## Architecture

Single file: `src/lambda_function.py`. No dependencies beyond the Python standard library and `boto3` (provided by the Lambda runtime).

Key flow in `lambda_handler`:
1. Determines target date: today if before 14:00 CET, tomorrow otherwise (prices are published around 13:00)
2. Fetches hourly prices from the Nordpool API for Belgium (`deliveryArea=BE`)
3. Marks the 12 cheapest hours before 21:00 CET with `*`
4. Emails a plain-text table + summary to `we@kielbaey-oliveros.eu` via SES

## Running locally

```bash
python3 src/lambda_function.py
```

The file has no `if __name__ == "__main__"` block, so invoke functions directly in a REPL or add a test call at the bottom. AWS credentials with SES send permission are required to actually send email.

To test price fetching and formatting without sending email:

```python
from src.lambda_function import fetch_prices, parse_entries, mark_cheapest, format_table, summarize_hours
data = fetch_prices("2025-05-27")
hourly = parse_entries(data)
marked = mark_cheapest(hourly)
print(format_table(marked))
print(summarize_hours(marked))
```

## Deployment

Deployed as an AWS Lambda. The function is triggered on a schedule (EventBridge/CloudWatch Events). No IaC files are present in this repo — deployment is managed externally.
