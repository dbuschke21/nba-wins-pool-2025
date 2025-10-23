# app.py — NBA Wins/Losses Pool Tracker (Google Sheets + ESPN)
# ------------------------------------------------------------
# This build:
# - Fixes AttributeError by using a version-proof rerun helper
# - "Hard refresh (no cache)" + normal refresh buttons
# - Robust ESPN parser (East+West; root fallback)
# - Stricter team-name normalization; diagnostics & mismatch hints
# - Default sort: Player & Per-Team by P% desc; NBA standings alphabetical
# - Short headers; compact widths; 1-decimal percentages (no % sign in values)
# - PLYR (<=6) used for colors & legend; TMF shows comma-separated team abbreviations

import re
import difflib
import pandas as pd
import requests
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from streamlit import column_config

# ----------------------------
# Configuration
# ----------------------------
SEASON = "2025-26"  # update each year
SHEET_ID = st.secrets["SHEET_ID"]
DRAFT_TAB = "Draft"  # Worksheet tab in your Google Sheet

# Player colors (extend if needed)
PLAYER_COLORS = [
    "#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2",
    "#F2CF5B", "#B279A2", "#FF9DA6", "#9D755D", "#BAB0AC",
]

# ---- Streamlit rerun compatibility (works on old & new versions)
def force_rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    else:  # older Streamlit
        st.experimental_rerun()

# ----------------------------
# Google Sheets client helpers
# ----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

def get_sheets_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    return gspread.authorize(creds)

def ensure_draft_tab(gc):
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(DRAFT_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=DRAFT_TAB, rows=200, cols=5)
        ws.update([["Player", "PLYR", "Team", "PointType", "TeamAbbr"]])
    return ws

def normalize_team_name(s: str) -> str:
    """Trim and condense internal whitespace so 'Los  Angeles  Lakers' -> 'Los Angeles Lakers'."""
    s = (s or "").strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s

def read_draft(gc) -> pd.DataFrame:
    ws = ensure_draft_tab(gc)
    rows = ws.get_all_records()
    df = pd.DataFrame(rows)

    # Ensure columns exist
    for col in ["Player", "PLYR", "Team", "PointType", "TeamAbbr", "Abbr"]:
        if col not in df.columns:
            df[col] = ""

    # Normalize
    df["Player"] = df["Player"].fillna("").astype(str)
    df["PLYR"]   = df["PLYR"].fillna("").astype(str).apply(lambda s: s[:6])
    df["Team"]   = df["Team"].fillna("").astype(str).apply(normalize_team_name)
    df["PointType"] = (
        df["PointType"].fillna("Wins").astype(str).apply(
            lambda x: "Wins" if x.strip().lower().startswith("win") else "Losses"
        )
    )

    # Prefer explicit TeamAbbr, else legacy Abbr, else blank (will fallback to ESPN later)
    df["TeamAbbr"] = df["TeamAbbr"].replace("", pd.NA)
    df["Abbr"] = df["Abbr"].replace("", pd.NA)
    df["TeamAbbr"] = df["TeamAbbr"].fillna(df["Abbr"]).fillna("")

    return df[["Player", "PLYR", "Team", "PointType", "TeamAbbr"]]

def write_draft(gc, entries):
    ws = ensure_draft_tab(gc)
    values = [["Player", "PLYR", "Team", "PointType", "TeamAbbr"]]
    for e in entries:
        values.append([
            e.get("Player",""),
            (e.get("PLYR","") or "")[:6],
            normalize_team_name(e.get("Team","")),
            e.get("PointType",""),
            e.get("TeamAbbr",""),
        ])
    ws.clear()
    ws.update(values)

def export_teams_tab(gc, sheet_id, teams):
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet("Teams")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Teams", rows=100, cols=1)
    values = [["Team"]] + [[t] for t in sorted(teams)]
    ws.clear()
    ws.update(values)

