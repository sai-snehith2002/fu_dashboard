"""
data_pull.py
============

Pulls the FollowUp dashboard data (Metabase Card ID 5360) via the plain
Metabase card-query API (/api/card/{id}/query — not the CSV/JSON export
endpoints), applies the same filters visible in the Metabase UI (End Date,
Cohort From, City, Cluster, Tl, Start Date), and normalises the resulting
DataFrame so it is ready to hand to a Streamlit app.

SETUP
-----
1. Open config.toml (next to this file) and paste your Metabase API key
   into the `api_key` field. Adjust url / card_id / filters as needed.
   Do NOT commit config.toml to version control if this folder is a git
   repo -- add it to .gitignore.

2. Install dependencies:

       pip install requests pandas
       pip install tomli   # only needed if your Python is older than 3.11

USAGE
-----
    python data_pull.py --output followup_dashboard.csv

Filters and connection details are read from config.toml by default. Any
of them can be overridden on the command line without touching the file,
e.g.:

    python data_pull.py --city Pune --end-date 2026-09-20

This module also exposes `load_followup_dashboard_data(...)` so the
Streamlit app can just do:

    from data_pull import load_followup_dashboard_data
    df, column_labels = load_followup_dashboard_data(
        cohort_from="2026-07-31", start_date="2026-09-01", end_date="2026-09-18",
    )
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # pip install tomli

IST = ZoneInfo("Asia/Kolkata")

CONFIG_PATH_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")
REQUEST_TIMEOUT_SEC = 180


# ---------------------------------------------------------------------------
# Config loading (config.toml)
# ---------------------------------------------------------------------------

def load_config(config_path: str = CONFIG_PATH_DEFAULT) -> dict:
    """
    Reads config.toml (if present) and returns a flat dict:
        {
          "url": ..., "api_key": ..., "card_id": ...,
          "cohort_from": ..., "start_date": ..., "end_date": ...,
          "city": ..., "cluster": ..., "tl": ...,
        }
    Empty-string filter values are treated as "not set".

    config.toml is now OPTIONAL, so this also works in CI (e.g. a scheduled
    GitHub Actions job) where no config.toml is checked out at all --
    connection details fall back to environment variables:
      - MB_API_KEY   overrides/supplies [metabase].api_key (this is the only
        one CI actually needs to set, normally from a repo secret -- the
        API key should never be committed to config.toml or the repo).
      - MB_URL       overrides [metabase].url
      - MB_CARD_ID   overrides [metabase].card_id
    Date/city/cluster/tl filters aren't read from the environment -- pass
    those as CLI flags instead (--cohort-from/--start-date/--end-date/etc.,
    see _parse_args()), since a scheduled job typically wants to compute
    --end-date fresh each run rather than have it baked into env vars.
    """
    raw: dict = {}
    if os.path.exists(config_path):
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    elif "MB_API_KEY" not in os.environ:
        raise FileNotFoundError(
            f"Could not find {config_path}, and no MB_API_KEY environment variable is "
            "set either. Copy config.toml next to data_pull.py and paste your Metabase "
            "API key into it, or set MB_API_KEY (e.g. a CI secret)."
        )

    mb = raw.get("metabase", {})
    filt = raw.get("filters", {})

    api_key = os.environ.get("MB_API_KEY") or mb.get("api_key", "")
    if not api_key or api_key.strip().upper() == "REPLACE_WITH_YOUR_API_KEY":
        raise RuntimeError(
            f"No real Metabase API key found (config.toml gave: {mb.get('api_key', '')!r}, "
            "MB_API_KEY env var not set). Open config.toml and paste your Metabase API key "
            "into the [metabase] api_key field, or set the MB_API_KEY environment variable."
        )

    return {
        "url": os.environ.get("MB_URL") or mb.get("url", "https://metabase-lighthouse.solarsquare.in"),
        "api_key": api_key,
        "card_id": int(os.environ.get("MB_CARD_ID") or mb.get("card_id", 5360)),
        "cohort_from": filt.get("cohort_from", "") or None,
        "start_date": filt.get("start_date", "") or None,
        "end_date": filt.get("end_date", "") or None,
        "city": filt.get("city", "") or None,
        "cluster": filt.get("cluster", "") or None,
        "tl": filt.get("tl", "") or None,
    }


# ---------------------------------------------------------------------------
# Normalisation helpers
# (adapted from the Build.js-mirroring helpers you shared; see notes below
#  each function for where this version intentionally diverges)
# ---------------------------------------------------------------------------

_ISO_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DMY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})")


def today_ist_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def normalize_date(v):
    """Returns 'yyyy-mm-dd' string, or '' — same as your reference version."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return ""
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return ""
    m = _ISO_PREFIX.match(s)
    if m:
        return m.group(0)
    m = _DMY.match(s)
    if m:
        # assumes dd/mm/yyyy, exactly like your reference normalizeDate_()
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    try:
        d = pd.to_datetime(s, errors="coerce")
        if pd.isna(d):
            return ""
        return d.strftime("%Y-%m-%d")
    except Exception:
        return ""


