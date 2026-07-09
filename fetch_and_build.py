#!/usr/bin/env python3
"""
Agency Funnel Performance Dashboard
Scoped fork of the MTD Funnel dashboard, limited to the five agency-managed
funnels. Only in-scope funnel data is ever fetched into the output artifacts.

Adds a Closed-Won lead detail table beneath the funnel breakdown, with
email addresses masked server-side before they reach any published file.
"""

import os
import re
import sys
import time
import json
import argparse
import calendar
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from html import escape as esc

import requests

# ── Config ─────────────────────────────────────────────────────────────────────

PACIFIC = ZoneInfo("America/Los_Angeles")
CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]

# GitHub Pages base path — change this one constant if the repo is renamed.
REPO_BASE = "/agency-funnel-dashboard"

session = requests.Session()
session.auth = (CLOSE_API_KEY, "")
session.headers.update({"Content-Type": "application/json"})

# ── Custom Field IDs ───────────────────────────────────────────────────────────

CF_FUNNEL_NAME  = "cf_xqDQE8fkPsWa0RNEve7hcaxKblCe6489XeZGRDzyPdX"  # Funnel Name DEAL (lead)
CF_SHOW_UP      = "cf_OPyvpU45RdvjLqfm8V1VWwNxrGKogEH2IBJmfCj0Uhq"  # First Call Show Up (lead)
CF_QUALIFIED    = "cf_ZDx7NBQaDzV1yYrFcBMzt6cIYj81dAcswpNN0CQzCPS"  # Qualified (lead)
CF_PROGRAM_TIER = "cf_XvdC8hcwyfkoOFn6ElNdGEWbd567Th65m4spLuugYm3"  # *Program Tier Purchased (N) (lead)
CF_UTM_CAMPAIGN = "cf_jnbd0xzUY3tuxzxiGxBs2hONuExeXMvAoTUM2R64Lq3"  # utm_campaign (contact)
CF_UTM_CONTENT  = "cf_R7o66i0XPycLQHlxOLbIqk6c6j3oB8CzxF3e3apI1hn"  # utm_content (contact)
CF_FIRST_SALES  = "cf_LFdYEQ6bsgp49YjZzefypDmdVx8iwuakWDSLPLpVrBq"  # First Sales Call Booked Date (lead)

# First Call Booked Date stamps on the FIRST meeting of any kind, including a
# setter-run "Quick Discovery". First Sales Call Booked Date stamps only on the
# closer call. Comparing the two tells you whether a discovery call happened:
#
#   FCBD <  FSCBD          -> discovery, then advanced to a closer
#   FCBD == FSCBD          -> no discovery; lead went straight to a closer
#   FCBD set, FSCBD unset  -> discovery held, no closer call  ("stuck")
#   FCBD unset             -> no meeting recorded
#
# Both fields hold the MEETING date, not the date the meeting was booked.
CF_FIRST_CALL   = "cf_JsJZIVh7QDcFQBXr4cTRBxf1AkREpLdsKiZB4AEJ8Xh"  # First Call Booked Date (lead)

# 💰 Cash Collected — OPPORTUNITY field, type=number.
# NOT stored in cents. Do not divide by 100. (opp.value IS in cents — it is.)
CF_CASH_COLLECTED = "cf_JP6Zdnv1ClODfctK5iwY9XseSeXEE9ZXbyopPw45OqZ"

# Funnels that use utm_content instead of utm_campaign for sub-breakdown
UTM_CONTENT_FUNNELS = {"Internal Webinar"}

WEEKLY_FEATURE_START = "2026-04"  # Weeks only available for this month and later

# ── Scope Gate ────────────────────────────────────────────────────────────────
# The single source of truth for what this dashboard is allowed to see.
# Strings must match the Close "Funnel Name DEAL" dropdown values EXACTLY.
# Note: Close spells it "Linkedin" (lowercase i), not "LinkedIn".

ALLOWED_FUNNELS = [
    "Instagram",
    "X",
    "Linkedin",
    "Anthony X",
    "Anthony IG",
]
ALLOWED_FUNNELS_SET = set(ALLOWED_FUNNELS)

# Flattened — no In-House / External grouping. All five belong to one agency.
FUNNEL_ORDER = list(ALLOWED_FUNNELS)

# Kept as an empty set so the pace/goal helpers keep the same shape as the
# parent dashboard. LTF - Quiz Funnel is simply out of scope here.
EXCLUDED_FROM_TOTALS_FUNNELS = set()

# ── Filter Constants ──────────────────────────────────────────────────────────

EXCLUDED_LEAD_STATUS_IDS = {
    "stat_hWIGHjzyNpl4YjIFSFz3VK4fp2ny10SFJLKAihmo4KT",  # Canceled (by Lead)
    "stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB",  # Outside the US
}

EXCLUDED_CLOSER_USER_IDS = {
    "user_yRF070m26JE67J6CJqzkAB3IqY7btNm1K5RisCglKa6",  # Ahmad Bukhari
    "user_5cZRqXu8kb4O1IeBVA98UMcMEhYZUhx1fnCHfSL0YMV",  # Stephen Olivas
    "user_4sfuKGMbv0LQZ4hpS8ipASv406kKTSNP5Xx79jOwSqM",  # Spencer Reynolds
    "user_SGISGe3kE7zhSm7LQgZ0Vrt7DKz5RVZ0JzFkI4S8llS",  # Mallory Kent
}


# ── API Helpers ────────────────────────────────────────────────────────────────