# ----------------------------
# ESPN Standings fetch (East + West)
# ----------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_nba_standings() -> pd.DataFrame:
    """
    Returns DataFrame with columns: Team, Abbr, W, L, WinPct (0..1).
    Robust: tries two ESPN endpoints and multiple record shapes.
    """
    import re

    def extract_w_l_pct_from_entry(team_dict, stats_list, records_list):
        # defaults
        W = L = None
        WPCT = None

        # 1) stats list (various keys ESPN uses)
        stats = {}
        for s in stats_list or []:
            # support both id/name and value/displayValue
            k = s.get("id") or s.get("name")
            if k:
                stats[k] = s

        def num(k):
            if k in stats:
                v = stats[k].get("value")
                if v is None:
                    dv = stats[k].get("displayValue")
                    if isinstance(dv, (int, float)):
                        return dv
                    # e.g. "12" or "12-8"
                    if isinstance(dv, str) and dv.isdigit():
                        return int(dv)
                if isinstance(v, (int, float)):
                    return v
            return None

        W = num("wins") if W is None else W
        L = num("losses") if L is None else L
        WPCT = num("winPercent") if WPCT is None else WPCT
        if WPCT is None:
            # sometimes winPercentV2
            WPCT = num("winPercentV2")

        # 2) If W/L still missing, parse records.summary like "12-8"
        if (W is None or L is None) and records_list:
            for rec in records_list:
                name = (rec.get("name") or rec.get("type") or "").lower()
                # pick an overall/total/regular record
                if any(key in name for key in ["overall", "total", "regular"]):
                    summary = rec.get("summary") or rec.get("displayValue") or ""
                    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)", summary)
                    if m:
                        W = int(m.group(1))
                        L = int(m.group(2))
                        break

        # 3) Compute pct if we now have W/L
        if (WPCT is None or WPCT == 0) and isinstance(W, int) and isinstance(L, int) and (W + L) > 0:
            WPCT = W / (W + L)

        # sanitize
        W = int(W) if isinstance(W, (int, float)) else 0
        L = int(L) if isinstance(L, (int, float)) else 0
        WPCT = float(WPCT) if isinstance(WPCT, (int, float)) else (W / (W + L) if (W + L) > 0 else 0.0)
        return W, L, WPCT

    # Try both shapes/endpoints
    urls = [
        "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings",
        "https://cdn.espn.com/core/nba/standings?xhr=1",
    ]

    for url in urls:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()

            rows = []

            # ---- Newer cdn.espn.com shape
            if isinstance(data, dict) and "content" in data and "standings" in data["content"]:
                groups = data["content"]["standings"].get("groups", [])
                for g in groups:
                    entries = g.get("standings", {}).get("entries", []) or g.get("entries", [])
                    for e in entries:
                        team = e.get("team", {}) or {}
                        name = team.get("displayName") or team.get("name") or team.get("shortDisplayName")
                        abbr = team.get("abbreviation") or (name[:3].upper() if name else "")
                        stats_list = e.get("stats", []) or e.get("standings", {}).get("stats", [])
                        records_list = e.get("records", [])
                        w, l, wp = extract_w_l_pct_from_entry(team, stats_list, records_list)
                        if name:
                            rows.append({"Team": name, "Abbr": abbr, "W": w, "L": l, "WinPct": wp})

            # ---- site.web.api shape with children/standings
            if not rows and "children" in data:
                for ch in data["children"]:
                    stg = ch.get("standings")
                    if not stg:
                        continue
                    for e in stg.get("entries", []):
                        team = e.get("team", {}) or {}
                        name = team.get("displayName") or team.get("name") or team.get("shortDisplayName")
                        abbr = team.get("abbreviation") or (name[:3].upper() if name else "")
                        stats_list = e.get("stats", [])
                        records_list = e.get("records", [])
                        w, l, wp = extract_w_l_pct_from_entry(team, stats_list, records_list)
                        if name:
                            rows.append({"Team": name, "Abbr": abbr, "W": w, "L": l, "WinPct": wp})

            # ---- site.web.api shape with standings at root
            if not rows and "standings" in data:
                for e in data["standings"].get("entries", []):
                    team = e.get("team", {}) or {}
                    name = team.get("displayName") or team.get("name") or team.get("shortDisplayName")
                    abbr = team.get("abbreviation") or (name[:3].upper() if name else "")
                    stats_list = e.get("stats", [])
                    records_list = e.get("records", [])
                    w, l, wp = extract_w_l_pct_from_entry(team, stats_list, records_list)
                    if name:
                        rows.append({"Team": name, "Abbr": abbr, "W": w, "L": l, "WinPct": wp})

            if rows:
                df = pd.DataFrame(rows)
                # ESPN can duplicate teams across groups; keep the row with the largest GP
                df["GP"] = df["W"] + df["L"]
                df = df.sort_values(["Team", "GP"], ascending=[True, False]).drop_duplicates("Team", keep="first")
                df = df.drop(columns=["GP"], errors="ignore")
                return df.sort_values("Team").reset_index(drop=True)

        except Exception:
            # try next url
            continue

    raise RuntimeError("Could not parse NBA standings from ESPN.")


