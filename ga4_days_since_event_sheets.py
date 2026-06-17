#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GA4 — Fired-Within-Threshold Audit → Google Sheet + Wrike Tickets
------------------------------------------------------------------
1) Loads all properties from 'US Properties' tab (Master ID, Property Name, GA4 ID).
2) Checks: page_view, click_to_call, appointment_complete, contact_us_complete.
3) Writes TRUE/FALSE results to 'GA4 Events' tab.
4) Saves local CSV.
5) Creates 1 Wrike ticket per property with events that had data but exceeded threshold.
   - Skips ticket if all failures are NO_DATA.
   - Skips ticket if one already exists in the last 14 days.
   Title: "{Master ID} - {Property Name} - Missing Events"
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import requests
from typing import List, Optional, Dict, Any

import gspread
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.api_core.exceptions import GoogleAPIError, ResourceExhausted, RetryError, PermissionDenied

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

# ----------------------------- Defaults -----------------------------
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
DEFAULT_LOOKBACK_DAYS = 730
DEFAULT_TOKEN_FILE = "token_ga4.json"
DEFAULT_CLIENT_SECRET_FILE = "client_secret.json"
DEFAULT_SA_PATH = "service_account.json"
DEFAULT_WRIKE_CONFIG = "wrike_config.json"
DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/15H5pO4yRWlRUIX6Fu81OiwxZuM-MSOQWWIHmKTdgk38"
DEFAULT_SHEET_TAB = "GA4 Events"

DEFAULT_EVENTS = [
    {"name": "page_view",            "threshold_days": 1},
    {"name": "click_to_call",        "threshold_days": 3},
    {"name": "appointment_complete", "threshold_days": 7},
    {"name": "contact_us_complete",  "threshold_days": 5},
]

MAX_RETRIES = 5
BASE_BACKOFF = 1.0
MAX_BACKOFF = 16.0
WRIKE_BASE = "https://www.wrike.com/api/v4"

# Custom field values to set on every created ticket
WRIKE_CUSTOM_FIELD_VALUES = {
    "MAP-Project Category": "GTM/Custom Event Tracking",
    "MAP-Team": "ExoEdge",
    "MAP-Timelog Category": "Ad Hoc",
    "MAP-Digital Analytics Request Type": "Data Troubleshoot",
}


# ----------------------------- Property Loading -----------------------------
def load_properties_from_sheet(sheet_url: str, sa_path: str) -> List[Dict[str, str]]:
    """Load Master ID (col A), Property Name (col B), GA4 ID (col G) from 'US Properties'."""
    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.Client(auth=creds)
    ws = client.open_by_url(sheet_url).worksheet("US Properties")
    all_rows = ws.get_all_values()
    props = []
    for row in all_rows[1:]:
        if len(row) < 7:
            continue
        master_id = str(row[0]).strip()
        prop_name = str(row[1]).strip()
        ga4_id = str(row[6]).strip()
        if ga4_id and ga4_id.isdigit():
            props.append({"master_id": master_id, "property_name": prop_name, "ga4_property_id": ga4_id})
    print(f"Loaded {len(props)} properties from 'US Properties' tab.")
    return props


def load_properties_from_file(path: str) -> List[Dict[str, str]]:
    """Load GA4 property IDs from a text file. NOTE: master_id and property_name will be the GA4 ID."""
    props = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                props.append({"master_id": s, "property_name": s, "ga4_property_id": s})
    print(f"NOTE: File-based loading uses GA4 ID as name. Run without --properties-file for real names.")
    return props


# ----------------------------- GA4 Auth & Queries -----------------------------
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
            raise FileNotFoundError(f"Service account not found: {sa_path}")
        return AnalyticsDataClient(
            credentials=service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        )
    if not os.path.exists(client_secret_file):
        raise FileNotFoundError(f"OAuth client secret not found: {client_secret_file}")
    return AnalyticsDataClient(credentials=get_credentials_oauth(client_secret_file, token_file))


def run_report_with_backoff(client, request):
    delay = BASE_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.run_report(request)
        except (ResourceExhausted, RetryError, GoogleAPIError) as e:
            msg = str(e).lower()
            if any(k in msg for k in ("quota", "exhausted", "429", "rate")) and attempt < MAX_RETRIES:
                time.sleep(min(delay, MAX_BACKOFF))
                delay *= 2
                continue
            raise


