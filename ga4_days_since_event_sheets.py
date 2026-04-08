#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GA4 — Days-Since-Event Audit → Google Sheet (No Wrike)
-----------------------------------------------------
What this script does
---------------------
1) Audits GA4 properties for selected events.
2) Writes results to Google Sheets tab in format:
     A) GA4 Property ID
     B) Event Name
     C) Days since last event
     D) Timestamp (run time)

New in this version
-------------------
• --no-data-as-zero => writes 0 instead of >lookback_days for NO_DATA cases
• --write-all       => writes ALL requested events for each property (not just exceeded)
• Default event set: click_to_call, appointment_complete, contact_us_complete, lease_application_complete
• No Wrike logic at all
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from typing import List, Optional, Dict, Any

# === Google Sheets support ===
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- Google Auth & GA4 Data API imports ---
try:
    from google.analytics.data_v1 import AnalyticsDataClient
    from google.analytics.data_v1.types import (
        RunReportRequest, DateRange, Dimension, Metric,
        Filter, FilterExpression, OrderBy
    )
    GA4_API_VERSION = "v1"
except Exception:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient as AnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric,
        Filter, FilterExpression, OrderBy
    )
    GA4_API_VERSION = "v1beta"

from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2 import service_account
from google.api_core.exceptions import GoogleAPIError, ResourceExhausted, RetryError, PermissionDenied

# ----------------------------- Defaults & Settings -----------------------------
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
DEFAULT_LOOKBACK_DAYS = 730
DEFAULT_TOKEN_FILE = "token_ga4.json"
DEFAULT_CLIENT_SECRET_FILE = "client_secret.json"

# Default monitored events & thresholds (requested set)
DEFAULT_EVENTS = [
    {"name": "click_to_call", "threshold_days": 3, "severity": "MEDIUM"},
    {"name": "appointment_complete", "threshold_days": 7, "severity": "MEDIUM"},
    {"name": "contact_us_complete", "threshold_days": 5, "severity": "MEDIUM"},
    {"name": "lease_application_complete", "threshold_days": 7, "severity": "MEDIUM"},
]

# Backoff
MAX_RETRIES = 5
BASE_BACKOFF = 1.0
MAX_BACKOFF = 16.0

# ---- Sheets defaults (override via CLI as needed) ----
DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/15H5pO4yRWlRUIX6Fu81OiwxZuM-MSOQWWIHmKTdgk38"
DEFAULT_SHEET_TAB = "GA4 Events"
DEFAULT_WRITE_MODE = "overwrite"  # overwrite or append
DEFAULT_SA_PATH = "service_account.json"

# ----------------------------- Utilities -----------------------------
def load_property_ids(path: str) -> List[str]:
    props: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                props.append(s)
    return props