def normalize_datetime_ist(v):
    """
    Returns an ISO 'yyyy-mm-dd HH:MM:SS' string in IST, or '' — used for the
    two columns that carry a meaningful time-of-day (order_closure_datetime_ist,
    note_submitted_at_ist). normalize_date() alone would silently drop the
    time component for these, which loses information the dashboard needs.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return ""
    try:
        d = pd.to_datetime(v, errors="coerce")
        if pd.isna(d):
            return ""
        if d.tzinfo is None:
            d = d.tz_localize(IST)
        else:
            d = d.tz_convert(IST)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def to_bool(v):
    """
    Returns a real Python bool (or None) — NOT the 'TRUE'/'FALSE' string your
    Build.js reference used. That string convention exists because Build.js
    writes into Google Sheets, where cell text is the natural format.
    Here the destination is a pandas DataFrame feeding a Streamlit app,
    where a native bool dtype is what lets st.checkbox / boolean filters
    and groupby aggregations work without a string comparison. Flagging
    this as a deliberate divergence from your reference, not an oversight.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().upper()
    if s in ("TRUE", "T", "1", "YES"):
        return True
    if s in ("FALSE", "F", "0", "NO"):
        return False
    return None


def to_num(v):
    """float or None — mirrors your reference num_()."""
    if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def blank_to(v, fallback: str) -> str:
    s = to_str(v)
    return s if s else fallback


# ---------------------------------------------------------------------------
# Column groups — which normaliser applies to which output column.
# Keyed on the exact column labels the SQL query's SELECT aliases produce.
# ---------------------------------------------------------------------------

DATE_ONLY_COLUMNS = [
    "first_meeting_done_date",
    "meeting_date",
    "meeting_done_date",
    "last_follow_up_date",
    "next_follow_up_date",
    "followup_due_date",
    "snapshot_date",
    "fu1_due_date", "fu1_completed_at",
    "fu2_due_date", "fu2_completed_at",
    "fu3_due_date", "fu3_completed_at",
    "fu4_due_date", "fu4_completed_at",
    "fu5_due_date", "fu5_completed_at",
    "fu6_due_date", "fu6_completed_at",
    "latest_due_date", "latest_completed_at",
]

# These two carry a time-of-day component worth keeping.
DATETIME_IST_COLUMNS = [
    "order_closure_datetime_ist",
    "note_submitted_at_ist",
]

BOOL_COLUMNS = [
    "is_field_sale_sc",
    "tl_resolved",
    "is_on_spot",
    "in_review_cohort",
    "audio_present",
    "note_submitted",
    "clean_note",
]

NUMERIC_COLUMNS = [
    "meetings_in_range",
    "notes_submitted_in_range",
    "dispositions_missing",
    "audio_duration_sec",
    "followup_tasks",
    "total_followup_done",
    "followups_done_in_range",
    "followups_done_overdue",
    "tl_is_dnp_tasks",
    "overdue_days",
    "fu1_overdue", "fu2_overdue", "fu3_overdue",
    "fu4_overdue", "fu5_overdue", "fu6_overdue",
    "latest_overdue",
    "fu_completedat_today",
    "fu_due_date_today",
]

# Everything else in the query output is treated as a plain trimmed string
# (lead_id, names, statuses, outcomes, voice-note Q&A text, etc.) — handled
# by the fallback branch in normalize_dataframe().