def last_event_date(client, property_id: str, event_name: str, lookback_days: int) -> Optional[dt.date]:
    string_filter = Filter.StringFilter(value=event_name)
    try:
        string_filter.match_type = Filter.StringFilter.MatchType.EXACT
    except Exception:
        pass
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=f"{lookback_days}daysAgo", end_date="today")],
        dimension_filter=FilterExpression(filter=Filter(field_name="eventName", string_filter=string_filter)),
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"), desc=True)],
        limit=1,
    )
    resp = run_report_with_backoff(client, request)
    if not resp.rows:
        return None
    try:
        d = dt.datetime.strptime(resp.rows[0].dimension_values[0].value, "%Y%m%d").date()
    except Exception:
        return None
    if int(resp.rows[0].metric_values[0].value or "0") <= 0:
        return None
    return d


# ----------------------------- Google Sheets -----------------------------
def gs_open_worksheet(sheet_url: str, tab_name: str, sa_path: str):
    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.Client(auth=creds)
    sh = client.open_by_url(sheet_url)
    try:
        return sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=tab_name, rows=5000, cols=10)


def gs_init_sheet(ws):
    """Clear sheet and write headers so rows can be appended live."""
    headers = ["Master ID", "Property Name", "GA4 Property ID", "Event Name", "Fired Within Threshold", "Timestamp"]
    delay = 2.0
    for attempt in range(1, 6):
        try:
            ws.clear()
            ws.append_row(headers, value_input_option="USER_ENTERED")
            return
        except Exception as e:
            if attempt < 5:
                time.sleep(delay); delay = min(delay * 2, 30.0)
            else:
                raise


def gs_append_rows(ws, rows: list):
    """Append a batch of rows (one property's events) to the sheet."""
    if not rows:
        return
    delay = 2.0
    for attempt in range(1, 6):
        try:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            return
        except Exception as e:
            if attempt < 5:
                time.sleep(delay); delay = min(delay * 2, 30.0)
            else:
                raise


# ----------------------------- Wrike -----------------------------
def wrike_resolve_contacts(token: str, emails: List[str], direct_ids: Optional[List[str]] = None) -> List[str]:
    # If direct contact IDs are provided, use them — no API lookup needed
    if direct_ids:
        print(f"  Using {len(direct_ids)} direct contact ID(s) from config.")
        return direct_ids
    if not emails:
        return []
    email_set = {e.lower() for e in emails}
    try:
        r = requests.get(
            f"{WRIKE_BASE}/contacts",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": '["profiles"]'},
            timeout=30
        )
        r.raise_for_status()
        contacts = r.json().get("data", [])
        ids = []
        for contact in contacts:
            for profile in contact.get("profiles", []):
                if profile.get("email", "").lower() in email_set:
                    ids.append(contact["id"])
                    print(f"  Resolved: {contact.get('firstName','')} {contact.get('lastName','')} → {contact['id']}")
                    break
        if not ids:
            print(f"  WARNING: No contacts matched {emails}. Listing all contacts to help you find the right ID:")
            for c in contacts:
                profile_emails = [p.get("email", "") for p in c.get("profiles", [])]
                print(f"    ID={c['id']}  {c.get('firstName','')} {c.get('lastName','')}  emails={profile_emails}")
            print("  → Add 'wrike_assign_to_ids': ['<ID>'] to wrike_config.json to bypass email lookup.")
        return ids
    except Exception as e:
        print(f"WARNING: Could not resolve Wrike contacts: {e}")
        return []


def wrike_get_custom_field_ids(token: str) -> Dict[str, str]:
    """Fetch Wrike custom fields and return name→id map for our target fields."""
    target_names = set(WRIKE_CUSTOM_FIELD_VALUES.keys())
    try:
        r = requests.get(
            f"{WRIKE_BASE}/customfields",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        r.raise_for_status()
        id_map = {}
        for field in r.json().get("data", []):
            title = field.get("title", "")
            if title in target_names:
                id_map[title] = field["id"]
        found = set(id_map.keys())
        missing = target_names - found
        if missing:
            print(f"  WARNING: Could not find Wrike custom fields: {missing}")
        else:
            print(f"  Found all {len(id_map)} custom field(s).")
        return id_map
    except Exception as e:
        print(f"WARNING: Could not fetch Wrike custom fields: {e}")
        return {}


def wrike_ticket_exists(token: str, active_folder: str, completed_folder: str, ticket_key: str, check_days: int) -> bool:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=check_days)
    for folder_id in filter(None, [active_folder, completed_folder]):
        try:
            r = requests.get(
                f"{WRIKE_BASE}/folders/{folder_id}/tasks",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30
            )
            r.raise_for_status()
            for task in r.json().get("data", []):
                if task.get("title", "").startswith(str(ticket_key)):
                    created_str = task.get("createdDate", "")
                    if not created_str:
                        return True  # title match, no date — assume recent
                    task_dt = dt.datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if task_dt >= cutoff:
                        return True
        except Exception as e:
            print(f"  WARNING: Could not check Wrike folder {folder_id}: {e}")
    return False