def load_property_ids_from_sheet(sheet_url: str, sa_path: str, tab_name: str = "US Properties", col_index: int = 7) -> List[str]:
    """Read GA4 property IDs from a Google Sheet column (default: col G = index 7 on 'US Properties' tab)."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(sa_path, scope)
    client = gspread.authorize(creds)
    sh = client.open_by_url(sheet_url)
    ws = sh.worksheet(tab_name)
    col_values = ws.col_values(col_index)  # 1-based; col G = 7
    props: List[str] = []
    for cell in col_values[1:]:  # skip header row
        val = str(cell).strip()
        if val and val.isdigit():
            props.append(val)
    print(f"Loaded {len(props)} property IDs from '{tab_name}' column G.")
    return props


def parse_events_file(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip() for p in s.split(",")]
            if len(parts) < 2:
                continue
            name = parts[0]
            try:
                thr = int(parts[1])
            except Exception:
                continue
            sev = parts[2].upper() if len(parts) >= 3 and parts[2].strip() else "MEDIUM"
            out.append({"name": name, "threshold_days": thr, "severity": sev})
    return out


def get_credentials_oauth(client_secret_file: str, token_file: str) -> UserCredentials:
    creds: Optional[UserCredentials] = None
    if os.path.exists(token_file):
        creds = UserCredentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def get_ga4_client(
    service_account_json: Optional[str] = None,
    client_secret_file: str = DEFAULT_CLIENT_SECRET_FILE,
    token_file: str = DEFAULT_TOKEN_FILE,
) -> AnalyticsDataClient:
    sa_path = service_account_json or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if sa_path:
        if not os.path.exists(sa_path):
            raise FileNotFoundError(
                f"You passed --service-account-json, but file not found: {sa_path}\n"
                f"If you intended to use OAuth, remove --service-account-json and ensure {client_secret_file} exists."
            )
        sa_creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        return AnalyticsDataClient(credentials=sa_creds)

    if not os.path.exists(client_secret_file):
        raise FileNotFoundError(
            f"OAuth client secret not found at {client_secret_file}. "
            f"Either provide --service-account-json or place your OAuth client at this path."
        )
    user_creds = get_credentials_oauth(client_secret_file, token_file)
    return AnalyticsDataClient(credentials=user_creds)


def run_report_with_backoff(client: AnalyticsDataClient, request: RunReportRequest):
    delay = BASE_BACKOFF
    for _attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.run_report(request)
        except (ResourceExhausted, RetryError, GoogleAPIError) as e:
            msg = (str(e) or "").lower()
            is_quota = ("quota" in msg) or ("exhausted" in msg) or ("429" in msg) or ("rate" in msg)
            if is_quota and _attempt < MAX_RETRIES:
                time.sleep(min(delay, MAX_BACKOFF))
                delay *= 2
                continue
            raise


def last_event_date_for_property(
    client: AnalyticsDataClient,
    property_id: str,
    event_name: str,
    lookback_days: int,
) -> Optional[dt.date]:
    """Return the last event date for a property+event or None if no data."""
    date_range = DateRange(start_date=f"{lookback_days}daysAgo", end_date="today")
    string_filter = Filter.StringFilter(value=event_name)
    try:
        string_filter.match_type = Filter.StringFilter.MatchType.EXACT
    except Exception:
        pass
    dim_filter = FilterExpression(filter=Filter(field_name="eventName", string_filter=string_filter))
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[date_range],
        dimension_filter=dim_filter,
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"), desc=True)],
        limit=1,
    )
    resp = run_report_with_backoff(client, request)
    if not resp.rows:
        return None
    last_date_str = resp.rows[0].dimension_values[0].value  # 'YYYYMMDD'
    try:
        last_date = dt.datetime.strptime(last_date_str, "%Y%m%d").date()
    except Exception:
        return None
    event_count_val = int(resp.rows[0].metric_values[0].value or "0")
    if event_count_val <= 0:
        return None
    return last_date

# ----------------------------- Google Sheets helpers -----------------------------
def _gs_load_worksheet(sheet_url: str, tab_name: str, sa_path: str):
    """Open by URL and return the worksheet; create tab if needed."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(sa_path, scope)
    client = gspread.authorize(creds)
    sh = client.open_by_url(sheet_url)
    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=10)
    return ws


def _gs_ensure_headers(ws):
    expected = ["GA4 Property ID", "Event Name", "Days since last event", "Timestamp"]
    current = ws.row_values(1)
    if current[:4] != expected:
        ws.update(range_name="A1:D1", values=[expected])


def _gs_first_empty_row(ws) -> int:
    used = len(ws.col_values(1))  # includes header
    return max(used + 1, 2)


def _gs_write_rows(ws, rows: list, write_mode: str):
    """Write rows shaped as [property_id, event_name, days_since, timestamp]."""
    if write_mode == "overwrite":
        ws.batch_clear(["A2:D"])  # clear under header

    if not rows:
        return 0

    start_row = _gs_first_empty_row(ws)
    end_row = start_row + len(rows) - 1
    rng = f"A{start_row}:D{end_row}"
    ws.update(range_name=rng, values=rows, value_input_option="USER_ENTERED")
    return len(rows)

