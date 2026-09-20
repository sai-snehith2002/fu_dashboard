"""
app.py
======
FollowUp Dashboard — Streamlit build.

Run with:
    streamlit run app.py

Reads the CSV produced by data_pull.py (default: followup_dashboard.csv in
this same folder), re-applies the same normalisation data_pull.py used
(so dtypes survive the CSV round-trip), lets the person pick a city/cluster
from a button-row list in the sidebar (or "Pan-India" for every cluster
combined -- no Python-level hardcoding of which city is shown), and
renders:
  - Card 1 (City snapshot): Meetings done / Closed on spot /
    Eligible for follow-ups / No audio notes
  - Due Today section: completion %, pending + top-pending TL, the
    Agreed+Another / P1+P2 / Others split, and a click-driven
    TL -> SC -> Lead drill-down (click a table row to go one level deeper;
    click the section header to open/close the drill-down itself).

All the actual math lives in metrics.py, kept free of Streamlit calls so it
stays testable on its own.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from data_pull import normalize_dataframe
import metrics as M

CSV_PATH_DEFAULT = os.environ.get("FU_CSV_PATH", "followup_dashboard.csv")
PAN_INDIA = "Pan-India"  # the sidebar's "no cluster filter -- every city combined" option


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Loading data...", ttl=6600)
def load_data(csv_path: str) -> pd.DataFrame:
    """
    Reads everything as string first (keep_default_na=False keeps blanks as
    "" instead of NaN) so normalize_dataframe's own blank/NaN handling is
    what decides the dtype, rather than pandas' own CSV type inference —
    exactly the same approach data_pull.py's tests were verified against.

    ttl=6600 (1h50m) is a safety net for the hosted deployment: the CSV is
    meant to be refreshed every 2 hours by a scheduled job (see
    .github/workflows/refresh_data.yml) that commits the new file and lets
    Streamlit Cloud's auto-redeploy-on-push pick it up, which already clears
    this cache by restarting the process -- this ttl just re-reads the file
    from disk on its own if a redeploy is ever delayed or disabled, staying
    a bit shorter than the 2-hour refresh cadence so it never masks a
    fresh commit.
    """
    raw = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    return normalize_dataframe(raw)


def fmt_pct(x: float) -> str:
    return f"{x:.1f}%"


def render_ageing_tile(container, value: int, pct: float, label: str, color: str) -> None:
    """
    One colored ageing bucket tile (Overdue section): a big count, a small
    inline %, and a label underneath, tinted green/amber/red via the
    .ageing-tile CSS classes defined in the page-level <style> block.
    color must be one of "green", "amber", "red".
    """
    container.markdown(
        f'<div class="ageing-tile ageing-{color}">'
        f'<div class="ageing-value-row">'
        f'<span class="ageing-value">{value:,}</span>'
        f'<span class="ageing-pct">{pct:.0f}%</span>'
        f'</div>'
        f'<div class="ageing-label">{label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Click-to-drill table helper
# ---------------------------------------------------------------------------

def render_drill_table(
    df_display: pd.DataFrame,
    id_series: pd.Series,
    table_key: str,
    column_config=None,
    row_style_fn=None,
):
    """
    Renders `df_display` as a single-row-selectable table. If the person has
    just clicked a row (in this rerun), returns the corresponding value from
    `id_series` (e.g. the TL or SC name for that row) and clears the table's
    own selection state, so the table starts fresh/unselected the next time
    it is rendered (otherwise a stale selection would immediately re-fire the
    same drill the moment someone navigates back to this view).

    column_config: optional dict passed straight through to st.dataframe's
    own column_config (e.g. st.column_config.NumberColumn(help=...)), used
    to add hover tooltips to specific columns.

    row_style_fn: optional function(row) -> list[str] of per-row CSS,
    applied via a pandas Styler (df_display.style.apply(row_style_fn,
    axis=1)) for whole-row conditional coloring. Combining a Styler with
    on_select/selection_mode="single-row" has been verified to work without
    exceptions in this Streamlit version.

    Returns None if nothing was just selected.
    """
    data = df_display
    if row_style_fn is not None:
        data = df_display.style.apply(row_style_fn, axis=1)
    event = st.dataframe(
        data,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=table_key,
        column_config=column_config,
    )
    rows = event.selection.rows if event is not None and event.selection else []
    if rows:
        selected_value = id_series.iloc[rows[0]]
        if table_key in st.session_state:
            del st.session_state[table_key]
        return selected_value
    return None


def clear_table_selection(table_key: str) -> None:
    """Drops a drill table's stored selection so it renders unselected next time."""
    if table_key in st.session_state:
        del st.session_state[table_key]


def _default_tier_row_style(levels: list[str]):
    """Row-style factory: tints TL rows light blue and SC rows light green, by position."""
    def _style(row: pd.Series) -> list[str]:
        lvl = levels[row.name] if row.name < len(levels) else "TL"
        color = "rgba(59, 130, 246, 0.08)" if lvl == "TL" else "rgba(16, 185, 129, 0.10)"
        return [f"background-color: {color}"] * len(row)
    return _style


