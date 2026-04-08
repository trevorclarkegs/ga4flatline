# GA4 Days-Since-Event Audit

Audits GA4 properties for key events and writes results to a Google Sheet.

## What It Does

- Pulls GA4 property IDs from the **US Properties** tab (column G) of the tracker Google Sheet
- Checks each property for the following events:
  - `click_to_call`
  - `appointment_complete`
  - `contact_us_complete`
  - `lease_application_complete`
- Writes results (Property ID, Event Name, Days Since Last Event, Timestamp) to the **GA4 Events** tab

## Requirements

Install dependencies:

```
pip install gspread oauth2client google-analytics-data google-auth-oauthlib
```

You will also need a `service_account.json` file with access to both the GA4 properties and the Google Sheet. **Do not commit this file to GitHub.**

## Run Command

```
python ga4_days_since_event_sheets.py ^
  --properties-sheet ^
  --write-all ^
  --no-data-as-zero ^
  --lookback-days 7 ^
  --sheet-service-account service_account.json
```

## Key Flags

| Flag | Description |
|---|---|
| `--properties-sheet` | Load property IDs from column G of the US Properties tab |
| `--write-all` | Write all events, not just ones that exceeded the threshold |
| `--no-data-as-zero` | Write 0 instead of >lookback_days when no data is found |
| `--lookback-days 7` | How many days back to search for each event |
| `--sheet-service-account` | Path to your service account credentials file |

## Files

| File | Description |
|---|---|
| `ga4_days_since_event_sheets.py` | Main script |
| `service_account.json` | Credentials file — **not tracked in GitHub** |