def close_get(endpoint, params=None):
    """GET from Close API with 0.5s throttle and 429 retry logic."""
    time.sleep(0.5)
    url = f"https://api.close.com/api/v1/{endpoint}"
    for attempt in range(5):
        resp = session.get(url, params=params or {}, timeout=60)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 5))
            print(f"  Rate limited — waiting {wait}s...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def opp_cf(opp, cf_id):
    """
    Read an opportunity custom field. Close returns these flat ("custom.cf_x")
    on some endpoints and nested under "custom" on others. Try both.
    """
    val = opp.get(f"custom.{cf_id}")
    if val is None:
        val = (opp.get("custom") or {}).get(cf_id)
    if isinstance(val, list):
        val = val[0] if val else None
    return val


# ── PII Handling ──────────────────────────────────────────────────────────────

def mask_email(raw):
    """
    m****h925@gmail.com — keeps first char, last 4 of local part, full domain.
    Called at fetch time. The raw address is never written to any output file.
    """
    if not raw or "@" not in str(raw):
        return "—"
    local, _, domain = str(raw).strip().partition("@")
    if len(local) <= 5:
        return f"{local[0]}****@{domain}"
    return f"{local[0]}****{local[-4:]}@{domain}"


# ── Week helpers ───────────────────────────────────────────────────────────────

def current_week_monday():
    today = datetime.now(PACIFIC).date()
    return today - timedelta(days=today.weekday())


def week_bounds(monday):
    sunday = monday + timedelta(days=6)
    today  = datetime.now(PACIFIC).date()
    return monday, min(sunday, today)


def week_display_label(monday, end_date=None):
    """e.g. 'Apr 6–12' or 'Apr 27–May 3'"""
    if end_date is None:
        end_date = monday + timedelta(days=6)
    if monday.month == end_date.month:
        return f"{monday.strftime('%b %-d')}–{end_date.day}"
    return f"{monday.strftime('%b %-d')}–{end_date.strftime('%b %-d')}"


# ── Users (for the Closer column) ─────────────────────────────────────────────

def fetch_users():
    """Map user_id → display name. One-time fetch, used by the lead table."""
    print("Fetching org users...", flush=True)
    users, skip = {}, 0
    while True:
        data = close_get("user/", {"_limit": 100, "_skip": skip})
        for u in data.get("data", []):
            name = (u.get("display_name")
                    or " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
                    or "—")
            users[u["id"]] = name.strip() or "—"
        if not data.get("has_more"):
            break
        skip += 100
    print(f"  Users: {len(users)}", flush=True)
    return users


# ── Lead Data ──────────────────────────────────────────────────────────────────

LEAD_FIELDS = (f"id,display_name,status_id,"
               f"custom.{CF_FUNNEL_NAME},"
               f"custom.{CF_FIRST_CALL},"
               f"custom.{CF_FIRST_SALES},"
               f"custom.{CF_SHOW_UP},"
               f"custom.{CF_QUALIFIED},"
               f"custom.{CF_PROGRAM_TIER}")


def fetch_lead(lead_id):
    return close_get(f"lead/{lead_id}", {"_fields": LEAD_FIELDS})


def get_funnel_name(lead):
    raw = lead.get(f"custom.{CF_FUNNEL_NAME}")
    val = (raw or "").strip()
    return val if val else "Unknown (Needs Review)"


def in_scope(lead):
    """The gate. Anything that fails this never enters funnel_data or the HTML."""
    return get_funnel_name(lead) in ALLOWED_FUNNELS_SET


def fetch_won_opps_by_range(start_date, end_date):
    """
    Fetch all won opportunities with date_won in [start_date, end_date].

    Deliberately NO _fields whitelist — combining _fields with custom field ids
    has bitten us before on other Close endpoints (silent drops). The payload is
    small enough (low hundreds/month) that pulling the full object is cheaper
    than debugging a field that quietly comes back null.
    """
    start_str = start_date.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")
    print(f"Fetching won opportunities ({start_str} → {end_str})...", flush=True)
    opps, skip = [], 0
    while True:
        data = close_get("opportunity/", {
            "status_type":   "won",
            "date_won__gte": start_str,
            "date_won__lte": end_str,
            "_skip":         skip,
            "_limit":        100,
        })
        batch = data.get("data", [])
        opps.extend(batch)
        if not data.get("has_more"):
            break
        skip += 100
    print(f"  Won opportunities (all funnels): {len(opps)}", flush=True)
    return opps


def parse_value(raw):
    """opp.value is in CENTS. Divide by 100."""
    if raw is None:
        return 0.0
    try:
        cents = float(str(raw).split()[0].replace(",", "").replace("$", ""))
        return cents / 100.0
    except Exception:
        return 0.0


def parse_cash(raw):
    """
    💰 Cash Collected is a plain number field — NOT cents.
    Returns None (not 0.0) when unset, so the UI can show '—' rather than '$0'.
    """
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


# ── Contact / UTM / Email ─────────────────────────────────────────────────────

def fetch_contact_data(lead_id):
    """
    Return (utm_campaign, utm_content, masked_email).
    Only ever called for in-scope leads. Raw email is masked here and discarded.
    """
    data = close_get("contact/", {
        "lead_id": lead_id,
        "_fields": f"id,emails,custom.{CF_UTM_CAMPAIGN},custom.{CF_UTM_CONTENT}",
        "_limit":  10,
    })
    contacts = data.get("data", [])
    best_campaign = None
    best_content  = None
    raw_email     = None
    for c in contacts:
        campaign = c.get(f"custom.{CF_UTM_CAMPAIGN}")
        content  = c.get(f"custom.{CF_UTM_CONTENT}")
        if campaign and not best_campaign:
            best_campaign = str(campaign).strip()
        if content and not best_content:
            best_content = str(content).strip()
        if not raw_email:
            for e in (c.get("emails") or []):
                if e.get("email"):
                    raw_email = e["email"]
                    break
    return best_campaign, best_content, mask_email(raw_email)


# ── Booked / Created Fetches ──────────────────────────────────────────────────

def fetch_leads_by_booked_date(start_date, end_date):
    """
    Leads where First Sales Call Booked Date falls in range.

    Note: we intentionally do NOT add a funnel OR-clause to the Close query.
    Server-side filtering on a multi-value dropdown risks silently dropping
    matches; the funnel gate is applied client-side in aggregate_data instead.
    """
    start_str = start_date.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")
    query     = f'custom.{CF_FIRST_SALES} >= "{start_str}" AND custom.{CF_FIRST_SALES} <= "{end_str}"'
    print(f"Fetching booked leads ({start_str} → {end_str})...", flush=True)

    leads, skip = [], 0
    while True:
        data = close_get("lead/", {
            "query":   query,
            "_fields": LEAD_FIELDS,
            "_limit":  200,
            "_skip":   skip,
        })
        batch = data.get("data", [])
        leads.extend(batch)
        print(f"  Fetched {len(leads)} leads so far...", flush=True)
        if not data.get("has_more"):
            break
        skip += 200

    print(f"  Total booked leads (all funnels): {len(leads)}", flush=True)
    return leads


def fetch_leads_by_disco_date(start_date, end_date):
    """
    Leads where First Call Booked Date falls in range. Superset of the booked
    query: it also returns leads that had a discovery call and never reached a
    closer — the ones this dashboard was previously blind to.
    """
    start_str = start_date.strftime("%Y-%m-%d")
    end_str   = end_date.strftime("%Y-%m-%d")
    query     = f'custom.{CF_FIRST_CALL} >= "{start_str}" AND custom.{CF_FIRST_CALL} <= "{end_str}"'
    print(f"Fetching discovery-call leads ({start_str} → {end_str})...", flush=True)

    leads, skip = [], 0
    while True:
        data = close_get("lead/", {
            "query":   query,
            "_fields": LEAD_FIELDS,
            "_limit":  200,
            "_skip":   skip,
        })
        batch = data.get("data", [])
        leads.extend(batch)
        if not data.get("has_more"):
            break
        skip += 200

    print(f"  Total first-call leads (all funnels): {len(leads)}", flush=True)
    return leads


def fetch_leads_created(start_date, end_date):
    """All leads created in range (Pacific). Filtered to scope downstream."""
    start_utc = datetime(start_date.year, start_date.month, start_date.day,
                         0, 0, 0, tzinfo=PACIFIC).astimezone(timezone.utc)
    end_utc   = datetime(end_date.year, end_date.month, end_date.day,
                         23, 59, 59, tzinfo=PACIFIC).astimezone(timezone.utc)
    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    end_str   = end_utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    query = f'date_created >= "{start_str}" AND date_created <= "{end_str}"'
    print(f"Fetching leads created ({start_date} → {end_date})...", flush=True)
    leads, skip = [], 0
    while True:
        data = close_get("lead/", {
            "query":   query,
            "_fields": f"id,status_id,custom.{CF_FUNNEL_NAME}",
            "_limit":  200,
            "_skip":   skip,
        })
        batch = data.get("data", [])
        leads.extend(batch)
        print(f"  Fetched {len(leads)} leads created so far...", flush=True)
        if not data.get("has_more"):
            break
        skip += 200

    print(f"  Total leads created (all funnels): {len(leads)}", flush=True)
    return leads


# ── Main Aggregation ───────────────────────────────────────────────────────────

def _is_yes(val):
    if val is None or val is False: return False
    if val is True: return True
    return str(val).strip().lower() in ("yes", "true", "1")


def _d(val):
    """Normalize a Close date field to a YYYY-MM-DD string, or '' if unset."""
    if not val:
        return ""
    return str(val).strip()[:10]


def had_disco(lead):
    """
    True if this lead sat on a setter's calendar before (or instead of) a closer.

    Caveat: a lead with FCBD set and FSCBD unset is assumed to be a discovery
    call. In the rare window between a direct closer booking and the field
    updater's next pass, that lead would be misread as a discovery. A dedicated
    'Quick Discovery Booked Date' field would remove the inference entirely.
    """
    fcbd  = _d(lead.get(f"custom.{CF_FIRST_CALL}"))
    fscbd = _d(lead.get(f"custom.{CF_FIRST_SALES}"))
    if not fcbd:
        return False
    if not fscbd:
        return True          # first meeting happened, no sales call ever booked
    return fcbd < fscbd      # equal => the first meeting WAS the sales call


def advanced_to_closer(lead):
    """Did this discovery lead ever reach a closer? Any date, not just this period."""
    return bool(_d(lead.get(f"custom.{CF_FIRST_SALES}")))


def _fmt_won_date(raw):
    if not raw:
        return "—"
    s = str(raw)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%-m/%-d/%Y")
    except ValueError:
        return s


def aggregate_data(start_date, end_date, month_label,
                   won_opps, users,
                   lead_cache=None, contact_cache=None):
    """
    Aggregate in-scope booked leads and won opps.
    Returns (data_dict, lead_cache, contact_cache).
    """
    lead_cache    = lead_cache    if lead_cache    is not None else {}
    contact_cache = contact_cache if contact_cache is not None else {}

    # ── Leads created ─────────────────────────────────────────────────────────
    created_leads = fetch_leads_created(start_date, end_date)
    leads_created_by_funnel = {}
    for lead in created_leads:
        if lead.get("status_id") in EXCLUDED_LEAD_STATUS_IDS:
            continue
        if not in_scope(lead):
            continue
        funnel = get_funnel_name(lead)
        leads_created_by_funnel[funnel] = leads_created_by_funnel.get(funnel, 0) + 1

    # ── Booked leads ──────────────────────────────────────────────────────────
    booked_leads = fetch_leads_by_booked_date(start_date, end_date)

    meeting_rows = []
    for lead in booked_leads:
        lid = lead.get("id")
        if not lid:
            continue
        lead_cache[lid] = lead
        if lead.get("status_id") in EXCLUDED_LEAD_STATUS_IDS:
            continue
        if not in_scope(lead):
            continue  # ← scope gate: out-of-scope funnels never reach the output
        funnel    = get_funnel_name(lead)
        show_up   = _is_yes(lead.get(f"custom.{CF_SHOW_UP}"))
        qualified = _is_yes(lead.get(f"custom.{CF_QUALIFIED}"))
        if lid not in contact_cache:
            contact_cache[lid] = fetch_contact_data(lid)
        utm_campaign, utm_content, _masked = contact_cache[lid]
        utm = (utm_content or "Unattributed") if funnel in UTM_CONTENT_FUNNELS \
              else (utm_campaign or "Unattributed")
        meeting_rows.append({"funnel": funnel, "show_up": show_up,
                             "qualified": qualified, "utm_campaign": utm})

    print(f"  In-scope closer calls: {len(meeting_rows)}", flush=True)

    # ── Discovery (setter) calls ──────────────────────────────────────────────
    # Counted independently of the booked set. A lead with a discovery on 6/19
    # and a closer call on 6/22 appears in BOTH columns — they are two calls.
    # Leads whose discovery never produced a closer call ("stuck") appear ONLY
    # here; they are invisible to the booked query entirely.
    disco_leads = fetch_leads_by_disco_date(start_date, end_date)

    disco_rows = []
    for lead in disco_leads:
        lid = lead.get("id")
        if not lid:
            continue
        lead_cache[lid] = lead
        if lead.get("status_id") in EXCLUDED_LEAD_STATUS_IDS:
            continue
        if not in_scope(lead):
            continue
        if not had_disco(lead):
            continue     # first meeting WAS the closer call — not a setter call
        funnel = get_funnel_name(lead)
        if lid not in contact_cache:
            contact_cache[lid] = fetch_contact_data(lid)
        utm_campaign, utm_content, _masked = contact_cache[lid]
        utm = (utm_content or "Unattributed") if funnel in UTM_CONTENT_FUNNELS \
              else (utm_campaign or "Unattributed")
        disco_rows.append({"funnel": funnel, "utm_campaign": utm,
                           "stuck": not advanced_to_closer(lead)})

    _stuck_n = sum(1 for r in disco_rows if r["stuck"])
    print(f"  In-scope setter calls: {len(disco_rows)} ({_stuck_n} stuck)", flush=True)

    # ── Closed-won ────────────────────────────────────────────────────────────
    closed_rows    = []
    closed_leads   = []   # ← powers the lead detail table
    tier_by_funnel = {}
    for opp in won_opps:
        lid = opp.get("lead_id")
        if not lid:
            continue
        if lid not in lead_cache:
            lead_cache[lid] = fetch_lead(lid)
        lead = lead_cache[lid]
        if lead.get("status_id") in EXCLUDED_LEAD_STATUS_IDS:
            continue
        if opp.get("user_id") in EXCLUDED_CLOSER_USER_IDS:
            continue
        if not in_scope(lead):
            continue  # ← scope gate

        funnel = get_funnel_name(lead)
        value  = parse_value(opp.get("value"))
        cash   = parse_cash(opp_cf(opp, CF_CASH_COLLECTED))

        if lid not in contact_cache:
            contact_cache[lid] = fetch_contact_data(lid)
        utm_campaign, utm_content, masked_email = contact_cache[lid]
        utm = (utm_content or "Unattributed") if funnel in UTM_CONTENT_FUNNELS \
              else (utm_campaign or "Unattributed")

        closed_rows.append({"funnel": funnel, "value": value, "utm_campaign": utm})

        tier_raw = lead.get(f"custom.{CF_PROGRAM_TIER}")
        if isinstance(tier_raw, list): tier_raw = tier_raw[0] if tier_raw else None
        tier = str(tier_raw).strip() if tier_raw else "Unknown"

        closed_leads.append({
            "date_won":  str(opp.get("date_won") or "")[:10],
            "date_disp": _fmt_won_date(opp.get("date_won")),
            "client":    lead.get("display_name") or "—",
            "email":     masked_email,
            "program":   tier,
            "funnel":    funnel,
            "closer":    users.get(opp.get("user_id"), "—"),
            "gross":     value,
            "cash":      cash,
        })

        tier_by_funnel.setdefault(funnel, {})
        tier_by_funnel[funnel].setdefault(tier, {"count": 0, "revenue": 0.0})
        tier_by_funnel[funnel][tier]["count"]   += 1
        tier_by_funnel[funnel][tier]["revenue"] += value

    closed_leads.sort(key=lambda r: (r["date_won"], -r["gross"]))
    print(f"  In-scope closed-won rows: {len(closed_rows)}", flush=True)

    # ── Aggregate ─────────────────────────────────────────────────────────────
    funnel_data = {}
    def slot(funnel, utm):
        funnel_data.setdefault(funnel, {})
        funnel_data[funnel].setdefault(utm, {
            "booked": 0, "setter": 0, "stuck": 0,
            "showed": 0, "qualified": 0, "closed": 0, "revenue": 0.0})
        return funnel_data[funnel][utm]

    for row in meeting_rows:
        s = slot(row["funnel"], row["utm_campaign"])
        s["booked"]    += 1
        s["showed"]    += 1 if row["show_up"]   else 0
        s["qualified"] += 1 if row["qualified"] else 0
    for row in disco_rows:
        s = slot(row["funnel"], row["utm_campaign"])
        s["setter"] += 1
        s["stuck"]  += 1 if row["stuck"] else 0
    for row in closed_rows:
        s = slot(row["funnel"], row["utm_campaign"])
        s["closed"]  += 1
        s["revenue"] += row["value"]

    # Ensure every allowed funnel has a row, even with zero activity
    for funnel in ALLOWED_FUNNELS:
        funnel_data.setdefault(funnel, {})

    funnel_totals = {}
    for funnel in ALLOWED_FUNNELS:
        t = {"leads_created": leads_created_by_funnel.get(funnel, 0),
             "booked": 0, "setter": 0, "stuck": 0,
             "showed": 0, "qualified": 0, "closed": 0, "revenue": 0.0}
        for v in funnel_data.get(funnel, {}).values():
            for k in ("booked", "setter", "stuck", "showed", "qualified", "closed", "revenue"):
                t[k] += v[k]
        funnel_totals[funnel] = t

    grand = {"leads_created": 0, "booked": 0, "setter": 0, "stuck": 0,
             "showed": 0, "qualified": 0, "closed": 0, "revenue": 0.0}
    for funnel, t in funnel_totals.items():
        for k in grand:
            grand[k] += t.get(k, 0)

    cash_vals    = [r["cash"] for r in closed_leads if r["cash"] is not None]
    grand_cash   = sum(cash_vals)
    cash_filled  = len(cash_vals)
    cash_total_n = len(closed_leads)

    now_pac      = datetime.now(PACIFIC)
    _goals       = load_goals()
    _days_in_mon = calendar.monthrange(start_date.year, start_date.month)[1]
    _day_elapsed = end_date.day if end_date.month == start_date.month else _days_in_mon

    data = {
        "funnel_data":    funnel_data,
        "funnel_totals":  funnel_totals,
        "tier_by_funnel": tier_by_funnel,
        "closed_leads":   closed_leads,
        "grand":          grand,
        "grand_cash":     grand_cash,
        "cash_filled":    cash_filled,
        "cash_total_n":   cash_total_n,
        "generated_at":   now_pac.strftime("%B %d, %Y at %I:%M %p PT"),
        "month_label":    month_label,
        "start_date":     start_date,
        "end_date":       end_date,
        "goals":          _goals,
        "day_of_month":   _day_elapsed,
        "days_in_month":  _days_in_mon,
    }
    return data, lead_cache, contact_cache


# ── HTML Helpers ───────────────────────────────────────────────────────────────

def pct(num, denom):
    if not denom:
        return "—"
    return f"{num / denom * 100:.1f}%"

def pct_class(num, denom, high=0.70, low=0.50):
    if not denom:
        return ""
    r = num / denom
    if r >= high:
        return "good"
    if r < low:
        return "bad"
    return "mid"

def fmt_currency(val):
    if not val:
        return "$0"
    return f"${val:,.0f}"

def fmt_cash(val):
    """Blank Cash Collected renders as an em dash, never $0."""
    if val is None:
        return "—"
    return f"${val:,.0f}"

def rev_per_close(revenue, closed):
    if not closed:
        return "—"
    return f"${revenue / closed:,.0f}"

def advance_pct(setter, stuck):
    """Share of discovery calls that went on to reach a closer."""
    if not setter:
        return "—"
    return f"{(setter - stuck) / setter * 100:.0f}%"


def advance_class(setter, stuck):
    if not setter:
        return ""
    r = (setter - stuck) / setter
    if r >= 0.60: return "good"
    if r <  0.35: return "bad"
    return "mid"


def funnel_slug(name):
    return re.sub(r"[^a-z0-9]", "_", name.lower())


# ── Goals ─────────────────────────────────────────────────────────────────────

def load_goals():
    try:
        with open("goals.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}

def calc_on_pace(booked, goal, day_of_month, days_in_month):
    if not day_of_month or not booked:
        return None
    return round((booked / day_of_month) * days_in_month)

def pace_class(booked, on_pace, goal):
    if on_pace is None or not goal: return "pace-muted"
    if on_pace > goal:  return "pace-exceed"
    if on_pace == goal: return "pace-on"
    return "pace-behind"

def pace_label(booked, on_pace, goal):
    if on_pace is None:
        return "—"
    return str(on_pace)

def goal_pct_label(booked, goal):
    if not goal:
        return "—"
    p = round(booked / goal * 100)
    return f"{p}% ({goal})"


# ── Funnel Rows (flat — no group sections) ────────────────────────────────────

def build_funnel_rows(funnel_data, funnel_totals, goals=None,
                      day_of_month=1, days_in_month=30, tier_by_funnel=None):
    rows = []

    for funnel in ALLOWED_FUNNELS:
        t   = funnel_totals.get(funnel, {})
        bo  = t.get("booked", 0)
        se  = t.get("setter", 0)
        stk = t.get("stuck", 0)
        sh  = t.get("showed", 0)
        qu  = t.get("qualified", 0)
        cl  = t.get("closed", 0)
        rev = t.get("revenue", 0.0)
        fid = funnel_slug(funnel)

        _goals   = goals or {}
        _goal    = _goals.get(funnel)
        _on_pace = calc_on_pace(bo, _goal, day_of_month, days_in_month)
        _pc      = pace_class(bo, _on_pace, _goal)
        lc       = t.get("leads_created", 0)
        lc_disp  = lc if lc else "—"

        rows.append(f"""
    <tr class="funnel-row" onclick="toggleUTM('{fid}')" data-fid="{fid}">
      <td class="col-name">
        <span class="chevron" id="chev-{fid}">›</span>{esc(funnel)}
      </td>
      <td class="col-num">{lc_disp}</td>
      <td class="col-num col-setter">{se if se else "—"}</td>
      <td class="col-num col-stuck">{stk if stk else "—"}</td>
      <td class="col-pct {advance_class(se, stk)}">{advance_pct(se, stk)}</td>
      <td class="col-num">{bo if bo else "—"}</td>
      <td class="col-pace {_pc}">{pace_label(bo, _on_pace, _goal)}</td>
      <td class="col-goal">{goal_pct_label(bo, _goal)}</td>
      <td class="col-num">{sh if sh else "—"}</td>
      <td class="col-pct {pct_class(sh, bo)}">{pct(sh, bo)}</td>
      <td class="col-num">{qu if qu else "—"}</td>
      <td class="col-pct {pct_class(qu, bo)}">{pct(qu, bo)}</td>
      <td class="col-num">{cl if cl else "—"}</td>
      <td class="col-pct {pct_class(cl, bo, high=0.15, low=0.07)}">{pct(cl, bo)}</td>
      <td class="col-rev pkg-trigger" onclick="event.stopPropagation();togglePkg('{fid}')" title="Click to see package breakdown">{fmt_currency(rev)} <span class="pkg-chevron" id="pkgchev-{fid}">›</span></td>
      <td class="col-num">{rev_per_close(rev, cl)}</td>
    </tr>""")

        tiers = (tier_by_funnel or {}).get(funnel, {})
        for tier_name, tvals in sorted(tiers.items(), key=lambda x: -x[1]["revenue"]):
            tc  = tvals["count"]
            tr_ = tvals["revenue"]
            rows.append(f"""
    <tr class="pkg-row" data-parent="{fid}" style="display:none">
      <td class="col-name col-pkg">↳ {esc(tier_name)}</td>
      <td class="col-num">—</td>
      <td class="col-num">—</td>
      <td class="col-num">—</td>
      <td class="col-pct"></td>
      <td class="col-num">—</td>
      <td class="col-pace"></td>
      <td class="col-goal"></td>
      <td class="col-num">—</td>
      <td class="col-pct"></td>
      <td class="col-num">—</td>
      <td class="col-pct"></td>
      <td class="col-num">{tc}</td>
      <td class="col-pct"></td>
      <td class="col-rev">{fmt_currency(tr_)}</td>
      <td class="col-num">{rev_per_close(tr_, tc)}</td>
    </tr>""")

        utms = funnel_data.get(funnel, {})
        for utm_label, vals in sorted(
                utms.items(),
                key=lambda x: (-x[1]["booked"], -x[1].get("setter", 0))):
            b, s, q, c, r = (vals["booked"], vals["showed"], vals["qualified"],
                             vals["closed"], vals["revenue"])
            use, ustk = vals.get("setter", 0), vals.get("stuck", 0)
            rows.append(f"""
    <tr class="utm-row" data-parent="{fid}">
      <td class="col-name col-utm">↳ {esc(utm_label)}</td>
      <td class="col-num">—</td>
      <td class="col-num col-setter">{use if use else "—"}</td>
      <td class="col-num col-stuck">{ustk if ustk else "—"}</td>
      <td class="col-pct {advance_class(use, ustk)}">{advance_pct(use, ustk)}</td>
      <td class="col-num">{b if b else "—"}</td>
      <td class="col-pace"></td>
      <td class="col-goal"></td>
      <td class="col-num">{s if s else "—"}</td>
      <td class="col-pct {pct_class(s, b)}">{pct(s, b)}</td>
      <td class="col-num">{q if q else "—"}</td>
      <td class="col-pct {pct_class(q, b)}">{pct(q, b)}</td>
      <td class="col-num">{c if c else "—"}</td>
      <td class="col-pct {pct_class(c, b, high=0.15, low=0.07)}">{pct(c, b)}</td>
      <td class="col-rev">{fmt_currency(r)}</td>
      <td class="col-num">{rev_per_close(r, c)}</td>
    </tr>""")

    return "\n".join(rows)


# ── Closed-Won Lead Table ─────────────────────────────────────────────────────

def build_lead_rows(closed_leads):
    if not closed_leads:
        return """
    <tr><td colspan="8" class="lead-empty">No closed-won deals in this period.</td></tr>"""
    rows = []
    for r in closed_leads:
        fid = funnel_slug(r["funnel"])
        cash_cls = "col-cash" if r["cash"] is not None else "col-cash cash-missing"
        rows.append(f"""
    <tr class="lead-row">
      <td class="col-date">{esc(r["date_disp"])}</td>
      <td class="col-client">{esc(r["client"])}</td>
      <td class="col-email">{esc(r["email"])}</td>
      <td class="col-program">{esc(r["program"])}</td>
      <td class="col-funnel"><span class="funnel-chip chip-{fid}">{esc(r["funnel"])}</span></td>
      <td class="col-closer">{esc(r["closer"])}</td>
      <td class="col-gross">{fmt_currency(r["gross"])}</td>
      <td class="{cash_cls}">{fmt_cash(r["cash"])}</td>
    </tr>""")
    return "\n".join(rows)


# ── HTML Generation ────────────────────────────────────────────────────────────

def generate_html(data, month_picker_html="", week_picker_html=""):
    grand          = data["grand"]
    goals          = data.get("goals", {})
    day_of_month   = data.get("day_of_month", 1)
    days_in_month  = data.get("days_in_month", 30)
    tier_by_funnel = data.get("tier_by_funnel", {})
    closed_leads   = data.get("closed_leads", [])

    funnel_rows = build_funnel_rows(data["funnel_data"], data["funnel_totals"],
                                    goals, day_of_month, days_in_month,
                                    tier_by_funnel)
    lead_rows   = build_lead_rows(closed_leads)

    g_lc  = grand.get("leads_created", 0)
    g_bo  = grand["booked"]
    g_se  = grand.get("setter", 0)
    g_stk = grand.get("stuck", 0)
    g_sh  = grand["showed"]
    g_qu  = grand["qualified"]
    g_cl  = grand["closed"]
    g_rev = grand["revenue"]

    g_cash    = data.get("grand_cash", 0.0)
    cash_n    = data.get("cash_filled", 0)
    cash_of   = data.get("cash_total_n", 0)
    cash_note = (f"{fmt_currency(g_cash)} cash · {cash_n} of {cash_of} deals"
                 if cash_of else "no cash data")
    cash_incomplete = cash_of and cash_n < cash_of

    lead_count_label = f"{len(closed_leads)} deal" + ("" if len(closed_leads) == 1 else "s")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Agency Funnel Performance — {data['month_label']}</title>
<style>
  :root {{
    --bg:        #f4f6f9;
    --surface:   #ffffff;
    --surface2:  #f0f2f7;
    --border:    #dde1ea;
    --border2:   #e8eaf0;
    --text:      #1a1f36;
    --muted:     #8792a2;
    --muted2:    #5c6680;
    --green:     #0e9f6e;
    --red:       #e02424;
    --amber:     #d97706;
    --blue:      #2563eb;
    --purple:    #7c3aed;
    --accent:    #4f46e5;
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
    font-size: 13px;
    min-height: 100vh;
  }}

  .kpi {{ box-shadow: 0 1px 3px rgba(0,0,0,0.07), 0 1px 2px rgba(0,0,0,0.04); }}
  table {{
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    border-radius: 8px;
    overflow: hidden;
    width: 100%;
    border-collapse: collapse;
  }}

  /* ── Header ── */
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding: 28px 36px 0;
  }}
  .header-left h1 {{
    font-size: 20px;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.01em;
  }}
  .header-left .sub {{
    font-size: 11.5px;
    color: var(--muted2);
    margin-top: 3px;
  }}
  .header-right {{
    text-align: right;
    font-size: 11px;
    color: var(--muted2);
    line-height: 1.6;
  }}
  .header-right .snapshot-label {{
    font-weight: 600;
    color: var(--muted2);
    display: block;
  }}

  /* ── KPI Cards ── */
  .kpis {{
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 12px;
    padding: 24px 36px;
  }}
  @media (max-width: 1500px) {{
    .kpis {{ grid-template-columns: repeat(4, 1fr); }}
  }}
  .kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
    position: relative;
    overflow: hidden;
  }}
  .kpi::before {{
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--kpi-accent, var(--accent));
    opacity: 0.6;
  }}
  .kpi .label {{
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--muted2);
    margin-bottom: 8px;
  }}
  .kpi .value {{
    font-size: 34px;
    font-weight: 700;
    line-height: 1;
    color: var(--kpi-color, var(--text));
  }}
  .kpi .kpi-sub {{
    font-size: 11px;
    color: var(--muted2);
    margin-top: 5px;
  }}
  .kpi .kpi-cash {{
    font-size: 10.5px;
    color: var(--muted);
    margin-top: 7px;
    padding-top: 7px;
    border-top: 1px solid var(--border);
  }}
  .kpi .kpi-cash.partial {{ color: var(--amber); }}

  /* ── Section label ── */
  .section-label {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 36px 10px;
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }}
  .section-label::after {{
    content: "";
    flex: 1;
    height: 1px;
    background: var(--border);
  }}
  .section-label .count-pill {{
    background: var(--accent);
    color: #fff;
    border-radius: 20px;
    padding: 2px 10px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.03em;
  }}

  /* ── Table ── */
  .table-wrap {{
    padding: 0 36px 40px;
    overflow-x: auto;
  }}

  thead th {{
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    font-weight: 500;
    text-align: left;
  }}
  thead th.col-num,
  thead th.col-pct,
  thead th.col-rev,
  thead th.col-gross,
  thead th.col-cash {{ text-align: right; }}

  .funnel-row {{
    cursor: pointer;
    border-top: 1px solid var(--border2);
    transition: background 0.1s;
  }}
  .funnel-row:hover {{ background: rgba(79,70,229,0.04); }}
  .funnel-row td {{ padding: 11px 12px; }}

  .utm-row {{ display: none; background: rgba(79,70,229,0.025); }}
  .utm-row.open {{ display: table-row; }}
  .utm-row td {{ padding: 7px 12px; }}
  .utm-row + .utm-row td {{ border-top: 1px solid var(--border2); }}

  .pkg-row {{ display: none; background: #faf9ff; }}
  .pkg-row.open {{ display: table-row; }}
  .pkg-row td {{ padding: 10px 12px; color: var(--muted) !important; font-weight: 400 !important; }}
  .pkg-row + .pkg-row td {{ border-top: 1px solid var(--border2); }}
  .pkg-row .col-rev  {{ color: #7bc4a0 !important; font-weight: 400 !important; }}
  .pkg-row .col-num  {{ color: var(--muted) !important; }}
  .col-pkg {{ color: var(--muted2) !important; font-size: 12px; padding-left: 28px !important; }}
  .pkg-trigger {{ cursor: pointer; user-select: none; }}
  .pkg-trigger:hover {{ opacity: 0.75; }}
  .pkg-chevron {{
    font-size: 11px; color: #c0c8d4; margin-left: 4px;
    display: inline-block; transition: transform 0.15s;
  }}
  .pkg-chevron.open {{ transform: rotate(90deg); color: var(--muted2); }}

  .total-row {{
    border-top: 2px solid var(--border);
    font-weight: 700;
    background: var(--surface2);
    color: var(--text);
  }}
  .total-row td {{ padding: 12px 12px; }}

  .col-name   {{ min-width: 190px; font-weight: 500; white-space: nowrap; }}
  .col-utm    {{ color: var(--muted2); padding-left: 32px !important; font-weight: 400; }}
  .col-num    {{ text-align: right; color: var(--text); }}
  .col-pct    {{ text-align: right; font-weight: 500; }}
  .col-rev    {{ text-align: right; color: var(--green); font-weight: 500; }}

  .col-pct.good {{ color: var(--green); }}
  .col-pct.bad  {{ color: var(--red); }}
  .col-pct.mid  {{ color: var(--amber); }}

  .col-setter {{ color: var(--purple); }}
  .col-stuck  {{ color: var(--amber); font-weight: 500; }}

  .col-pace  {{ text-align: right; font-size: 12px; color: var(--muted); }}
  .col-goal  {{ text-align: right; font-size: 12px; color: var(--muted); }}
  .col-pace.pace-exceed  {{ color: var(--green); font-weight: 600; }}
  .col-pace.pace-on      {{ color: #ca8a04;      font-weight: 500; }}
  .col-pace.pace-behind  {{ color: var(--red);   font-weight: 500; }}

  .chevron {{
    display: inline-block;
    width: 16px;
    color: var(--muted);
    font-size: 14px;
    transition: transform 0.15s ease;
    transform: rotate(0deg);
    line-height: 1;
  }}
  .chevron.open {{ transform: rotate(90deg); color: var(--accent); }}

  /* ── Lead Detail Table ── */
  .lead-row {{ border-top: 1px solid var(--border2); }}
  .lead-row td {{ padding: 10px 12px; white-space: nowrap; }}
  .lead-row:hover {{ background: rgba(79,70,229,0.03); }}
  .col-date    {{ color: var(--muted2); font-variant-numeric: tabular-nums; }}
  .col-client  {{ font-weight: 500; }}
  .col-email   {{ color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }}
  .col-program {{ color: var(--muted2); }}
  .col-closer  {{ color: var(--muted2); }}
  .col-gross   {{ text-align: right; color: var(--green); font-weight: 500; font-variant-numeric: tabular-nums; }}
  .col-cash    {{ text-align: right; color: var(--blue);  font-weight: 500; font-variant-numeric: tabular-nums; }}
  .col-cash.cash-missing {{ color: var(--muted); font-weight: 400; }}
  .lead-empty  {{ padding: 24px 12px; text-align: center; color: var(--muted); }}

  .funnel-chip {{
    display: inline-block;
    font-size: 10.5px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--surface2);
    color: var(--muted2);
    border: 1px solid var(--border);
  }}
  .chip-instagram {{ background:#fce7f3; color:#9d174d; border-color:#fbcfe8; }}
  .chip-x         {{ background:#e5e7eb; color:#1f2937; border-color:#d1d5db; }}
  .chip-linkedin  {{ background:#dbeafe; color:#1e40af; border-color:#bfdbfe; }}
  .chip-anthony_x  {{ background:#ede9fe; color:#5b21b6; border-color:#ddd6fe; }}
  .chip-anthony_ig {{ background:#fef3c7; color:#92400e; border-color:#fde68a; }}

  /* ── Pickers ── */
  .pickers-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }}
  .month-picker, .week-picker {{ display: flex; align-items: center; gap: 7px; }}
  .month-picker select, .week-picker select {{
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    outline: none;
  }}
  .month-picker select:hover, .week-picker select:hover {{ border-color: var(--accent); }}
  .picker-divider {{ color: var(--border); font-size: 16px; line-height: 1; margin: 0 2px; }}
  .archive-badge {{
    display: inline-block;
    background: #fef3c7;
    color: #92400e;
    border: 1px solid #fcd34d;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    padding: 2px 7px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    white-space: nowrap;
  }}

  @media (max-width: 960px) {{
    .kpis {{ grid-template-columns: repeat(2, 1fr); }}
    .header {{ flex-direction: column; gap: 12px; }}
    .header-right {{ text-align: left; }}
  }}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <h1>Agency Funnel Performance</h1>
    <p class="sub">Vendingpreneurs · Instagram · X · Linkedin · Anthony X · Anthony IG · {data['month_label']}{data.get('week_range_label','')}</p>
  </div>
  <div class="header-right">
    <div class="pickers-row">
      {month_picker_html}{week_picker_html}
    </div>
    <span class="snapshot-label">{data.get("badge_html","") or "Snapshot"}</span>
    {data['generated_at']}<br>
    Source · Close CRM
  </div>
</div>

<!-- KPI Cards -->
<div class="kpis">
  <div class="kpi" style="--kpi-accent:#6366f1; --kpi-color:#6366f1;">
    <div class="label">Leads Created</div>
    <div class="value">{g_lc}</div>
    <div class="kpi-sub">new leads this period</div>
  </div>
  <div class="kpi" style="--kpi-accent:#9333ea; --kpi-color:#9333ea;">
    <div class="label">Setter Calls</div>
    <div class="value">{g_se}</div>
    <div class="kpi-sub">{advance_pct(g_se, g_stk)} advanced to a closer</div>
    <div class="kpi-cash {'partial' if g_stk else ''}">{g_stk} stuck · no closer call</div>
  </div>
  <div class="kpi" style="--kpi-accent:#4f46e5; --kpi-color:var(--text);">
    <div class="label">Closer Calls</div>
    <div class="value">{g_bo}</div>
    <div class="kpi-sub">first sales calls</div>
  </div>
  <div class="kpi" style="--kpi-accent:#2563eb; --kpi-color:#2563eb;">
    <div class="label">Showed</div>
    <div class="value">{g_sh}</div>
    <div class="kpi-sub">{pct(g_sh, g_bo)} show rate</div>
  </div>
  <div class="kpi" style="--kpi-accent:#7c3aed; --kpi-color:#7c3aed;">
    <div class="label">Qualified</div>
    <div class="value">{g_qu}</div>
    <div class="kpi-sub">{pct(g_qu, g_bo)} qual rate</div>
  </div>
  <div class="kpi" style="--kpi-accent:#d97706; --kpi-color:#d97706;">
    <div class="label">Closed Won</div>
    <div class="value">{g_cl}</div>
    <div class="kpi-sub">{pct(g_cl, g_bo)} booked→close · {pct(g_cl, g_qu)} qual→close</div>
  </div>
  <div class="kpi" style="--kpi-accent:#0e9f6e; --kpi-color:#0e9f6e;">
    <div class="label">Closed Revenue</div>
    <div class="value">{fmt_currency(g_rev)}</div>
    <div class="kpi-sub">{rev_per_close(g_rev, g_cl)} avg deal</div>
    <div class="kpi-cash {'partial' if cash_incomplete else ''}">{cash_note}</div>
  </div>
</div>

<!-- Funnel Table -->
<div class="section-label">Funnel Breakdown — Setter → Closer → Showed → Qualified → Closed Won → Revenue</div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th class="col-name">Funnel</th>
        <th class="col-num">Leads</th>
        <th class="col-num" title="Discovery calls held this period, converted or not">Setter</th>
        <th class="col-num" title="Discovery calls with no closer call booked at all">Stuck</th>
        <th class="col-pct" title="Share of discovery calls that reached a closer">Adv %</th>
        <th class="col-num" title="First sales calls with a closer">Closer</th>
        <th class="col-pace">Projected</th>
        <th class="col-goal">Goal %</th>
        <th class="col-num">Showed</th>
        <th class="col-pct">Show %</th>
        <th class="col-num">Qualified</th>
        <th class="col-pct">Qual %</th>
        <th class="col-num">Closed</th>
        <th class="col-pct">CW %</th>
        <th class="col-rev">Revenue</th>
        <th class="col-num">Rev / Close</th>
      </tr>
    </thead>
    <tbody>
{funnel_rows}

    <tr class="total-row">
      <td class="col-name">TOTAL</td>
      <td class="col-num">{g_lc if g_lc else "—"}</td>
      <td class="col-num col-setter">{g_se}</td>
      <td class="col-num col-stuck">{g_stk}</td>
      <td class="col-pct {advance_class(g_se, g_stk)}">{advance_pct(g_se, g_stk)}</td>
      <td class="col-num">{g_bo}</td>
      <td class="col-pace">—</td>
      <td class="col-goal">—</td>
      <td class="col-num">{g_sh}</td>
      <td class="col-pct {pct_class(g_sh, g_bo)}">{pct(g_sh, g_bo)}</td>
      <td class="col-num">{g_qu}</td>
      <td class="col-pct {pct_class(g_qu, g_bo)}">{pct(g_qu, g_bo)}</td>
      <td class="col-num">{g_cl}</td>
      <td class="col-pct {pct_class(g_cl, g_bo, high=0.15, low=0.07)}">{pct(g_cl, g_bo)}</td>
      <td class="col-rev">{fmt_currency(g_rev)}</td>
      <td class="col-num">{rev_per_close(g_rev, g_cl)}</td>
    </tr>
    </tbody>
  </table>
</div>

<!-- Closed-Won Lead Detail -->
<div class="section-label">
  Closed-Won Deal Line Items
  <span class="count-pill">{lead_count_label}</span>
</div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th class="col-date">Date</th>
        <th class="col-client">Client</th>
        <th class="col-email">Email</th>
        <th class="col-program">Program</th>
        <th class="col-funnel">Funnel</th>
        <th class="col-closer">Closer</th>
        <th class="col-gross">Gross</th>
        <th class="col-cash">Cash Collected</th>
      </tr>
    </thead>
    <tbody>
{lead_rows}

    <tr class="total-row">
      <td class="col-date">TOTAL</td>
      <td class="col-client"></td>
      <td class="col-email"></td>
      <td class="col-program"></td>
      <td class="col-funnel"></td>
      <td class="col-closer"></td>
      <td class="col-gross">{fmt_currency(g_rev)}</td>
      <td class="col-cash">{fmt_currency(g_cash)}<div style="font-size:10px;font-weight:400;color:var(--muted);">{cash_n} of {cash_of} deals</div></td>
    </tr>
    </tbody>
  </table>
</div>

<script src="{REPO_BASE}/archives/picker.js"></script>
<script>
  function togglePkg(fid) {{
    const pkgRows = document.querySelectorAll(`.pkg-row[data-parent="${{fid}}"]`);
    const chevron = document.getElementById("pkgchev-" + fid);
    const isOpen  = chevron && chevron.classList.contains("open");
    pkgRows.forEach(r => {{
      r.style.display = isOpen ? "none" : "table-row";
      r.classList.toggle("open", !isOpen);
    }});
    if (chevron) chevron.classList.toggle("open", !isOpen);
  }}

  function toggleUTM(fid) {{
    const utmRows = document.querySelectorAll(`.utm-row[data-parent="${{fid}}"]`);
    const chevron = document.getElementById("chev-" + fid);
    const isOpen  = chevron.classList.contains("open");
    utmRows.forEach(r => r.classList.toggle("open", !isOpen));
    chevron.classList.toggle("open", !isOpen);
  }}
</script>

<div style="padding: 24px 36px 32px; border-top: 1px solid var(--border); margin-top: 8px;">
  <p style="font-size: 11px; color: var(--muted); line-height: 1.7; max-width: 680px;">
    <strong style="color: var(--muted2);">Scope</strong> — This dashboard covers only the five agency-managed funnels
    (Instagram, X, Linkedin, Anthony X, Anthony IG). No other funnel data is fetched or published.
    &nbsp;·&nbsp;
    <strong style="color: var(--muted2);">Setter / Closer</strong> — two independent counts, not a split.
    <em>Setter</em> is discovery calls held this period; leads that score below the bar are vetted by a setter
    before a closer's calendar opens up. <em>Closer</em> is first sales calls. A lead with a discovery on the 19th
    and a sales call on the 22nd is counted once in each — they are two separate calls, so the columns do not add up.
    &nbsp;·&nbsp;
    <strong style="color: var(--muted2);">Stuck</strong> — discovery calls that have produced no closer call at all.
    A discovery held in the last few days may simply not have converted yet. Re-running an archive refreshes this.
    &nbsp;·&nbsp;
    <strong style="color: var(--muted2);">Show %, Qual %, CW %</strong> are all measured against
    <em>Closer</em>, never Setter.
    &nbsp;·&nbsp;
    <strong style="color: var(--muted2);">Projected</strong> — End-of-month estimate based on current daily booking pace:
    <em>(Booked ÷ Days Elapsed) × Days in Month</em>.
    <span style="color: var(--green); font-weight:600;">Green</span> = exceeding pace ·
    <span style="color: #ca8a04; font-weight:600;">Yellow</span> = on pace ·
    <span style="color: var(--red); font-weight:600;">Red</span> = behind pace.
    Funnels without a goal show —.
    &nbsp;·&nbsp;
    <strong style="color: var(--muted2);">Cash Collected</strong> — entered manually in Close.
    A dash means not yet recorded, not zero. Cash may exceed gross when payments land from
    deals closed in a prior period.
    &nbsp;·&nbsp;
    <strong style="color: var(--muted2);">Email</strong> — partially masked.
  </p>
</div>

</body>
</html>"""


# ── Archive Helpers ────────────────────────────────────────────────────────────

ARCHIVES_DIR = Path("archives")


def scan_monthly_archives():
    ARCHIVES_DIR.mkdir(exist_ok=True)
    months = []
    for p in sorted(ARCHIVES_DIR.glob("*.html"), reverse=True):
        key = p.stem
        try:
            d = datetime.strptime(key, "%Y-%m")
            months.append((key, d.strftime("%B %Y")))
        except ValueError:
            continue
    return months


def scan_weekly_archives(month_key):
    ARCHIVES_DIR.mkdir(exist_ok=True)
    weeks = []
    for p in sorted(ARCHIVES_DIR.glob("week-20*.html"), reverse=True):
        key = p.stem
        try:
            monday = datetime.strptime(key, "week-%Y-%m-%d").date()
        except ValueError:
            continue
        if monday.strftime("%Y-%m") == month_key:
            sunday = monday + timedelta(days=6)
            weeks.append((key, week_display_label(monday, sunday), monday))
    return weeks


def write_nav_json(live_month, archive_months):
    now_pac    = datetime.now(PACIFIC)
    live_label = now_pac.strftime("%B %Y")

    months = [{"key": live_month, "label": live_label, "is_live": True}]
    for key, label in archive_months:
        if key != live_month:
            months.append({"key": key, "label": label, "is_live": False})

    weeks = {}
    for p in sorted(ARCHIVES_DIR.glob("week-20*.html"), reverse=True):
        key = p.stem
        try:
            monday = datetime.strptime(key, "week-%Y-%m-%d").date()
        except ValueError:
            continue
        month_key = monday.strftime("%Y-%m")
        sunday    = monday + timedelta(days=6)
        weeks.setdefault(month_key, []).append({
            "key":        key,
            "label":      week_display_label(monday, sunday),
            "is_current": False,
        })

    monday    = current_week_monday()
    sunday    = monday + timedelta(days=6)
    cur_label = week_display_label(monday, min(sunday, now_pac.date())) + " ▶"
    weeks.setdefault(live_month, []).append({
        "key": "week-current", "label": cur_label, "is_current": True,
    })

    nav = {
        "live_month":       live_month,
        "live_month_label": live_label,
        "months":           months,
        "weeks":            weeks,
        "updated_at":       now_pac.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    nav_path = ARCHIVES_DIR / "nav.json"
    with open(nav_path, "w") as f:
        json.dump(nav, f, indent=2)
    print(f"Written: {nav_path}", flush=True)


def save_data_json(data, month_key):
    """
    Aggregate-only export. Deliberately excludes closed_leads — no names or
    emails are written to this file, even masked.
    """
    ARCHIVES_DIR.mkdir(exist_ok=True)
    export = {
        "month_key":      month_key,
        "month_label":    data["month_label"],
        "grand":          data["grand"],
        "cash_collected": data.get("grand_cash", 0.0),
        "cash_filled":    data.get("cash_filled", 0),
        "cash_total_n":   data.get("cash_total_n", 0),
        "funnels":        {},
    }
    for funnel, totals in data["funnel_totals"].items():
        bo  = totals.get("booked", 0)
        se  = totals.get("setter", 0)
        stk = totals.get("stuck", 0)
        sh  = totals.get("showed", 0)
        qu  = totals.get("qualified", 0)
        cl  = totals.get("closed", 0)
        rev = totals.get("revenue", 0.0)
        lc  = totals.get("leads_created", 0)
        export["funnels"][funnel] = {
            "leads_created": lc,
            "setter":    se,
            "stuck":     stk,
            "advance_pct": round((se - stk) / se * 100, 1) if se else 0,
            "booked":    bo,
            "book_pct":  round(bo / lc * 100, 1) if lc else 0,
            "showed":    sh,
            "show_pct":  round(sh / bo * 100, 1) if bo else 0,
            "qualified": qu,
            "qual_pct":  round(qu / bo * 100, 1) if bo else 0,
            "closed":    cl,
            "cw_pct":    round(cl / bo * 100, 1) if bo else 0,
            "revenue":   rev,
        }
    path = ARCHIVES_DIR / f"data-{month_key}.json"
    with open(path, "w") as f:
        json.dump(export, f, indent=2)
    print(f"Written: {path}", flush=True)


def write_picker_js():
    ARCHIVES_DIR.mkdir(exist_ok=True)
    js = r"""
// Dynamic nav picker v3 — loaded externally so all archive pages stay current
(async function() {
  const BASE = '__REPO_BASE__';
  try {
    const r = await fetch(BASE + '/archives/nav.json?t=' + Date.now());
    if (!r.ok) return;
    const nav = await r.json();
    const path = window.location.pathname;

    let curMonth = nav.live_month;
    let curWeek  = null;
    const mMatch = path.match(/archives\/(\d{4}-\d{2})\.html/);
    const wMatch = path.match(/archives\/(week-[\d-]+)\.html/);
    const wCur   = path.includes('week-current.html');

    if (mMatch)      { curMonth = mMatch[1]; }
    else if (wMatch) { curWeek = wMatch[1]; curMonth = wMatch[1].replace('week-','').substring(0,7); }
    else if (wCur)   { curWeek = 'week-current'; curMonth = nav.live_month; }

    const mSel = document.querySelector('.month-picker select');
    if (mSel) {
      const curLabel = (nav.months.find(m => m.key === curMonth) || {}).label || 'Select month';
      let opts = `<option value="" disabled selected>${curLabel}</option>`;
      opts += nav.months.map(m => {
        const href = m.is_live ? BASE+'/index.html' : BASE+'/archives/'+m.key+'.html';
        return `<option value="${href}">${m.label}</option>`;
      }).join('');
      mSel.innerHTML = opts;
      mSel.onchange = function() { if (this.value) window.location.href = this.value; };
    }

    const wSel = document.querySelector('.week-picker select');
    if (wSel) {
      const weeks  = nav.weeks[curMonth] || [];
      const isLive = curMonth === nav.live_month;
      const fullHref = isLive ? BASE+'/index.html' : BASE+'/archives/'+curMonth+'.html';

      const opts = [`<option value="${fullHref}">Full Month</option>`];
      weeks.forEach(w => {
        opts.push(`<option value="${BASE+'/archives/'+w.key+'.html'}">${w.label}</option>`);
      });
      wSel.innerHTML = opts.join('');

      const curWkLabel = curWeek
        ? (weeks.find(w => w.key === curWeek) || {}).label || 'This week'
        : 'Full Month';
      wSel.insertAdjacentHTML('afterbegin', `<option value="" disabled selected>${curWkLabel}</option>`);
      wSel.querySelectorAll('option:not([disabled])').forEach(o => o.removeAttribute('selected'));
      wSel.onchange = function() { if (this.value) window.location.href = this.value; };

      if (weeks.length === 0) {
        const wp  = document.querySelector('.week-picker');
        const div = document.querySelector('.picker-divider');
        if (wp)  wp.style.display  = 'none';
        if (div) div.style.display = 'none';
      }
    }
  } catch(e) {
    // Silently fail — baked-in picker remains as fallback
  }
})();
"""
    js = js.replace("__REPO_BASE__", REPO_BASE)
    path = ARCHIVES_DIR / "picker.js"
    with open(path, "w") as f:
        f.write(js.strip())
    print(f"Written: {path}", flush=True)


def build_month_picker(current_month_key, archive_months, is_in_archives):
    now_pac    = datetime.now(PACIFIC)
    live_key   = now_pac.strftime("%Y-%m")
    live_label = now_pac.strftime("%B %Y")

    options = [(live_key, live_label, f"{REPO_BASE}/index.html")]
    for key, label in archive_months:
        if key == live_key:
            continue
        options.append((key, label, f"{REPO_BASE}/archives/{key}.html"))

    select_opts = ""
    for key, label, href in options:
        sel = "selected" if key == current_month_key else ""
        select_opts += f'<option value="{href}" {sel}>{label}</option>\n      '

    return ('<div class="month-picker">'
            '<select onchange="window.location.href=this.value">'
            + select_opts + "</select></div>")


def build_week_picker(current_week_key, month_key, weekly_archives,
                      is_in_archives, is_current_month):
    if month_key < WEEKLY_FEATURE_START:
        return ""

    now_pac = datetime.now(PACIFIC)
    monday  = current_week_monday()
    sunday  = monday + timedelta(days=6)

    if is_in_archives and not is_current_month:
        full_month_href = f"{REPO_BASE}/archives/{month_key}.html"
    else:
        full_month_href = f"{REPO_BASE}/index.html"

    options = []
    sel = "selected" if current_week_key is None else ""
    options.append(f'<option value="{full_month_href}" {sel}>Full Month</option>')

    for key, label, wmonday in weekly_archives:
        href = f"{REPO_BASE}/archives/{key}.html"
        sel  = "selected" if current_week_key == key else ""
        options.append(f'<option value="{href}" {sel}>{label}</option>')

    if is_current_month:
        cur_label = week_display_label(monday, min(sunday, now_pac.date())) + " ▶"
        href = f"{REPO_BASE}/archives/week-current.html"
        sel  = "selected" if current_week_key == "week-current" else ""
        options.append(f'<option value="{href}" {sel}>{cur_label}</option>')

    select_opts = "\n      ".join(options)
    return ('<span class="picker-divider">|</span>'
            '<div class="week-picker">'
            '<select onchange="window.location.href=this.value">'
            + select_opts + "</select></div>")


def write_dashboard(data, out_path, month_picker_html, week_picker_html,
                    is_archive_page, is_week_page):
    badge = ""
    if is_week_page:
        badge = '<span class="archive-badge">Week View</span>'
    elif is_archive_page:
        badge = '<span class="archive-badge">Archive</span>'
    data["badge_html"] = badge
    html = generate_html(data, month_picker_html=month_picker_html,
                         week_picker_html=week_picker_html)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Written: {out_path}", flush=True)


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agency Funnel Performance Dashboard")
    parser.add_argument("--month", "-m", help="Archive month YYYY-MM", default=None)
    parser.add_argument("--week",  "-w", help="Archive week YYYY-MM-DD (Monday)", default=None)
    args = parser.parse_args()

    now_pac    = datetime.now(PACIFIC)
    live_month = now_pac.strftime("%Y-%m")

    print("Agency Funnel Performance Dashboard — Build Start", flush=True)
    print(f"  Scope: {', '.join(ALLOWED_FUNNELS)}", flush=True)

    users = fetch_users()
    lead_cache, contact_cache = {}, {}

    ARCHIVES_DIR.mkdir(exist_ok=True)
    archive_months = scan_monthly_archives()

    # ── MODE: Monthly archive ─────────────────────────────────────────────────
    if args.month:
        try:
            parsed  = datetime.strptime(args.month, "%Y-%m")
            m_start = date(parsed.year, parsed.month, 1)
            last_d  = calendar.monthrange(parsed.year, parsed.month)[1]
            m_end   = date(parsed.year, parsed.month, last_d)
        except ValueError:
            print(f"ERROR: --month must be YYYY-MM, got: {args.month}", flush=True)
            sys.exit(1)

        print(f"\n=== Building monthly archive: {args.month} ===", flush=True)
        won_opps = fetch_won_opps_by_range(m_start, m_end)
        data, lead_cache, contact_cache = aggregate_data(
            m_start, m_end, parsed.strftime("%B %Y"),
            won_opps, users, lead_cache, contact_cache)

        out_path     = ARCHIVES_DIR / f"{args.month}.html"
        weekly_arcs  = scan_weekly_archives(args.month)
        month_picker = build_month_picker(args.month, archive_months, is_in_archives=True)
        week_picker  = build_week_picker(None, args.month, weekly_arcs,
                                         is_in_archives=True,
                                         is_current_month=(args.month == live_month))
        write_dashboard(data, out_path, month_picker, week_picker,
                        is_archive_page=True, is_week_page=False)
        save_data_json(data, args.month)

    # ── MODE: Weekly archive ──────────────────────────────────────────────────
    elif args.week:
        try:
            w_monday = datetime.strptime(args.week, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: --week must be YYYY-MM-DD, got: {args.week}", flush=True)
            sys.exit(1)
        w_sunday  = w_monday + timedelta(days=6)
        w_end     = min(w_sunday, now_pac.date())
        month_key = w_monday.strftime("%Y-%m")
        label     = f"{w_monday.strftime('%B %Y')} · {week_display_label(w_monday, w_sunday)}"

        print(f"\n=== Building weekly archive: {args.week} ===", flush=True)
        won_opps = fetch_won_opps_by_range(w_monday, w_end)
        data, lead_cache, contact_cache = aggregate_data(
            w_monday, w_end, label, won_opps, users, lead_cache, contact_cache)
        data["week_range_label"] = ""

        out_path     = ARCHIVES_DIR / f"week-{args.week}.html"
        weekly_arcs  = scan_weekly_archives(month_key)
        week_key     = f"week-{args.week}"
        month_picker = build_month_picker(month_key, archive_months, is_in_archives=True)
        week_picker  = build_week_picker(week_key, month_key, weekly_arcs,
                                         is_in_archives=True,
                                         is_current_month=(month_key == live_month))
        write_dashboard(data, out_path, month_picker, week_picker,
                        is_archive_page=True, is_week_page=True)

    # ── MODE: Regular live run ────────────────────────────────────────────────
    else:
        m_start  = date(now_pac.year, now_pac.month, 1)
        m_end    = now_pac.date()
        m_label  = now_pac.strftime("%B %Y")
        w_monday = current_week_monday()
        w_end    = now_pac.date()
        w_sunday = w_monday + timedelta(days=6)

        print(f"\n=== Building live month: {m_label} ===", flush=True)
        won_month = fetch_won_opps_by_range(m_start, m_end)
        data_month, lead_cache, contact_cache = aggregate_data(
            m_start, m_end, m_label, won_month, users, lead_cache, contact_cache)
        data_month["week_range_label"] = ""

        weekly_arcs  = scan_weekly_archives(live_month)
        month_picker = build_month_picker(live_month, archive_months, is_in_archives=False)
        week_picker  = build_week_picker(None, live_month, weekly_arcs,
                                         is_in_archives=False, is_current_month=True)
        write_dashboard(data_month, Path("index.html"), month_picker, week_picker,
                        is_archive_page=False, is_week_page=False)
        save_data_json(data_month, live_month)

        print(f"\n=== Building current week: {week_display_label(w_monday, w_end)} ===", flush=True)
        won_week = fetch_won_opps_by_range(w_monday, w_end)
        w_label  = f"{m_label} · {week_display_label(w_monday, w_sunday)}"
        data_week, lead_cache, contact_cache = aggregate_data(
            w_monday, w_end, w_label, won_week, users, lead_cache, contact_cache)
        data_week["week_range_label"] = ""

        week_picker_cur  = build_week_picker("week-current", live_month, weekly_arcs,
                                             is_in_archives=True, is_current_month=True)
        month_picker_cur = build_month_picker(live_month, archive_months, is_in_archives=True)
        write_dashboard(data_week, ARCHIVES_DIR / "week-current.html",
                        month_picker_cur, week_picker_cur,
                        is_archive_page=False, is_week_page=True)

    # ── Always refresh nav + picker ───────────────────────────────────────────
    archive_months = scan_monthly_archives()
    write_nav_json(live_month, archive_months)
    write_picker_js()

    # ── Summary ───────────────────────────────────────────────────────────────
    final_data = data_month if not (args.month or args.week) else data
    g = final_data["grand"]
    print(f"\n=== Build Summary ===", flush=True)
    print(f"  Period:    {final_data['month_label']}", flush=True)
    print(f"  Setter:    {g.get('setter',0)}  "
          f"({g.get('stuck',0)} stuck, {advance_pct(g.get('setter',0), g.get('stuck',0))} advanced)", flush=True)
    print(f"  Closer:    {g['booked']}", flush=True)
    print(f"  Showed:    {g['showed']}  ({pct(g['showed'], g['booked'])})", flush=True)
    print(f"  Qualified: {g['qualified']}  ({pct(g['qualified'], g['booked'])})", flush=True)
    print(f"  Closed:    {g['closed']}  ({pct(g['closed'], g['booked'])})", flush=True)
    print(f"  Revenue:   {fmt_currency(g['revenue'])}", flush=True)
    print(f"  Cash:      {fmt_currency(final_data.get('grand_cash', 0))} "
          f"({final_data.get('cash_filled',0)} of {final_data.get('cash_total_n',0)} deals)", flush=True)