# ----------------------------
# Helpers
# ----------------------------
def build_player_palette(plyrs):
    uniq = [p for p in plyrs if p]
    seen, ordered = set(), []
    for p in uniq:
        if p not in seen:
            seen.add(p); ordered.append(p)
    return {p: PLAYER_COLORS[i % len(PLAYER_COLORS)] for i, p in enumerate(ordered)}

def style_by_plyr(df, plyr_col, cmap):
    def _row_style(r):
        color = cmap.get(r[plyr_col], "#FFFFFF")
        return [f"background-color: {color}22"] * len(r)
    styler = df.style.apply(_row_style, axis=1)
    # Explicit numeric formatting for % columns
    fmt = {"P%": "{:.1f}", "W%": "{:.1f}"}
    return styler.format(fmt)

def add_index(df):
    if df.empty:
        return df
    df = df.copy()
    df.insert(0, "#", range(1, len(df) + 1))
    return df

def compact_cols_config(include_tmf_width=220):
    return {
        "#":   column_config.NumberColumn("#", width=40),
        "PLYR":column_config.TextColumn("PLYR", width=40),
        "TM":  column_config.TextColumn("TM", width=45),
        "PT":  column_config.TextColumn("PT", width=30),
        "P":   column_config.NumberColumn("P", width=30),
        "P%":  column_config.NumberColumn("P%", width=40, format="%.1f"),
        "W":   column_config.NumberColumn("W", width=30),
        "L":   column_config.NumberColumn("L", width=30),
        "W%":  column_config.NumberColumn("W%", width=40, format="%.1f"),
        "GP":  column_config.NumberColumn("GP", width=40),
        "TMF": column_config.TextColumn("TMF", width=include_tmf_width),
    }