def wrike_create_task(
    token: str,
    folder_id: str,
    title: str,
    description: str,
    responsible_ids: List[str],
    custom_field_ids: Dict[str, str],
) -> bool:
    custom_fields_payload = [
        {"id": fid, "value": WRIKE_CUSTOM_FIELD_VALUES[fname]}
        for fname, fid in custom_field_ids.items()
        if fname in WRIKE_CUSTOM_FIELD_VALUES
    ] if custom_field_ids else []

    # Step 1: Create the task (responsibleIds not allowed on create for this folder)
    payload: Dict[str, str] = {"title": title, "description": description}
    if custom_fields_payload:
        payload["customFields"] = json.dumps(custom_fields_payload)

    try:
        r = requests.post(
            f"{WRIKE_BASE}/folders/{folder_id}/tasks",
            headers={"Authorization": f"Bearer {token}"},
            data=payload,
            timeout=30
        )
        if not r.ok:
            print(f"  Wrike error {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
        task_id = r.json()["data"][0]["id"]
    except Exception as e:
        print(f"  ERROR creating ticket: {e}")
        return False

    # Step 2: Assign via update (uses addResponsibles, not responsibleIds)
    if responsible_ids:
        try:
            r2 = requests.put(
                f"{WRIKE_BASE}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {token}"},
                data={"addResponsibles": json.dumps(responsible_ids)},
                timeout=30
            )
            if not r2.ok:
                print(f"  WARNING: Ticket created but assignment failed: {r2.text[:200]}")
        except Exception as e:
            print(f"  WARNING: Ticket created but assignment failed: {e}")

    return True


def build_description(prop: Dict, failing: List[Dict]) -> str:
    event_lines = "".join(f"<li><b>{e['event_name']}</b>: {e['reason']}</li>" for e in failing)
    return (
        f"<b>GA4 Event Alert</b><br><br>"
        f"<b>Property:</b> {prop['property_name']}<br>"
        f"<b>Master ID:</b> {prop['master_id']}<br>"
        f"<b>GA4 Property ID:</b> {prop['ga4_property_id']}<br><br>"
        f"<b>Events not firing within threshold:</b><br><ul>{event_lines}</ul>"
    )