def slugify_column(col: str) -> str:
    """
    'Sales Channel (FS/IS)' -> 'sales_channel_fs_is'
    "SC's Reading"          -> 'scs_reading'
    'Site Visit Done?'      -> 'site_visit_done'
    Plain snake_case columns pass through unchanged.
    """
    s = col.strip().lower()
    s = s.replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies date / datetime / bool / numeric / string normalisation
    column-by-column, based on the groups above. Returns a new DataFrame;
    does not mutate the input in place.
    """
    out = df.copy()

    for col in out.columns:
        if col in DATE_ONLY_COLUMNS:
            out[col] = out[col].map(normalize_date)
        elif col in DATETIME_IST_COLUMNS:
            out[col] = out[col].map(normalize_datetime_ist)
        elif col in BOOL_COLUMNS:
            out[col] = out[col].map(to_bool).astype("boolean")  # pandas nullable bool
        elif col in NUMERIC_COLUMNS:
            out[col] = out[col].map(to_num)
        else:
            out[col] = out[col].map(to_str)

    return out


# ---------------------------------------------------------------------------
# Metabase fetch — plain /api/card/{id}/query endpoint
# ---------------------------------------------------------------------------

# Which of the card's own parameters (identified by the template-tag name
# inside their "target") we know how to fill in, and from what value.
_KNOWN_TAG_NAMES = {"cohort_from", "start_date", "end_date", "city", "cluster", "tl"}


def fetch_card_parameter_defs(
    card_id: int,
    base_url: str,
    api_key: str,
    timeout: int = REQUEST_TIMEOUT_SEC,
) -> list[dict]:
    """
    GET /api/card/{card_id} and return its 'parameters' list.

    Why this step exists: guessing our own "id" for each parameter (e.g.
    "cohort_from") is what caused the "missing required parameters" error —
    Metabase's query processor matches incoming parameters against the
    card's OWN registered parameter definitions (each with an id Metabase
    assigned when the filter widgets were configured, e.g. a short hash,
    not the template-tag name itself). The only reliable way to supply a
    value that actually binds is to fetch those real definitions and just
    attach "value" to each one — not to reconstruct them from scratch.
    """
    url = f"{base_url.rstrip('/')}/api/card/{card_id}"
    resp = requests.get(url, headers={"x-api-key": api_key}, timeout=timeout)

    # Metabase can return 200 or 202 for a successful response depending on
    # version/endpoint — only >=400 is an actual failure.
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Failed to fetch card definition [{resp.status_code}]: {resp.text[:1000]}"
        )

    body = resp.json()
    param_defs = body.get("parameters") or []

    if not param_defs:
        raise RuntimeError(
            f"Card {card_id} has no 'parameters' in its metadata. Its "
            "template tags may not have filter widgets configured, or "
            "this API key may lack permission to see them."
        )

    return param_defs


def _template_tag_name(param_def: dict) -> str | None:
    """
    Pulls the tag name (e.g. 'cohort_from') out of a parameter definition's
    target, which looks like ["variable", ["template-tag", "cohort_from"]].
    Matching on this (rather than the parameter's 'slug' or 'name', which
    could have been renamed in the Metabase UI) is what makes this robust
    to however the filter widgets happen to be labelled.
    """
    target = param_def.get("target")
    try:
        return target[1][1]
    except (TypeError, IndexError, KeyError):
        return None


def build_metabase_parameters(
    param_defs: list[dict],
    cohort_from: str,
    start_date: str,
    end_date: str,
    city: str | None = None,
    cluster: str | None = None,
    tl: str | None = None,
) -> list[dict]:
    """
    Takes the card's own parameter definitions (from fetch_card_parameter_defs)
    and returns them with "value" filled in — id/type/target are copied
    through untouched, exactly as Metabase registered them, so the query
    processor recognises the binding. A tag with no value supplied (the
    optional city/cluster/tl filters, when left as None/empty) is left out
    of the returned list entirely — matching the [[ AND ... ]] optional
    clauses in the SQL: omitted = no filter applied.
    """
    values = {
        "cohort_from": cohort_from,
        "start_date": start_date,
        "end_date": end_date,
        "city": city,
        "cluster": cluster,
        "tl": tl,
    }

    seen_tags = set()
    out = []
    for pdef in param_defs:
        tag = _template_tag_name(pdef)
        seen_tags.add(tag)
        if tag not in values:
            continue
        val = values[tag]
        if val is None or val == "":
            continue
        out.append(
            {
                "id": pdef["id"],
                "type": pdef["type"],
                "target": pdef["target"],
                "value": val,
            }
        )

    required_tags = {"cohort_from", "start_date", "end_date"}
    present_in_output = {_template_tag_name(p) for p in out}
    missing_required = required_tags - present_in_output
    if missing_required:
        print(
            f"[data_pull] Warning: no filter widget found on this card for "
            f"required date tag(s) {missing_required} — the query call will "
            "likely fail with 'missing required parameters'. This means the "
            "card's own metadata (fetch_card_parameter_defs) doesn't list a "
            "parameter targeting that template tag; check the tag actually "
            "has a filter widget attached in Metabase's question editor.",
            file=sys.stderr,
        )

    return out


def fetch_metabase_card(
    card_id: int,
    parameters: list[dict],
    base_url: str,
    api_key: str,
    timeout: int = REQUEST_TIMEOUT_SEC,
) -> pd.DataFrame:
    """
    Calls Metabase's JSON export endpoint:
        POST /api/card/{card_id}/query/json
    which returns a plain list[dict] of rows (column name -> value).

    This endpoint runs in Metabase's "export" query context rather than the
    interactive "question" context, which is what matters here: the plain
    /api/card/{id}/query endpoint enforces a default max-results-bare-rows
    cap of 2000 rows for any non-aggregated result set, and that cap isn't
    reliably overridable via a client-supplied `constraints` value when
    authenticating with an API key (confirmed — a 1,000,000 override still
    came back capped at exactly 2000). The export context does not apply
    that interactive cap, which is the whole reason to use it here.
    """
    url = f"{base_url.rstrip('/')}/api/card/{card_id}/query/json"

    resp = requests.post(
        url,
        headers={"x-api-key": api_key},
        params={"format_rows": "false"},
        data={"parameters": json.dumps(parameters)},
        timeout=timeout,
    )

    # Metabase can return 200 or 202 for a successful response depending on
    # version/endpoint — only >=400 is an actual failure.
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Metabase request failed [{resp.status_code}]: {resp.text[:2000]}"
        )

    try:
        rows = resp.json()
    except ValueError as e:
        raise RuntimeError(
            "Metabase did not return JSON as expected. Response starts with: "
            f"{resp.text[:500]}"
        ) from e

    if isinstance(rows, dict) and rows.get("status") == "failed":
        raise RuntimeError(
            f"Metabase reported the query failed despite HTTP {resp.status_code}: "
            f"{json.dumps(rows)[:2000]}"
        )

    if not isinstance(rows, list):
        raise RuntimeError(
            f"Unexpected response shape from Metabase (expected a list of row "
            f"dicts): {json.dumps(rows)[:1000] if isinstance(rows, dict) else type(rows)}"
        )

    df = pd.DataFrame(rows)

    # Heads-up if the row count looks like it hit a round-number cap —
    # some Metabase instances still enforce a max export row limit
    # (commonly 1,000,000, occasionally lower on self-hosted setups).
    if len(df) in (2000, 100_000, 1_000_000):
        print(
            f"[data_pull] Warning: fetched exactly {len(df)} rows — this is a "
            "common Metabase row-limit boundary. Check with your Metabase "
            "admin if you expect more rows than this.",
            file=sys.stderr,
        )

    return df


# ---------------------------------------------------------------------------
# Public entry point for reuse from Streamlit
# ---------------------------------------------------------------------------

def load_followup_dashboard_data(
    cohort_from: str,
    start_date: str,
    end_date: str,
    city: str | None = None,
    cluster: str | None = None,
    tl: str | None = None,
    config_path: str = CONFIG_PATH_DEFAULT,
    slugify_columns: bool = True,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """
    Fetches + normalises the FollowUp dashboard data in one call. Connection
    details (url, api_key, card_id) always come from config.toml; the
    filter args here override whatever is in config.toml's [filters] block.

    Returns:
        df             — normalised DataFrame, one row per lead_id
        column_labels  — {slug_column_name: original_metabase_label}
                         (only populated when slugify_columns=True; empty
                         dict otherwise, since column names are unchanged)
    """
    cfg = load_config(config_path)

    param_defs = fetch_card_parameter_defs(
        card_id=cfg["card_id"], base_url=cfg["url"], api_key=cfg["api_key"]
    )

    params = build_metabase_parameters(
        param_defs,
        cohort_from=cohort_from,
        start_date=start_date,
        end_date=end_date,
        city=city,
        cluster=cluster,
        tl=tl,
    )

    raw_df = fetch_metabase_card(
        card_id=cfg["card_id"],
        parameters=params,
        base_url=cfg["url"],
        api_key=cfg["api_key"],
    )

    df = normalize_dataframe(raw_df)

    column_labels: dict[str, str] = {}
    if slugify_columns:
        rename_map = {col: slugify_column(col) for col in df.columns}
        column_labels = {slug: original for original, slug in rename_map.items()}
        df = df.rename(columns=rename_map)

    return df, column_labels


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=CONFIG_PATH_DEFAULT, help="Path to config.toml")
    p.add_argument("--cohort-from", default=None, help="Override config.toml's cohort_from (YYYY-MM-DD)")
    p.add_argument("--start-date", default=None, help="Override config.toml's start_date (YYYY-MM-DD)")
    p.add_argument("--end-date", default=None, help="Override config.toml's end_date (YYYY-MM-DD)")
    p.add_argument("--city", default=None, help="Override config.toml's city filter")
    p.add_argument("--cluster", default=None, help="Override config.toml's cluster filter")
    p.add_argument("--tl", default=None, help="Override config.toml's tl filter")
    p.add_argument("--output", default="followup_dashboard.csv", help="Output CSV path")
    p.add_argument(
        "--no-slugify-columns",
        action="store_true",
        help="Keep original Metabase column labels instead of snake_case",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request payload and exit without calling Metabase",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = load_config(args.config)

    cohort_from = args.cohort_from or cfg["cohort_from"]
    start_date = args.start_date or cfg["start_date"]
    end_date = args.end_date or cfg["end_date"]
    city = args.city or cfg["city"]
    cluster = args.cluster or cfg["cluster"]
    tl = args.tl or cfg["tl"]

    if not (cohort_from and start_date and end_date):
        raise SystemExit(
            "cohort_from / start_date / end_date must be set, either in "
            "config.toml's [filters] block or via --cohort-from / --start-date "
            "/ --end-date."
        )

    # Fetching the card's real parameter definitions requires one GET call
    # even for --dry-run, since that's the only way to see the exact ids
    # Metabase will actually accept (see fetch_card_parameter_defs docstring).
    # This call is read-only and does not run the underlying query.
    param_defs = fetch_card_parameter_defs(
        card_id=cfg["card_id"], base_url=cfg["url"], api_key=cfg["api_key"]
    )

    parameters = build_metabase_parameters(
        param_defs,
        cohort_from=cohort_from,
        start_date=start_date,
        end_date=end_date,
        city=city,
        cluster=cluster,
        tl=tl,
    )

    if args.dry_run:
        print(f"GET  {cfg['url']}/api/card/{cfg['card_id']}  (fetched {len(param_defs)} parameter defs)")
        print(f"POST {cfg['url']}/api/card/{cfg['card_id']}/query/json  (not sent — dry run)")
        print(json.dumps({"parameters": parameters}, indent=2))
        return

    df, column_labels = load_followup_dashboard_data(
        cohort_from=cohort_from,
        start_date=start_date,
        end_date=end_date,
        city=city,
        cluster=cluster,
        tl=tl,
        config_path=args.config,
        slugify_columns=not args.no_slugify_columns,
    )

    print(f"[data_pull] Pulled {len(df)} rows, {len(df.columns)} columns.")
    print(f"[data_pull] lead_id unique: {df['lead_id'].nunique() if 'lead_id' in df.columns else 'n/a'}")
    print(df.dtypes)
    print(df.head(3).to_string())

    df.to_csv(args.output, index=False)
    print(f"[data_pull] Saved to {args.output}")

    if column_labels:
        labels_path = os.path.splitext(args.output)[0] + "_column_labels.json"
        with open(labels_path, "w") as f:
            json.dump(column_labels, f, indent=2)
        print(f"[data_pull] Column label mapping saved to {labels_path}")


if __name__ == "__main__":
    main()