# ----------------------------
# Calculations
# ----------------------------
def calc_tables(draft_df: pd.DataFrame, standings: pd.DataFrame):
    # Build lookup dicts
    standings["TeamNorm"] = standings["Team"].apply(normalize_team_name)
    team_stats = standings.set_index("TeamNorm")[["W", "L", "WinPct"]].to_dict(orient="index")
    abbr_map   = standings.set_index("TeamNorm")["Abbr"].to_dict()

    rows = []
    for _, r in draft_df.iterrows():
        player = r["Player"]
        plyr   = r["PLYR"] or (player[:6] if player else "")
        team   = r["Team"]
        team_key = normalize_team_name(team)
        pt     = r.get("PointType", "Wins")
        pt_short = "W" if pt == "Wins" else "L"

        s = team_stats.get(team_key)
        if s is None:
            W = L = 0
        else:
            W, L = s["W"], s["L"]

        GP = W + L
        points = W if pt == "Wins" else L
        tm_abbr = (r.get("TeamAbbr", "") or "").strip() or abbr_map.get(team_key, "")

        rows.append(
            {
                "PLYR": plyr,
                "P_FULL": player,
                "TM": tm_abbr,
                "PT": pt_short,
                "P": int(points),
                "P%": round((points / GP) * 100, 1) if GP else 0.0,
                "W": int(W),
                "L": int(L),
                "W%": round((W / GP) * 100, 1) if GP else 0.0,
                "GP": int(GP),
                "TMF": team,  # full team name (input)
            }
        )

    per_team_df = pd.DataFrame(rows)

    if per_team_df.empty:
        player_table = pd.DataFrame(columns=["PLYR", "P", "P%", "W", "L", "W%", "GP", "TMF"])
    else:
        agg = (
            per_team_df.groupby(["PLYR"], as_index=False)
            .agg(P=("P", "sum"), GP=("GP", "sum"), W=("W", "sum"), L=("L", "sum"))
        )
        agg["P%"] = round((agg["P"] / agg["GP"].replace(0, pd.NA)) * 100, 1).fillna(0.0)
        agg["W%"] = round((agg["W"] / (agg["W"] + agg["L"]).replace(0, pd.NA)) * 100, 1).fillna(0.0)

        # TMF for top table = comma-separated abbreviations (TM)
        tm_list = (
            per_team_df.groupby("PLYR")["TM"]
            .apply(lambda s: ", ".join([t for t in s.tolist() if t]))
            .reset_index(name="TMF")
        )
        player_table = agg.merge(tm_list, on="PLYR", how="left").fillna({"TMF": ""})

        # Default sort by P% desc (then P, then GP)
        player_table = player_table.sort_values(["P%", "P", "GP"], ascending=[False, False, False]).reset_index(drop=True)

    # Per-team default sort by P% desc (then P, then W)
    if not per_team_df.empty:
        per_team_df = per_team_df.sort_values(["P%", "P", "W"], ascending=[False, False, False]).reset_index(drop=True)

    return player_table, per_team_df

# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="NBA Draft Tracker", page_icon="🏀", layout="wide")
st.title("🏀 NBA Wins/Losses Pool Tracker")
st.caption(
    "Draft data is stored in your **Google Sheet** (tab 'Draft'). "
    "Each team earns points based on its 'PointType': 1 per Win or 1 per Loss. "
    "Standings update live from ESPN."
)

# Fetch standings (with a true no-cache path)
colR, colBlank = st.columns([1, 9])
with colR:
    if st.button("🔧 Hard refresh (no cache)"):
        fetch_nba_standings.clear()
        force_rerun()

# Normal (cached) fetch
try:
    standings_df = fetch_nba_standings()
    team_list = standings_df["Team"].tolist()
    st.success(f"✅ Live standings loaded for {SEASON}")
except Exception as e:
    st.error(f"❌ Could not load standings: {e}")
    st.stop()

# Sidebar: regular refresh
with st.sidebar:
    if st.button("🔄 Refresh data (clear cache)"):
        fetch_nba_standings.clear()
        force_rerun()

# Sheets
gc = get_sheets_client()
try:
    draft_df = read_draft(gc)
except Exception as e:
    st.error(
        "Google Sheets permission error.\n\n"
        "Share the Sheet with:\n"
        f"  **{st.secrets.get('gcp_service_account', {}).get('client_email', '(service-account)')}** (Editor)\n\n"
        f"Details: {e}"
    )
    st.stop()

# Diagnostics: counts + mismatches
diag_cols = st.columns([1, 1, 1, 5])
with diag_cols[0]:
    st.metric("Draft rows", len(draft_df))
with diag_cols[1]:
    st.metric("ESPN teams", len(standings_df))