# ----------------------------- Main -----------------------------
def main():
    parser = argparse.ArgumentParser(description="GA4 audit → Google Sheet + Wrike.")
    parser.add_argument("--properties-file", help="Text file of GA4 IDs. Omit to load from 'US Properties' sheet.")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--service-account-json", help="Service Account JSON for GA4 (default: OAuth).")
    parser.add_argument("--client-secret-file", default=DEFAULT_CLIENT_SECRET_FILE)
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    parser.add_argument("--sheet-tab", default=DEFAULT_SHEET_TAB)
    parser.add_argument("--sheet-service-account", default=DEFAULT_SA_PATH)
    parser.add_argument("--wrike-config", default=DEFAULT_WRIKE_CONFIG)
    parser.add_argument("--no-wrike", action="store_true", help="Skip Wrike ticket creation.")
    parser.add_argument("--summary-csv", default="ga4_audit_results.csv")
    args = parser.parse_args()

    # Load properties
    if args.properties_file:
        props = load_properties_from_file(args.properties_file)
    else:
        props = load_properties_from_sheet(args.sheet_url, args.sheet_service_account)

    if not props:
        print("ERROR: No properties loaded."); sys.exit(2)

    # GA4 client (OAuth by default)
    try:
        ga4 = get_ga4_client(args.service_account_json, args.client_secret_file, args.token_file)
    except Exception as e:
        print(f"Auth error: {e}"); sys.exit(1)

    # Wrike setup
    wrike_cfg = None
    responsible_ids = []
    custom_field_ids = {}
    if not args.no_wrike:
        try:
            with open(args.wrike_config, "r", encoding="utf-8") as f:
                wrike_cfg = json.load(f)
            print("Wrike: resolving contacts...")
            responsible_ids = wrike_resolve_contacts(
                wrike_cfg["wrike_token"],
                wrike_cfg.get("wrike_assign_to", []),
                wrike_cfg.get("wrike_assign_to_ids"),
            )
            print("Wrike: fetching custom field IDs...")
            custom_field_ids = wrike_get_custom_field_ids(wrike_cfg["wrike_token"])
            print("Wrike ready.")
        except Exception as e:
            print(f"WARNING: Wrike config error ({e}) — tickets will be skipped.")
            wrike_cfg = None

    # Initialize sheet upfront so rows can be appended live
    ws = None
    try:
        ws = gs_open_worksheet(args.sheet_url, args.sheet_tab, args.sheet_service_account)
        gs_init_sheet(ws)
        print(f"Sheet '{args.sheet_tab}' ready.")
    except Exception as e:
        print(f"WARNING: Could not initialize sheet ({e}) — sheet writes will be skipped.")

    now_ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = dt.date.today()

    print(f"\n=== GA4 Audit: {len(props)} properties, {len(DEFAULT_EVENTS)} events ===\n")

    csv_rows = []
    wrike_created = wrike_skipped = wrike_errors = 0

    for prop in props:
        pid = prop["ga4_property_id"]
        master_id = prop["master_id"]
        prop_name = prop["property_name"]
        prop_sheet_rows = []
        prop_failing = []
        skip_property = False

        for ev in DEFAULT_EVENTS:
            if skip_property:
                break
            name = ev["name"]
            thr = ev["threshold_days"]
            try:
                ld = last_event_date(ga4, pid, name, args.lookback_days)

                if ld is None:
                    fired = "FALSE"
                    days_since = ""
                    reason = f"No data in last {args.lookback_days} days"
                    prop_failing.append({"event_name": name, "reason": reason, "no_data": True})
                    print(f"  {master_id} | {prop_name[:35]} | {name} → FALSE (no data)")
                else:
                    days_since = max((today - ld).days, 0)
                    exceeded = days_since > thr
                    fired = "FALSE" if exceeded else "TRUE"
                    reason = f"Last fired {days_since}d ago (threshold: {thr}d)"
                    if exceeded:
                        prop_failing.append({"event_name": name, "reason": reason, "no_data": False})
                        print(f"  {master_id} | {prop_name[:35]} | {name} → FALSE ({days_since}d > {thr}d)")

                prop_sheet_rows.append([master_id, prop_name, pid, name, fired, now_ts])
                csv_rows.append({
                    "master_id": master_id,
                    "property_name": prop_name,
                    "ga4_property_id": pid,
                    "event_name": name,
                    "days_since": days_since,
                    "threshold_days": thr,
                    "fired_within_threshold": fired,
                    "timestamp": now_ts,
                })

            except PermissionDenied:
                print(f"  # PERMISSION {master_id} | {prop_name} — skipping property")
                skip_property = True
            except GoogleAPIError as e:
                msg = str(e)
                if any(k in msg.lower() for k in ("permission", "403", "insufficient")):
                    print(f"  # PERMISSION {master_id} | {prop_name} — skipping property")
                    skip_property = True
                else:
                    print(f"  # ERROR {master_id} | {prop_name} | {name}: {msg[:100]}")
            except Exception as e:
                print(f"  # ERROR {master_id} | {prop_name} | {name}: {e}")

        # Write this property's rows to sheet immediately
        if ws and prop_sheet_rows:
            try:
                gs_append_rows(ws, prop_sheet_rows)
            except Exception as e:
                print(f"  WARNING: Sheet write failed for {master_id}: {e}")

        # Create Wrike ticket immediately if this property has real failures
        ticketable = [e for e in prop_failing if not e.get("no_data")]
        if ticketable and wrike_cfg:
            token = wrike_cfg["wrike_token"]
            folder_id = wrike_cfg["wrike_folder_id"]
            completed_folder_id = wrike_cfg.get("wrike_completed_folder_id", "")
            check_days = wrike_cfg.get("wrike_check_existing_days", 14)
            ticket_key = master_id if master_id else pid
            title = f"{prop['master_id']} - {prop['property_name']} - Missing Events"

            if wrike_ticket_exists(token, folder_id, completed_folder_id, ticket_key, check_days):
                print(f"  SKIP WRIKE: {title} (ticket exists within {check_days}d)")
                wrike_skipped += 1
            else:
                desc = build_description(prop, ticketable)
                if wrike_create_task(token, folder_id, title, desc, responsible_ids, custom_field_ids):
                    print(f"  WRIKE CREATED: {title}")
                    wrike_created += 1
                else:
                    wrike_errors += 1
            time.sleep(0.5)

    # Save CSV at the end (local, fast)
    with open(args.summary_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "master_id", "property_name", "ga4_property_id",
            "event_name", "days_since", "threshold_days",
            "fired_within_threshold", "timestamp"
        ])
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"\nSaved CSV: {args.summary_csv}")

    if wrike_cfg:
        print(f"Wrike: {wrike_created} created, {wrike_skipped} skipped, {wrike_errors} errors.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted."); sys.exit(130)