# ----------------------------- Main -----------------------------
def main():
    parser = argparse.ArgumentParser(
        description="GA4 days-since-event audit → Google Sheet (no Wrike)."
    )

    # Targets
    parser.add_argument("--property-id", help="GA4 property ID (e.g., 123456789).")
    parser.add_argument("--properties-file", help="Text file with GA4 property IDs, one per line.")
    parser.add_argument("--properties-sheet", action="store_true",
                        help="Load property IDs from column G of the 'US Properties' tab in --sheet-url.")

    # Events
    parser.add_argument("--events-file", help="CSV-like list (event,days[,severity]).")

    # Time window
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help="How many days back to search (default: 730).")

    # Auth (GA4)
    parser.add_argument("--service-account-json", help="Path to a Service Account JSON key.")
    parser.add_argument("--client-secret-file", default=DEFAULT_CLIENT_SECRET_FILE,
                        help="OAuth client secret JSON (if not using Service Account).")
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                        help="Path to store OAuth token (default: token_ga4.json).")

    # Output (optional CSV on disk)
    parser.add_argument("--summary-csv", help="Optional: write CSV to this path (respects --write-all).")
    parser.add_argument("--include-no-data", action="store_true",
                        help="Include events with no occurrences in the lookback window, as >lookback_days (or 0 with --no-data-as-zero).")

    # Google Sheets output
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_URL,
                        help="Full URL of the Google Sheet (default: your tracker).")
    parser.add_argument("--sheet-tab", default=DEFAULT_SHEET_TAB,
                        help="Tab name to write (default: 'GA4 Events').")
    parser.add_argument("--sheet-write-mode", choices=["append", "overwrite"], default=DEFAULT_WRITE_MODE,
                        help="append or overwrite rows under the header (default: overwrite).")
    parser.add_argument("--sheet-service-account", default=DEFAULT_SA_PATH,
                        help="Path to service account JSON for Sheets (default: service_account.json).")

    # New behavior switches
    parser.add_argument("--no-data-as-zero", action="store_true",
                        help="Write 0 instead of >lookback_days for NO_DATA cases.")
    parser.add_argument("--write-all", action="store_true",
                        help="Write ALL events for each property (even if not exceeded and even if NO_DATA).")

    args = parser.parse_args()

    # Build property list
    props: List[str] = []
    if args.property_id:
        props.append(args.property_id)
    if args.properties_file:
        props.extend(load_property_ids(args.properties_file))
    if args.properties_sheet:
        props.extend(load_property_ids_from_sheet(
            sheet_url=args.sheet_url,
            sa_path=args.sheet_service_account,
            tab_name="US Properties",
            col_index=7,
        ))
    if not props:
        print("ERROR: Provide --property-id, --properties-file, or --properties-sheet.")
        sys.exit(2)

    # Events to check
    events = DEFAULT_EVENTS.copy()
    if args.events_file:
        try:
            parsed = parse_events_file(args.events_file)
            if parsed:
                events = parsed
        except Exception as e:
            print(f"WARNING: Failed to parse --events-file ({e}); using default events.")

    # Auth (GA4)
    try:
        client = get_ga4_client(
            service_account_json=args.service_account_json,
            client_secret_file=args.client_secret_file,
            token_file=args.token_file,
        )
    except Exception as e:
        print(f"Auth error: {e}")
        sys.exit(1)

    now_ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_local = dt.date.today()

    print("\n=== GA4 — Days-Since-Event Audit ===")
    print(f"Lookback: {args.lookback_days} days  API: {GA4_API_VERSION}")
    print("Events & thresholds:")
    for ev in events:
        sev = f", {ev.get('severity','MEDIUM')}" if ev.get("severity") else ""
        print(f" - {ev['name']}: {ev['threshold_days']} day(s){sev}")
    print("")

    # We'll accumulate two lists:
    # 1) output_rows_for_sheet -> list of [pid, event, days_since, timestamp]
    # 2) csv_rows -> list of dicts for optional CSV (mirrors previous fields + timestamp)
    output_rows_for_sheet: List[List[Any]] = []
    csv_rows: List[Dict[str, Any]] = []

    for pid in props:
        pid = pid.strip()
        if not pid:
            continue
        for ev in events:
            name = ev["name"]
            thr = int(ev["threshold_days"])
            sev = (ev.get("severity") or "").upper()
            try:
                last_date = last_event_date_for_property(
                    client=client,
                    property_id=pid,
                    event_name=name,
                    lookback_days=args.lookback_days,
                )

                # Compute days_since; handle NO_DATA
                if last_date is None:
                    # If write-all is enabled, we include NO_DATA rows regardless of --include-no-data
                    if args.write_all or args.include_no_data:
                        display_days = f">{args.lookback_days}"
                        write_days = 0 if args.no_data_as_zero else display_days
                        print(f"{pid} {name} {display_days} (threshold={thr}{', HIGH' if sev=='HIGH' else ''})")
                        output_rows_for_sheet.append([pid, name, write_days, now_ts])
                        csv_rows.append({
                            "property_id": pid,
                            "event_name": name,
                            "threshold_days": thr,
                            "last_date": "",
                            "days_since": write_days,
                            "severity": sev or "MEDIUM",
                            "note": "NO_DATA",
                            "timestamp": now_ts,
                        })
                    # else: skip NO_DATA rows when not requested
                    continue

                # We have data: compute days_since normally
                days_since = (today_local - last_date).days
                if days_since < 0:
                    days_since = 0

                exceeded = days_since > thr
                if exceeded:
                    print(f"{pid} {name} {days_since} (threshold={thr}{', HIGH' if sev=='HIGH' else ''})")

                # Decide whether to write the row:
                # - If write_all: always write
                # - Else: only write if exceeded
                if args.write_all or exceeded:
                    output_rows_for_sheet.append([pid, name, days_since, now_ts])
                    csv_rows.append({
                        "property_id": pid,
                        "event_name": name,
                        "threshold_days": thr,
                        "last_date": last_date.isoformat(),
                        "days_since": days_since,
                        "severity": sev or "MEDIUM",
                        "note": "" if exceeded else "OK",
                        "timestamp": now_ts,
                    })

            except PermissionDenied as e:
                msg = str(e)
                print(f"# PERMISSION {pid} {name}: {msg}")
                break
            except GoogleAPIError as e:
                msg = str(e)
                lower = msg.lower()
                if "permission" in lower or "403" in lower or "insufficient" in lower or "permissiondenied" in lower:
                    print(f"# PERMISSION {pid} {name}: {msg}")
                    break
                else:
                    print(f"# ERROR {pid} {name}: {msg}")
            except Exception as e:
                print(f"# ERROR {pid} {name}: {e}")

    # Optional CSV (write-all respected)
    if args.summary_csv:
        with open(args.summary_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["property_id", "event_name", "last_date", "days_since", "threshold_days", "severity", "note", "timestamp"])
            for r in csv_rows:
                writer.writerow([
                    r["property_id"], r["event_name"], r.get("last_date", ""),
                    r.get("days_since", ""), r["threshold_days"], r.get("severity", ""), r.get("note", ""), r.get("timestamp", "")
                ])
        print(f"\nSaved CSV to: {args.summary_csv}")

    # Write to Google Sheet
    try:
        ws = _gs_load_worksheet(
            sheet_url=args.sheet_url,
            tab_name=args.sheet_tab,
            sa_path=args.sheet_service_account,
        )
        _gs_ensure_headers(ws)

        written = _gs_write_rows(ws, output_rows_for_sheet, args.sheet_write_mode)
        if written > 0:
            print(f"\nWrote {written} row(s) to '{args.sheet_tab}' in your Google Sheet.")
        else:
            print("\nNo rows to write to Google Sheet.")
    except Exception as e:
        print(f"\n# Sheets write skipped due to error: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)