def render_accordion_drill(
    tl_df: pd.DataFrame,
    tl_id_col: str,
    sc_lookup,
    sc_id_col: str,
    lead_lookup,
    display_cols: dict,
    group_label: str,
    sub_label: str,
    state_key: str,
    column_config=None,
    row_style_fn=None,
):
    """
    A single, growing "accordion" table in place of a TL -> SC -> Lead
    click-to-drill: clicking a TL row inserts its SC rows directly beneath
    it (indented, tinted), and clicking an SC row (within the expanded TL)
    reveals its Lead-level table directly below the accordion. Clicking an
    already-expanded row collapses it back. Only one TL and one SC can be
    expanded at a time, mirroring the single-path drill this replaces.

    tl_df/sc_lookup(tl_name) must return frames sharing the same metric
    columns (just grouped by tl_id_col vs sc_id_col) -- display_cols maps
    those raw metric column names to the display labels shown in the table.

    row_style_fn: optional row_style_fn(row) -> list[str] (e.g. an existing
    metric-based row Styler) used instead of the default TL/SC tier tint --
    for a table that already has its own semantic row coloring.

    Session state keys used: st.session_state[f"{state_key}_tl"] and
    st.session_state[f"{state_key}_sc"].
    """
    tl_key = f"{state_key}_tl"
    sc_key = f"{state_key}_sc"
    if tl_key not in st.session_state:
        st.session_state[tl_key] = None
    if sc_key not in st.session_state:
        st.session_state[sc_key] = None

    expanded_tl = st.session_state[tl_key]
    expanded_sc = st.session_state[sc_key]

    metric_cols = list(display_cols.keys())
    id_col_label = f"{group_label} / {sub_label}"
    display_labels = [id_col_label] + [display_cols[c] for c in metric_cols]

    rows = []
    levels = []
    raw_names = []

    for _, tl_row in tl_df.iterrows():
        tl_name = tl_row[tl_id_col]
        row = {id_col_label: f"🔵 {tl_name}"}
        for c in metric_cols:
            row[display_cols[c]] = tl_row[c]
        rows.append(row)
        levels.append("TL")
        raw_names.append(tl_name)

        if expanded_tl == tl_name:
            sc_df = sc_lookup(tl_name)
            for _, sc_row in sc_df.iterrows():
                sc_name = sc_row[sc_id_col]
                srow = {id_col_label: f"     ↳ 🟢 {sc_name}"}
                for c in metric_cols:
                    srow[display_cols[c]] = sc_row[c]
                rows.append(srow)
                levels.append("SC")
                raw_names.append(sc_name)

    display_df = pd.DataFrame(rows, columns=display_labels)
    style_fn = row_style_fn if row_style_fn is not None else _default_tier_row_style(levels)
    styled = display_df.style.apply(style_fn, axis=1)

    st.caption(f"Click a {group_label} to reveal its {sub_label}s; click an {sub_label} to reveal its leads.")
    table_key = f"{state_key}_accordion_table"
    event = st.dataframe(
        styled, width="stretch", hide_index=True, on_select="rerun",
        selection_mode="single-row", key=table_key, column_config=column_config,
    )
    sel_rows = event.selection.rows if event is not None and event.selection else []
    if sel_rows:
        i = sel_rows[0]
        lvl, name = levels[i], raw_names[i]
        if table_key in st.session_state:
            del st.session_state[table_key]
        if lvl == "TL":
            st.session_state[tl_key] = None if expanded_tl == name else name
            st.session_state[sc_key] = None
        else:
            st.session_state[sc_key] = None if expanded_sc == name else name
        st.rerun()

    if expanded_tl and expanded_sc:
        lead_df = lead_lookup(expanded_tl, expanded_sc)
        st.caption(f"Leads under **{expanded_tl} → {expanded_sc}**")
        st.dataframe(lead_df, width="stretch", hide_index=True)


def audio_index_row_style(row: pd.Series) -> list[str]:
    """
    Whole-row background color for the Audio Index TL/SC tables, keyed off
    the "Index" column: above 8 -> green, 7-8 (inclusive) -> amber, below 7
    -> red. Used as render_drill_table's row_style_fn for those two tables.
    """
    try:
        idx = float(row.get("Index"))
    except (TypeError, ValueError):
        return [""] * len(row)
    if pd.isna(idx):
        return [""] * len(row)
    if idx > 8:
        color = "rgba(46, 139, 87, 0.25)"  # green
    elif idx >= 7:
        color = "rgba(255, 191, 0, 0.25)"  # amber
    else:
        color = "rgba(220, 20, 60, 0.25)"  # red
    return [f"background-color: {color}"] * len(row)


def _fmt_count_pct(n: int, pct: int, dash_if_zero: bool = True) -> str:
    """'10 (50%)', or '—' for a zero count in a per-box cell (not for the Total cells, which always show)."""
    if dash_if_zero and n == 0:
        return "—"
    return f"{int(n)} ({int(pct)}%)"


def build_funnel_group_wise_display(gw: pd.DataFrame, group_col: str, group_label: str) -> pd.DataFrame:
    """
    Turns metrics.funnel_tl_wise_table()/funnel_sc_wise_table()'s flat
    columns into the grouped, two-level-header table from the reference
    layout (Still in play / Closed out spanning several sub-columns each),
    with each box's count and share folded into one "N (X%)" cell --
    st.dataframe can't stack two numbers on separate lines within a cell,
    so this is the closest single-line rendering of the reference's
    two-line count/% cells. group_col/group_label select "tl"/"TL" for the
    top-level view or "sc"/"SC" for the TL -> SC drill-down.
    """
    columns = pd.MultiIndex.from_tuples([
        ("", group_label),
        ("", "Followed up at least once"),
        ("Still in play", "Total"),
        ("Still in play", "Another follow up required"),
        ("Still in play", "DNP"),
        ("Still in play", "Will go later"),
        ("Still in play", "Agreed to meet"),
        ("Closed out", "Total"),
        ("Closed out", "No action possible"),
        ("Closed out", "Lost to competitor"),
        ("Closed out", "Booked"),
    ])
    rows = []
    for _, r in gw.iterrows():
        rows.append([
            r[group_col],
            f"{int(r['followed_up_once']):,}",
            _fmt_count_pct(r["still_in_play_total"], r["still_in_play_total_pct"], dash_if_zero=False),
            _fmt_count_pct(r["another_fu"], r["another_fu_pct"]),
            _fmt_count_pct(r["dnp"], r["dnp_pct"]),
            _fmt_count_pct(r["will_go_later"], r["will_go_later_pct"]),
            _fmt_count_pct(r["agreed_to_meet"], r["agreed_to_meet_pct"]),
            _fmt_count_pct(r["closed_out_total"], r["closed_out_total_pct"], dash_if_zero=False),
            _fmt_count_pct(r["no_action_possible"], r["no_action_possible_pct"]),
            _fmt_count_pct(r["lost_to_competitor"], r["lost_to_competitor_pct"]),
            _fmt_count_pct(r["booked"], r["booked_pct"]),
        ])
    return pd.DataFrame(rows, columns=columns)


def _fmt_pct_or_dash(v) -> str:
    """'43.1%', or '—' when the value is missing (no denominator) or a literal 0%."""
    if pd.isna(v) or v == 0:
        return "—"
    return f"{v:.1f}%"


def build_funnel_quality_overall_display(overall: pd.DataFrame) -> pd.DataFrame:
    """Measure / Now / Threshold / Basis / State, matching the reference layout."""
    rows = []
    for _, r in overall.iterrows():
        rows.append([
            r["measure"],
            f"{r['now_pct']:.1f}%",
            f"{int(r['threshold_pct'])}%",
            f"{r['basis_label']} ({int(r['basis_count'])})",
            r["state"],
        ])
    return pd.DataFrame(rows, columns=["Measure", "Now", "Threshold", "Basis", "State"])