with diag_cols[2]:
    espn_set = set(standings_df["Team"].apply(normalize_team_name))
    typed = draft_df["Team"].apply(normalize_team_name)
    total_drafted = int((typed != "").sum())
    matched_count = int(((typed.isin(espn_set)) & (typed != "")).sum())
    st.metric("Teams matched", f"{matched_count}/{total_drafted}")
    
# Mismatch table with suggestions
bad = draft_df[
    (draft_df["Team"].fillna("") != "") &
    (~draft_df["Team"].apply(normalize_team_name).isin(set(standings_df["Team"].apply(normalize_team_name))))
][["Player", "PLYR", "Team"]].copy()

if not bad.empty:
    bad["Suggestions"] = bad["Team"].apply(
        lambda t: ", ".join(difflib.get_close_matches(normalize_team_name(t), list(standings_df["Team"]), n=3, cutoff=0.6)) or "(no close match)"
    )
    with diag_cols[3]:
        st.warning("Some team names in your Draft sheet don’t match ESPN’s names. Fix them in the Sheet or via the editor.")
        st.dataframe(bad, use_container_width=True)

# Editor (includes PLYR)
st.sidebar.header("Draft Editor (saves to Google Sheets)")
team_options = sorted(team_list)
editable_df = st.sidebar.data_editor(
    draft_df,
    num_rows="dynamic",
    use_container_width=True,
    hide_index=True,
    column_config={
        "Player": column_config.TextColumn("Player (full)", width=160),
        "PLYR": column_config.TextColumn("PLYR (<=6)", width=80),
        "Team": column_config.SelectboxColumn("Team (full)", options=[""] + team_options, required=False, width=220),
        "PointType": column_config.SelectboxColumn("PointType", options=["Wins", "Losses"], required=True, width=80),
        "TeamAbbr": column_config.TextColumn("TeamAbbr (TM)", width=80),
    }
)

# Save guards (max 6 per player, unique team, PLYR <= 6 chars)
if st.sidebar.button("💾 Save Draft to Google Sheets"):
    df_clean = editable_df.copy().fillna("")
    df_clean["Team"] = df_clean["Team"].apply(normalize_team_name)
    df_clean = df_clean[df_clean["Team"] != ""]
    too_long = df_clean[df_clean["PLYR"].str.len() > 6]
    if not too_long.empty:
        st.sidebar.error("Some PLYR values exceed 6 chars. Please shorten them.")
        st.stop()
    counts = df_clean.groupby("Player").size()
    offenders = [p for p, n in counts.items() if n > 6]
    if offenders:
        st.sidebar.error(f"Each player can have up to 6 teams. Offending: {', '.join(offenders)}")
        st.stop()
    dups = df_clean.duplicated(subset=["Team"], keep=False)
    if dups.any():
        bad_teams = df_clean.loc[dups, "Team"].unique().tolist()
        st.sidebar.error(f"These teams appear more than once: {', '.join(bad_teams)}")
        st.stop()
    entries = df_clean[["Player", "PLYR", "Team", "PointType", "TeamAbbr"]].to_dict(orient="records")
    write_draft(gc, entries)
    st.sidebar.success("Draft saved to Google Sheets ✅")
    force_rerun()

# Calculate (sorted by P% desc inside)
player_table_raw, per_team_table_raw = calc_tables(editable_df, standings_df)

# Colors (ordered by PLYR to keep legend stable)
plyr_order = player_table_raw["PLYR"].tolist() if not player_table_raw.empty else editable_df["PLYR"].tolist()
cmap = build_player_palette(plyr_order)

# ---- Legend (tight, one row) ----
if cmap:
    legend_html = "<div style='display:flex;gap:10px;flex-wrap:nowrap;align-items:center;'>"
    for plyr, color in cmap.items():
        legend_html += (
            f"<div style='display:flex;align-items:center;gap:6px;'>"
            f"<span style='width:12px;height:12px;background:{color};display:inline-block;border-radius:2px;'></span>"
            f"<span style='font-size:0.9rem'>{plyr}</span></div>"
        )
    legend_html += "</div>"
    st.caption("Player colors")
    st.markdown(legend_html, unsafe_allow_html=True)

def display_with_index(df, col_cfg):
    df = add_index(df)
    styled = style_by_plyr(df, "PLYR", cmap) if "PLYR" in df.columns else df
    st.dataframe(styled, use_container_width=True, hide_index=True, column_config=col_cfg)

# ---- Player Standings (sorted by P% desc) ----
st.divider()
st.subheader("🏆 Player Standings")
pt_display = player_table_raw[["PLYR", "P", "P%", "W", "L", "W%", "GP", "TMF"]].copy()
display_with_index(pt_display, compact_cols_config(include_tmf_width=220))

# ---- Per-team breakdown (sorted by P% desc) ----
st.divider()
col1, col2 = st.columns([1, 3])
with col1:
    st.subheader("Per-Team Breakdown")
    players_filter = ["All"] + sorted(editable_df["Player"].dropna().unique().tolist())
    who = st.selectbox("Filter by player", players_filter, index=0)
with col2:
    st.write("")

ptm = per_team_table_raw.copy()
if not ptm.empty:
    cols = ["PLYR", "TM", "PT", "P", "P%", "W", "L", "W%", "GP", "TMF"]
    ptm = ptm[cols]
    if who != "All":
        # map full Player -> PLYR
        full_to_plyr = editable_df.set_index("Player")["PLYR"].to_dict()
        sel_plyr = full_to_plyr.get(who, None)
        if sel_plyr:
            ptm = ptm[ptm["PLYR"] == sel_plyr]
        else:
            ptm = ptm.iloc[0:0]
    display_with_index(ptm, compact_cols_config(include_tmf_width=220))
else:
    st.info("Add draft rows (Player, PLYR, Team, PointType) to your Google Sheet to see results.")

# ---- Raw standings (alphabetical) ----
st.divider()
st.subheader("NBA Standings (ESPN)")
raw = standings_df[["Team", "Abbr", "W", "L", "WinPct"]].rename(columns={"WinPct": "W%"})
raw["W%"] = (raw["W%"] * 100).round(1)
raw = raw.copy()
raw.insert(0, "#", range(1, len(raw) + 1))
st.dataframe(
    raw,
    use_container_width=True,
    hide_index=True,
    column_config={
        "#":   column_config.NumberColumn("#", width=40),
        "Team":column_config.TextColumn("Team", width=220),
        "Abbr":column_config.TextColumn("Abbr", width=45),
        "W":   column_config.NumberColumn("W", width=40),
        "L":   column_config.NumberColumn("L", width=40),
        "W%":  column_config.NumberColumn("W%", width=40, format="%.1f"),
    }
)

# Export Teams tab
colA, colB = st.columns([1, 3])
with colA:
    if st.button("📤 Export team list to 'Teams' sheet"):
        export_teams_tab(gc, SHEET_ID, standings_df["Team"].unique().tolist())
        st.success("Wrote team list to 'Teams' sheet. In Google Sheets: Data → Data validation → List from a range = Teams!A2:A")
with colB:
    st.caption("Use the exported list for Google Sheets validation to avoid name mismatches.")

# Footer notes
st.divider()
with st.expander("ℹ️ Notes / Tips"):
    st.markdown(f"""
- **Google Sheet Tab:** `{DRAFT_TAB}` • Columns: `Player, PLYR (<=6), Team, PointType, TeamAbbr (optional)`
- **Enter Team as full name** (e.g., "Oklahoma City Thunder"). Abbreviations go in **TeamAbbr** only.
- Use **Hard refresh** if the app just woke up and standings look stale.
- **Season:** {SEASON} • Source: ESPN (cached ~15 min)
- **Guards:** Max 6 teams per player; a team can't belong to multiple players.
""")