def style_funnel_quality_state(display: pd.DataFrame):
    """Colors the State column red for Breaching, green for Within."""
    def _color(v: str) -> str:
        if v == "Breaching":
            return "color: #d63031; font-weight: 600"
        if v == "Within":
            return "color: #2e8b57; font-weight: 600"
        return ""
    return display.style.map(_color, subset=["State"])


def build_funnel_quality_sc_display(sc_df: pd.DataFrame, category_label: str) -> pd.DataFrame:
    """SC / Leads / <category label>, for one category's expander."""
    return pd.DataFrame({
        "SC": sc_df["sc"],
        "Leads": sc_df["leads"].map(lambda n: f"{int(n):,}"),
        category_label: sc_df["matched_leads"].map(lambda n: f"{int(n):,}"),
    })


def build_funnel_quality_tlwise_display(tlw: pd.DataFrame) -> pd.DataFrame:
    """TL / With an outcome / Did not pick up / + Lost + Nurture / Dated beyond 5 days."""
    rows = []
    for _, r in tlw.iterrows():
        rows.append([
            r["tl"],
            f"{int(r['with_outcome']):,}",
            _fmt_pct_or_dash(r["dnp_pct"]),
            _fmt_pct_or_dash(r["dnp_lost_nurture_pct"]),
            _fmt_pct_or_dash(r["beyond_5_days_pct"]),
        ])
    return pd.DataFrame(
        rows, columns=["TL", "With an outcome", "Did not pick up", "+ Lost + Nurture", "Dated beyond 5 days"]
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="FollowUp Dashboard", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    /* ==== Base typography (applied everywhere) ==== */
    html, body, [class*="css"], .stApp, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
        -webkit-font-smoothing: antialiased;
        color: #0F172A;
    }

    /* ==== Page background & top strip ==== */
    .stApp { background-color: #F5F7FA; }
    header[data-testid="stHeader"] { background: transparent; }
    .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1400px; }

    /* ==== Hide Streamlit-owned chrome for a clean public look ====
       toolbarMode="minimal" in .streamlit/config.toml already kills
       the top-right hamburger / fork / deploy buttons; the rules below
       cover the "Hosted with Streamlit" badge and any residual footer
       decoration that config can't touch. */
    #MainMenu { visibility: hidden !important; }
    footer { visibility: hidden !important; }
    [data-testid="stDeployButton"] { display: none !important; }
    [data-testid="stAppDeployButton"] { display: none !important; }
    [data-testid="stStatusWidget"] { display: none !important; }
    [data-testid="stDecoration"] { display: none !important; }
    /* "Hosted with Streamlit" badge in the bottom-right (class hash
       varies across Streamlit versions, so match by any container that
       lives directly under viewerBadge). */
    a[href*="streamlit.io/cloud"], a[href*="viewerBadge"] { display: none !important; }
    ._container_gzau3_1, ._link_gzau3_10, ._viewerBadge_link__qRIco { display: none !important; }

    /* ==== Dark navy sidebar (mimics the reference dashboard) ==== */
    [data-testid="stSidebar"] {
        background-color: #142542;
        border-right: 1px solid rgba(255, 255, 255, 0.04);
    }
    /* Push sidebar content into a flex column so a spacer can shove nav to the bottom */
    [data-testid="stSidebar"] > div:first-child {
        display: flex;
        flex-direction: column;
        min-height: 100vh;
    }
    [data-testid="stSidebar"] .stMarkdown p,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] .stCaption,
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"],
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        color: #CBD5E1;
    }
    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 { color: #FFFFFF; font-weight: 700; }

    .sb-brand-title {
        color: #FFFFFF;
        font-size: 1.35rem;
        font-weight: 700;
        letter-spacing: -0.01em;
        margin: 0.25rem 0 0.15rem 0;
    }
    .sb-brand-sub {
        color: #94A3B8;
        font-size: 0.85rem;
        font-weight: 400;
        margin-bottom: 1.1rem;
    }
    .sb-divider {
        border: none;
        border-top: 1px solid rgba(255, 255, 255, 0.08);
        margin: 0 0 1.15rem 0;
    }
    .sb-label {
        color: #FFFFFF;
        font-size: 0.85rem;
        font-weight: 500;
        margin-bottom: 0.35rem;
    }
    .sb-hint {
        color: #94A3B8;
        font-size: 0.78rem;
        line-height: 1.4;
        margin-top: 0.7rem;
    }
    .sb-spacer { flex: 1 1 auto; min-height: 1rem; }

    /* Sidebar dropdown: white on dark, matches the reference */
    [data-testid="stSidebar"] [data-baseweb="select"] > div {
        background-color: #FFFFFF !important;
        border-radius: 10px !important;
        border: none !important;
        min-height: 44px;
    }
    [data-testid="stSidebar"] [data-baseweb="select"] > div * { color: #0F172A !important; }
    [data-testid="stSidebar"] [data-baseweb="select"] svg { fill: #64748B !important; }

    /* Sidebar nav pill buttons */
    [data-testid="stSidebar"] .stButton > button {
        background-color: transparent;
        color: #E2E8F0;
        border: 1px solid transparent;
        border-radius: 10px;
        font-weight: 500;
        text-align: left;
        justify-content: flex-start;
        padding: 0.65rem 0.9rem;
        min-height: 44px;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background-color: rgba(255, 255, 255, 0.06);
        color: #FFFFFF;
        border-color: transparent;
    }
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background-color: rgba(59, 130, 246, 0.22);
        color: #FFFFFF;
        border-color: rgba(96, 165, 250, 0.25);
        font-weight: 600;
    }
    [data-testid="stSidebar"] [data-testid="stExpander"] {
        background-color: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
        margin-top: 0.75rem;
    }
    [data-testid="stSidebar"] [data-testid="stExpander"] summary,
    [data-testid="stSidebar"] [data-testid="stExpander"] p { color: #CBD5E1; }
    [data-testid="stSidebar"] [data-testid="stExpander"] input {
        background-color: #FFFFFF !important;
        color: #0F172A !important;
    }
    /* Hide the sidebar's own top spacing so brand can sit near the top */
    [data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }

    /* ==== Page header (eyebrow + big title) ==== */
    .page-eyebrow {
        color: #94A3B8;
        font-size: 0.9rem;
        font-weight: 500;
        margin-bottom: 0.35rem;
    }
    .page-title {
        color: #0F172A;
        font-size: 2.4rem;
        font-weight: 700;
        letter-spacing: -0.015em;
        line-height: 1.1;
        margin: 0 0 0.35rem 0;
    }
    .page-caption {
        color: #64748B;
        font-size: 0.9rem;
        margin-bottom: 1.5rem;
    }

    /* ==== Section cards ==== */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background-color: #FFFFFF;
        border-radius: 14px;
        border: 1px solid #E2E8F0 !important;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
        padding: 1.5rem 1.75rem 1rem 1.75rem !important;
        margin-bottom: 1.25rem;
    }

    /* ==== Section headers (st.header rendered as h2) ==== */
    [data-testid="stVerticalBlockBorderWrapper"] h2,
    .stApp h2 {
        color: #0F172A;
        font-size: 1.4rem;
        font-weight: 700;
        letter-spacing: -0.01em;
        padding: 0 0 0.5rem 0;
    }
    /* Subheaders (st.subheader = h3) */
    .stApp h3 {
        color: #0F172A;
        font-size: 1.05rem;
        font-weight: 600;
    }

    /* ==== Metrics ==== */
    [data-testid="stMetric"] {
        background: transparent;
    }
    [data-testid="stMetricLabel"] {
        color: #64748B;
        font-size: 0.82rem;
        font-weight: 500;
    }
    [data-testid="stMetricLabel"] p {
        font-size: 0.82rem;
        font-weight: 500;
        color: #64748B;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.9rem;
        font-weight: 700;
        color: #0F172A;
        line-height: 1.1;
    }
    [data-testid="stMetricValue"] > div {
        font-size: 1.9rem;
        font-weight: 700;
    }

    /* ==== Captions ==== */
    [data-testid="stCaptionContainer"], .stCaption {
        color: #64748B;
        font-size: 0.82rem;
    }

    /* ==== Divider ==== */
    hr {
        border-top: 1px solid #E2E8F0;
        margin: 1.75rem 0;
    }

    /* ==== Dataframes: cleaner frame ==== */
    [data-testid="stDataFrame"] {
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid #E2E8F0;
    }

    /* ==== Expanders (main content) ==== */
    .stApp [data-testid="stExpander"] {
        border: 1px solid #E2E8F0;
        border-radius: 10px;
        background: #F8FAFC;
    }
    .stApp [data-testid="stExpander"] summary {
        font-weight: 500;
        color: #0F172A;
    }

    /* ==== Ageing gradient tiles (Overdue section) ==== */
    .ageing-tile {
        border-radius: 12px;
        padding: 1rem 1.15rem;
        border: 1px solid transparent;
        display: flex;
        flex-direction: column;
        gap: 0.35rem;
        min-height: 92px;
    }
    .ageing-tile .ageing-value-row {
        display: flex;
        align-items: baseline;
        gap: 0.5rem;
    }
    .ageing-tile .ageing-value {
        font-size: 1.75rem;
        font-weight: 700;
        line-height: 1;
    }
    .ageing-tile .ageing-pct {
        font-size: 0.9rem;
        font-weight: 500;
        opacity: 0.85;
    }
    .ageing-tile .ageing-label {
        font-size: 0.9rem;
        font-weight: 500;
    }
    .ageing-green { background-color: #E7F5EC; color: #1E7E34; border-color: #C7E7D2; }
    .ageing-amber { background-color: #FFF6DE; color: #B7791F; border-color: #F5E4B5; }
    .ageing-red   { background-color: #FCEAEB; color: #C0392B; border-color: #F5C7CC; }

    /* ==== Segmented control ==== */
    [data-testid="stSegmentedControl"] label {
        font-weight: 500;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# CSV path is a developer-facing config; it's kept in the sidebar so the
# path stays adjustable at runtime, but it's tucked inside a collapsed
# expander at the bottom so it doesn't clutter the professional look.
# Read it now (a placeholder value pre-load_data) so load_data can run
# below; the widget itself lives near the end of the sidebar block.
csv_path = st.session_state.get("csv_path", CSV_PATH_DEFAULT)

try:
    df_all = load_data(csv_path)
except FileNotFoundError:
    st.error(
        f"Could not find `{csv_path}`. Run `python data_pull.py --output {csv_path}` "
        "first, or point the sidebar CSV path at an existing file."
    )
    st.stop()

# Cities driven entirely by the data (every distinct `cluster` value
# present), alphabetically -- not a hardcoded Python constant.
known_clusters = sorted({
    c for c in df_all.get("cluster", pd.Series(dtype=str)).astype(str).str.strip().tolist()
    if c and c.lower() != "nan"
})

# Two-level nav: "PAN India" (no cluster filter) vs. "City view" (a specific
# city, chosen from a dropdown that defaults to the alphabetically-first
# city). This replaces the old flat Pan-India-plus-every-city button list.
if "nav_mode" not in st.session_state:
    st.session_state.nav_mode = "pan_india"
if "selected_city_dropdown" not in st.session_state or st.session_state.selected_city_dropdown not in known_clusters:
    st.session_state.selected_city_dropdown = known_clusters[0] if known_clusters else None

with st.sidebar:
    # ---- Brand block ----
    st.markdown(
        '<div class="sb-brand-title">FollowUp Dashboard</div>'
        '<div class="sb-brand-sub">Meeting → Follow-up tracking</div>'
        '<hr class="sb-divider" />',
        unsafe_allow_html=True,
    )

    # ---- City selector (city_view mode only) ----
    if st.session_state.nav_mode == "city_view" and known_clusters:
        st.markdown('<div class="sb-label">City</div>', unsafe_allow_html=True)
        st.selectbox(
            "City", options=known_clusters, key="selected_city_dropdown",
            label_visibility="collapsed",
        )
        st.markdown(
            '<div class="sb-hint">Cluster + Field Sale SC filter applied dashboard-wide.</div>',
            unsafe_allow_html=True,
        )

    # ---- Spacer to push nav pills to the bottom of the sidebar ----
    st.markdown('<div class="sb-spacer"></div>', unsafe_allow_html=True)

    # ---- Nav pills anchored at the bottom ----
    is_pan_india_active = st.session_state.nav_mode == "pan_india"
    if st.button(
        "🌐  PAN India", key="nav_pan_india",
        type="primary" if is_pan_india_active else "secondary", width="stretch",
    ):
        st.session_state.nav_mode = "pan_india"
        st.rerun()
    if st.button(
        "🏢  City view", key="nav_city_view",
        type="primary" if not is_pan_india_active else "secondary", width="stretch",
    ):
        st.session_state.nav_mode = "city_view"
        st.rerun()

    # ---- Developer-facing CSV path, tucked into a subtle expander ----
    with st.expander("⚙️ Data source"):
        new_csv_path = st.text_input(
            "CSV path", value=csv_path, key="csv_path_input", label_visibility="collapsed",
        )
        st.caption("Produced by data_pull.py.")
        if new_csv_path != csv_path:
            st.session_state.csv_path = new_csv_path
            st.rerun()

if st.session_state.nav_mode == "city_view" and st.session_state.selected_city_dropdown:
    CLUSTER_NAME = st.session_state.selected_city_dropdown
else:
    CLUSTER_NAME = PAN_INDIA
IS_PAN_INDIA = CLUSTER_NAME == PAN_INDIA  # drives the City-level breakdowns/replacements used throughout the page

SC_CHANNEL = "Field Sale SC"  # applied dashboard-wide, not just for the Card 1 "Meetings done" count

if CLUSTER_NAME == PAN_INDIA:
    city_df = df_all.copy()
else:
    city_df = M.filter_cluster(df_all, CLUSTER_NAME)
city_df = M.filter_sc_channel(city_df, SC_CHANNEL)

as_of = ""
if "snapshot_date" in city_df.columns and len(city_df):
    vals = city_df["snapshot_date"].dropna()
    as_of = vals.iloc[0] if len(vals) else ""

page_eyebrow = "Pan India" if IS_PAN_INDIA else "City view"
page_display_name = "India" if IS_PAN_INDIA else CLUSTER_NAME
page_caption_bits = [f"{len(city_df):,} {SC_CHANNEL} records"]
if not IS_PAN_INDIA:
    page_caption_bits.append(f"in the {CLUSTER_NAME} cluster")
if as_of:
    page_caption_bits.append(f"as of {as_of}")
st.markdown(
    f'<div class="page-eyebrow">{page_eyebrow}</div>'
    f'<div class="page-title">{page_display_name}</div>'
    f'<div class="page-caption">{" · ".join(page_caption_bits)}</div>',
    unsafe_allow_html=True,
)

if city_df.empty:
    if CLUSTER_NAME == PAN_INDIA:
        st.warning(
            f"No '{SC_CHANNEL}' rows found in the data. Check the `sc_channel` column values in the CSV."
        )
    else:
        st.warning(
            f"No '{SC_CHANNEL}' rows found for cluster '{CLUSTER_NAME}'. "
            "Check the `cluster` and `sc_channel` column values in the CSV."
        )
    st.stop()


# ---------------------------------------------------------------------------
# Card 1 — Overview
# ---------------------------------------------------------------------------

with st.container(border=True):
    st.header("Overview")

    c1 = M.card1_metrics(city_df)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Meetings done", f"{c1['meetings_done']:,}")
    col2.metric("Closed on spot", f"{c1['closed_on_spot']:,}")
    col2.caption(f"{c1['closed_on_spot_pct']:.1f}% of Meetings done")
    col3.metric("Eligible for follow-ups", f"{c1['eligible_for_followups']:,}")
    col3.caption(f"{c1['eligible_for_followups_pct']:.1f}% of Meetings done")
    col4.metric("No audio notes", f"{c1['no_audio_notes']:,}")
    col4.caption(f"{c1['no_audio_notes_pct']:.1f}% of Meetings done")

    if IS_PAN_INDIA:
        with st.expander("🔽 City breakdown"):
            c1_city = M.card1_metrics_by_city(city_df)
            c1_city_display = c1_city.rename(columns={
                "meetings_done": "Meetings done",
                "closed_on_spot": "Closed on spot",
                "eligible_for_followups": "Eligible for follow-ups",
                "no_audio_notes": "No audio notes",
            })
            st.dataframe(c1_city_display, width="stretch", hide_index=True)

st.divider()


# ---------------------------------------------------------------------------
# Audio Index
# ---------------------------------------------------------------------------

with st.container(border=True):
    st.header("Audio Index")

    ai = M.audio_index_summary(city_df)

    if ai["total_denominator"] == 0:
        st.info("No non-on-spot meetings for this cluster to compute the Audio Index against.")
    else:
        ai_c1, ai_c2, ai_c3 = st.columns(3)
        ai_c1.metric(
            "Coverage %",
            fmt_pct(ai["coverage_pct"]),
            help="Represents the percentage of leads whose Audio is Present",
        )
        ai_c2.metric(
            "Completeness %",
            fmt_pct(ai["completeness_pct"]),
            help=f"Share of all {M.TOTAL_DISPOSITIONS} disposition slots, across leads whose Audio is present",
        )
        ai_c3.metric(
            "Audio Index",
            f"{ai['audio_index']:.1f}/10",
            help="(Coverage % × Completeness %) / 1000, rounded to 1 decimal.",
        )

        if IS_PAN_INDIA:
            with st.expander("🔽 City breakdown"):
                ai_city = M.audio_index_summary_by_city(city_df)
                ai_city_display = ai_city.copy()
                ai_city_display["coverage_pct"] = ai_city_display["coverage_pct"].map(lambda x: f"{x:.1f}%")
                ai_city_display["completeness_pct"] = ai_city_display["completeness_pct"].map(lambda x: f"{x:.1f}%")
                ai_city_display["audio_index"] = ai_city_display["audio_index"].map(lambda x: f"{x:.1f}/10")
                ai_city_display = ai_city_display.rename(columns={
                    "coverage_pct": "Coverage %", "completeness_pct": "Completeness %", "audio_index": "Audio Index",
                })
                st.dataframe(ai_city_display, width="stretch", hide_index=True)

        st.subheader("Audio Index — drill down")
        meetings_today_only = st.toggle("From Meeting's Today", key="audio_meetings_today")
        st.caption(
            "Filtered to leads with fu_completedat_today = 1."
            if meetings_today_only else
            "Showing all leads with audio_present = True & is_on_spot = False."
        )

        meeting_done_pool = M.apply_meetings_today_filter(city_df, meetings_today_only)
        audio_base = M.audio_pool(meeting_done_pool)

        if audio_base.empty:
            st.info("No leads match this sub-section's filters right now.")
        else:
            tab_split, tab_disp = st.tabs(["Split", "Disposition Breakdown"])

            with tab_split:
                audio_display_cols = {
                    "index": "Index",
                    "leads": "Leads",
                    "pct_with_missing": "% leads with a disposition missing",
                    "avg_missing": "Dispositions missing (average per lead)",
                }

                if IS_PAN_INDIA:
                    # ---- Pan-India: flat City view, no further drill ----
                    st.caption("Index = (Coverage % × Completeness %) / 1000.")
                    city_audio = M.audio_split_breakdown(meeting_done_pool, audio_base, "cluster")
                    city_audio_display = city_audio.copy()
                    city_audio_display["pct_with_missing"] = city_audio_display["pct_with_missing"].map(
                        lambda x: f"{x:.1f}%"
                    )
                    city_audio_display = city_audio_display.rename(columns={"cluster": "City", **audio_display_cols})
                    st.dataframe(
                        city_audio_display.style.apply(audio_index_row_style, axis=1),
                        width="stretch", hide_index=True,
                    )
                else:
                    def _audio_fmt(df: pd.DataFrame) -> pd.DataFrame:
                        d = df.copy()
                        d["pct_with_missing"] = d["pct_with_missing"].map(lambda x: f"{x:.1f}%")
                        return d

                    st.caption("Index = (Coverage % × Completeness %) / 1000.")
                    st.markdown("**TL → SC → Lead**")
                    render_accordion_drill(
                        tl_df=_audio_fmt(M.tl_audio_breakdown(meeting_done_pool, audio_base)),
                        tl_id_col="tl",
                        sc_lookup=lambda tl: _audio_fmt(
                            M.sc_audio_breakdown(meeting_done_pool, audio_base, tl)
                        ),
                        sc_id_col="sc",
                        lead_lookup=lambda tl, sc: M.audio_lead_table(audio_base, tl_name=tl, sc_name=sc),
                        display_cols=audio_display_cols,
                        group_label="TL",
                        sub_label="SC",
                        state_key="audio_index",
                        row_style_fn=audio_index_row_style,
                    )

            with tab_disp:
                disp = M.disposition_breakdown(audio_base)
                disp_display = disp.copy()
                disp_display["Share"] = disp_display["Share"].map(lambda x: f"{x:.1f}%")
                st.dataframe(disp_display, width="stretch", hide_index=True)

st.divider()


# ---------------------------------------------------------------------------
# Due Today
# ---------------------------------------------------------------------------

with st.container(border=True):
    st.header("Due Today")

    dts = M.due_today_summary(city_df)

    if dts["total_came_due"] == 0:
        st.info("No follow-ups came due today for this cluster.")
    else:
        st.metric(
            "Completed of today's due",
            f"{dts['completed_count']:,}/{dts['total_came_due']:,}",
            help="Count of leads with fu_completedat_today=1 out of count of leads with fu_due_date_today=1.",
        )
        if not IS_PAN_INDIA:
            top_tl_note = (
                f"highest pending: **{dts['top_pending_tl']}** ({dts['top_pending_count']:,})"
                if dts["top_pending_tl"] else "—"
            )
            st.caption(top_tl_note)

        cat1, cat2, cat3 = st.columns(3)
        cat1.metric(
            "Agreed to Meet + Another follow-up",
            f"{dts['agreed_another_completed']:,}/{dts['agreed_another']:,}",
            help="Completed today for this outcome, over Due today for this outcome.",
        )
        cat1.caption(f"{dts['agreed_another_pct']:.1f}%")
        cat2.metric(
            "P1 + P2",
            f"{dts['p1p2_completed']:,}/{dts['p1p2']:,}",
            help="Completed today for this outcome, over Due today for this outcome.",
        )
        cat2.caption(f"{dts['p1p2_pct']:.1f}%")
        cat3.metric(
            "Others",
            f"{dts['others_completed']:,}/{dts['others']:,}",
            help="Completed today for this outcome, over Due today for this outcome.",
        )
        cat3.caption(f"{dts['others_pct']:.1f}%")
        st.caption(
            "Agreed+Another and P1+P2 are counted independently (a lead can be both), "
            "so Others = total − P1+P2 − Agreed+Another can run lower than the drill-down "
            "table below, which assigns each lead to exactly one category. Each fraction "
            "above is that outcome's Completed today over its own Due today count."
        )

        came_due = M.came_due_pool(city_df)

        display_cols = {
            "came_due": "Due Today", "worked": "Worked", "pending": "Pending", "pct": "%",
            "Agreed to Meet + Another follow-up": "Agreed + Another",
            "P1+P2": "P1+P2", "Others": "Others",
        }
        due_today_column_config = {
            "%": st.column_config.NumberColumn(
                "%",
                help="Percentage of Worked out of Due Today leads for this row "
                     "(fu_completedat_today flag out of fu_due_date_today flag leads).",
            ),
        }

        if IS_PAN_INDIA:
            # ---- Pan-India: flat City view, no further drill ----
            with st.expander("🔽 Due Today — City breakdown"):
                cityb = M.breakdown_by(came_due, "cluster")
                cityb_display = cityb.rename(columns={"cluster": "City", **display_cols})
                st.dataframe(
                    cityb_display, width="stretch", hide_index=True, column_config=due_today_column_config,
                )
        else:
            st.markdown("**TL → SC → Lead**")
            render_accordion_drill(
                tl_df=M.tl_breakdown(came_due),
                tl_id_col="tl",
                sc_lookup=lambda tl: M.sc_breakdown(came_due, tl),
                sc_id_col="sc",
                lead_lookup=lambda tl, sc: M.lead_table(came_due, str(as_of), tl_name=tl, sc_name=sc),
                display_cols=display_cols,
                group_label="TL",
                sub_label="SC",
                state_key="due_today",
                column_config=due_today_column_config,
            )

st.divider()


# ---------------------------------------------------------------------------
# Overdue
# ---------------------------------------------------------------------------

with st.container(border=True):
    st.header("Overdue")

    ov = M.overdue_summary(city_df)

    if ov["total_denominator"] == 0:
        st.info("No leads in the funnel are eligible for the Overdue % denominator for this cluster.")
    else:
        ov_top, ov_p = st.columns([2, 2])
        with ov_top:
            st.metric(
                "Overdue",
                fmt_pct(ov["pct_overdue"]),
                help="funnel_bucket = 'overdue' leads, over all leads whose funnel_bucket "
                     "isn't booked / terminal / lost / later / customer_unreachable.",
            )
            st.caption(f"{ov['total_overdue']:,} of {ov['total_denominator']:,} eligible leads")
        with ov_p:
            st.metric("Total overdue leads", f"{ov['total_overdue']:,}")
            if not IS_PAN_INDIA:
                top_tl_note = (
                    f"highest overdue: **{ov['top_overdue_tl']}** ({ov['top_overdue_count']:,})"
                    if ov["top_overdue_tl"] else "—"
                )
                st.caption(top_tl_note)

        age1, age2, age3 = st.columns(3)
        render_ageing_tile(age1, ov["under_3_days"], ov["under_3_days_pct"], "Under 3 days", "green")
        render_ageing_tile(age2, ov["three_to_five_days"], ov["three_to_five_days_pct"], "3-5 days", "amber")
        render_ageing_tile(age3, ov["over_5_days"], ov["over_5_days_pct"], "Over 5 days", "red")

        ocat1, ocat2, ocat3 = st.columns(3)
        ocat1.metric("Agreed to Meet + Another follow-up", f"{ov['agreed_another']:,}")
        ocat2.metric("P1 + P2", f"{ov['p1p2']:,}")
        ocat3.metric("Others", f"{ov['others']:,}")
        st.caption(
            "Agreed+Another and P1+P2 are counted independently (a lead can be both), "
            "so Others = total − P1+P2 − Agreed+Another can run lower than the drill-down "
            "table below, which assigns each lead to exactly one category."
        )

        ov_display_cols = {
            "overdue": "Overdue", "overdue_pct": "Overdue %",
            "Agreed to Meet + Another follow-up": "Agreed + Another",
            "P1+P2": "P1+P2", "Others": "Others",
        }
        overdue_column_config = {
            "Overdue %": st.column_config.NumberColumn(
                "Overdue %",
                help="This row's Overdue count over its own eligible-leads denominator "
                     "(funnel_bucket not booked/terminal/lost/later/customer_unreachable) "
                     "-- same ratio as the Overdue % shown at the top of this section.",
            ),
        }

        if IS_PAN_INDIA:
            # ---- Pan-India: flat City view, no further drill ----
            with st.expander("🔽 Overdue — City breakdown"):
                city_ov = M.overdue_breakdown_by(city_df, "cluster")
                city_ov_display = city_ov.rename(columns={"cluster": "City", **ov_display_cols})
                st.dataframe(
                    city_ov_display, width="stretch", hide_index=True, column_config=overdue_column_config,
                )
        else:
            st.markdown("**TL → SC → Lead**")
            ov_pool_all = M.overdue_pool(city_df)
            render_accordion_drill(
                tl_df=M.tl_overdue_breakdown(city_df),
                tl_id_col="tl",
                sc_lookup=lambda tl: M.sc_overdue_breakdown(city_df, tl),
                sc_id_col="sc",
                lead_lookup=lambda tl, sc: M.overdue_lead_table(ov_pool_all, tl_name=tl, sc_name=sc),
                display_cols=ov_display_cols,
                group_label="TL",
                sub_label="SC",
                state_key="overdue",
                column_config=overdue_column_config,
            )

st.divider()


# ---------------------------------------------------------------------------
# Funnel quality
# ---------------------------------------------------------------------------

with st.container(border=True):
    st.header("Funnel quality")

    fq_cut = st.segmented_control(
        "Funnel quality cut", options=["Overall", "TL wise"], default="Overall",
        key="funnel_quality_cut", label_visibility="collapsed",
    )
    if fq_cut is None:
        fq_cut = "Overall"

    fq_pool = M.funnel_quality_pool(city_df)

    if fq_pool.empty:
        st.info("No leads in the funnel quality pool for this cluster.")
    elif fq_cut == "TL wise":
        fqtlw = M.funnel_quality_tl_wise(city_df)
        if fqtlw.empty:
            st.info("No leads in the funnel quality pool for this cluster.")
        else:
            fqtlw_display = build_funnel_quality_tlwise_display(fqtlw)
            st.dataframe(fqtlw_display, width="stretch", hide_index=True)
    else:
        fq_overall = M.funnel_quality_overall(city_df)
        fq_overall_display = build_funnel_quality_overall_display(fq_overall)
        st.dataframe(style_funnel_quality_state(fq_overall_display), width="stretch", hide_index=True)

        st.caption("Click a category below to see its SC-wise breakdown.")

        for _, cat_row in fq_overall.iterrows():
            with st.expander(f"{cat_row['measure']} ({cat_row['now_pct']:.1f}%, {cat_row['state']})"):
                fq_sc = M.funnel_quality_sc_breakdown(city_df, cat_row["key"])
                if fq_sc.empty:
                    st.caption("No leads in this category.")
                else:
                    fq_sc_display = build_funnel_quality_sc_display(fq_sc, cat_row["measure"])
                    st.dataframe(fq_sc_display, width="stretch", hide_index=True)

st.divider()


# ---------------------------------------------------------------------------
# Follow-up funnel
# ---------------------------------------------------------------------------

with st.container(border=True):
    st.header("Follow-up funnel")

    GROUP_CUT_LABEL = "City wise" if IS_PAN_INDIA else "TL wise"

    funnel_cut = st.segmented_control(
        "Follow-up funnel cut", options=["Overall", GROUP_CUT_LABEL], default="Overall",
        key="funnel_cut", label_visibility="collapsed",
    )
    if funnel_cut is None:
        funnel_cut = "Overall"

    if funnel_cut == GROUP_CUT_LABEL and IS_PAN_INDIA:
        # ---- Pan-India: flat City view, no further drill ----
        cw = M.funnel_group_wise_table(city_df, "cluster")
        if cw.empty:
            st.info("No leads in the follow-up funnel pool for this cluster.")
        else:
            cw_display = build_funnel_group_wise_display(cw, "cluster", "City")
            st.dataframe(cw_display, width="stretch", hide_index=True)
    elif funnel_cut == GROUP_CUT_LABEL:
        if "funnel_tlwise_drill_tl" not in st.session_state:
            st.session_state.funnel_tlwise_drill_tl = None

        funnel_crumb = ["All TLs"]
        if st.session_state.funnel_tlwise_drill_tl:
            funnel_crumb.append(st.session_state.funnel_tlwise_drill_tl)
        st.caption(" › ".join(funnel_crumb))

        if st.session_state.funnel_tlwise_drill_tl:
            if st.button("⟵ Reset to all TLs", key="funnel_tlwise_reset_btn"):
                st.session_state.funnel_tlwise_drill_tl = None
                clear_table_selection("funnel_tlwise_tl_table")
                st.rerun()

        if not st.session_state.funnel_tlwise_drill_tl:
            # ---- TL view ----
            tlw = M.funnel_tl_wise_table(city_df)
            if tlw.empty:
                st.info("No leads in the follow-up funnel pool for this cluster.")
            else:
                st.caption("Click a row to drill into that TL's SCs.")
                tlw_display = build_funnel_group_wise_display(tlw, "tl", "TL")
                picked = render_drill_table(tlw_display, tlw["tl"], "funnel_tlwise_tl_table")
                if picked is not None:
                    st.session_state.funnel_tlwise_drill_tl = picked
                    st.rerun()
        else:
            # ---- SC view (within selected TL) ----
            scw = M.funnel_sc_wise_table(city_df, st.session_state.funnel_tlwise_drill_tl)
            if scw.empty:
                st.info("No leads in the follow-up funnel pool for this TL.")
            else:
                scw_display = build_funnel_group_wise_display(scw, "sc", "SC")
                st.dataframe(scw_display, width="stretch", hide_index=True)
    else:
        funnel_boxes = M.funnel_box_counts(city_df)

        if funnel_boxes.empty:
            st.info("No leads in the follow-up funnel pool for this cluster.")
        else:
            box_cols = st.columns(min(len(funnel_boxes), 4))
            for i, box_row in funnel_boxes.iterrows():
                box_cols[i % len(box_cols)].metric(box_row["label"], f"{box_row['leads']:,}")

            st.caption("Click a box below to see its breakdown.")

            for _, box_row in funnel_boxes.iterrows():
                raw_status, label, leads = box_row["raw_status"], box_row["label"], box_row["leads"]
                with st.expander(f"{label} ({leads:,} leads)"):
                    box_pool = M.funnel_box_pool(city_df, raw_status)
                    if box_pool.empty:
                        st.caption("No leads in this box.")
                        continue

                    if raw_status == "another_follow_up_required":
                        # ---- Another Follow up needed: 1st cut (When/Leads/Share) ----
                        when_tbl = M.another_fu_when_table(box_pool, str(as_of))
                        st.dataframe(when_tbl, width="stretch", hide_index=True)

                        affu_display_cols = {
                            "leads": "Leads", "overdue": "Overdue", "due_today": "Due today",
                            "upcoming": "Upcoming", "avg_days_out": "Avg days out",
                        }

                        if IS_PAN_INDIA:
                            # ---- Pan-India: flat City view, no further drill ----
                            st.markdown("**City breakdown**")
                            city_affu = M.another_fu_breakdown_by(box_pool, str(as_of), "cluster")
                            city_affu_display = city_affu.rename(columns={"cluster": "City", **affu_display_cols})
                            st.dataframe(city_affu_display, width="stretch", hide_index=True)
                        else:
                            st.markdown("**TL → SC → Lead**")
                            render_accordion_drill(
                                tl_df=M.another_fu_tl_breakdown(box_pool, str(as_of)),
                                tl_id_col="tl",
                                sc_lookup=lambda tl: M.another_fu_sc_breakdown(box_pool, str(as_of), tl),
                                sc_id_col="sc",
                                lead_lookup=lambda tl, sc: M.another_fu_lead_table(
                                    box_pool, str(as_of), tl_name=tl, sc_name=sc,
                                ),
                                display_cols=affu_display_cols,
                                group_label="TL",
                                sub_label="SC",
                                state_key="affu",
                            )

                    elif raw_status == "dnp":
                        # ---- DNP: 1st cut (Consecutive DNPs/Leads/Share) ----
                        ct = M.dnp_consecutive_table(box_pool)
                        st.dataframe(ct, width="stretch", hide_index=True)

                        dnp_display_cols = {
                            "dnp_leads": "DNP leads", "three_plus": "3+ in a row", "avg_dnps": "Avg DNPs",
                        }

                        if IS_PAN_INDIA:
                            # ---- Pan-India: flat City view, no further drill ----
                            st.markdown("**City breakdown**")
                            city_dnp = M.dnp_breakdown_by(box_pool, "cluster")
                            city_dnp_display = city_dnp.rename(columns={"cluster": "City", **dnp_display_cols})
                            st.dataframe(city_dnp_display, width="stretch", hide_index=True)
                        else:
                            st.markdown("**TL → SC → Lead**")
                            render_accordion_drill(
                                tl_df=M.dnp_tl_breakdown(box_pool),
                                tl_id_col="tl",
                                sc_lookup=lambda tl: M.dnp_sc_breakdown(box_pool, tl),
                                sc_id_col="sc",
                                lead_lookup=lambda tl, sc: M.dnp_lead_table(box_pool, tl_name=tl, sc_name=sc),
                                display_cols=dnp_display_cols,
                                group_label="TL",
                                sub_label="SC",
                                state_key="dnp",
                            )

                    elif raw_status == "agreed_to_meet":
                        # ---- Agreed to Meet: 3 metrics only, no drill-down per spec ----
                        atm = M.agreed_to_meet_summary(box_pool, str(as_of))
                        am1, am2, am3 = st.columns(3)
                        am1.metric("Already slipped", f"{atm['already_slipped']:,}")
                        am2.metric("Due today", f"{atm['due_today']:,}")
                        am3.metric("Still ahead", f"{atm['still_ahead']:,}")

                    else:
                        # ---- Generic template: No action possible / Will go later / Lost to Competitor / Booked / other ----
                        generic_group_col = "cluster" if IS_PAN_INDIA else "tl"
                        generic_group_label = "City" if IS_PAN_INDIA else "TL"
                        gb = M.funnel_generic_breakdown(box_pool, str(as_of), group_col=generic_group_col)
                        gb_display = gb.rename(columns={
                            generic_group_col: generic_group_label, "leads": "Leads", "share": "Share",
                            "avg_fus": "Avg FUs", "avg_days_to_last_fu": "Avg days to last FU", "p1p2": "P1+P2",
                        })
                        gb_display["Share"] = gb_display["Share"].map(lambda x: f"{x:.1f}%")
                        st.dataframe(gb_display, width="stretch", hide_index=True)

st.divider()
