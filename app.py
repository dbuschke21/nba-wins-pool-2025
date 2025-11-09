# app.py — NBA Wins/Losses Pool Tracker (Google Sheets + ESPN)
# ------------------------------------------------------------
# This build:
# - Player Standings: PLYR column moved left of GP (order: PLYR, GP, P, NP, P%, TMF)
# - Weekly chart: plots per-week points (delta vs prior week) by PLYR,
#   limited to the FIRST THREE weeks of the season (from History tab).
# - Robust ESPN parser; hard-refresh button with version-proof rerun
# - History tab auto-snapshots weekly totals
# - Compact widths & 1-decimal % values; mismatch diagnostics

import re
import difflib
from datetime import datetime, timezone
import pandas as pd
import requests
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from streamlit import column_config
import altair as alt

APP_VERSION = "build-3wk-weekly-plot"

# ----------------------------
# Configuration
# ----------------------------
SEASON = "2025-26"          # update each year
SHEET_ID = st.secrets["SHEET_ID"]
DRAFT_TAB = "Draft"         # Worksheet for draft data
HISTORY_TAB = "History"     # Worksheet for weekly snapshots

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
# Google Sheets helpers
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

def ensure_tab(sh, title, rows=200, cols=8, header=None):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        if header:
            ws.update([header])
    return ws

def ensure_draft_tab(gc):
    sh = gc.open_by_key(SHEET_ID)
    return ensure_tab(sh, DRAFT_TAB, rows=200, cols=5,
                      header=["Player", "PLYR", "Team", "PointType", "TeamAbbr"])

def ensure_history_tab(gc):
    sh = gc.open_by_key(SHEET_ID)
    return ensure_tab(
        sh, HISTORY_TAB, rows=1000, cols=8,
        header=["DateUTC", "WeekStart", "PLYR", "P", "GP", "NP", "P%"]
    )

def normalize_team_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s

def read_draft(gc) -> pd.DataFrame:
    ws = ensure_draft_tab(gc)
    rows = ws.get_all_records()
    df = pd.DataFrame(rows)

    for col in ["Player", "PLYR", "Team", "PointType", "TeamAbbr", "Abbr"]:
        if col not in df.columns:
            df[col] = ""

    df["Player"] = df["Player"].fillna("").astype(str)
    df["PLYR"]   = df["PLYR"].fillna("").astype(str).apply(lambda s: s[:6])
    df["Team"]   = df["Team"].fillna("").astype(str).apply(normalize_team_name)
    df["PointType"] = (
        df["PointType"].fillna("Wins").astype(str)
        .apply(lambda x: "Wins" if x.strip().lower().startswith("win") else "Losses")
    )

    df["TeamAbbr"] = df.get("TeamAbbr", "").replace("", pd.NA)
    df["Abbr"] = df.get("Abbr", "").replace("", pd.NA)
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
# ESPN Standings fetch (robust)
# ----------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_nba_standings() -> pd.DataFrame:
    """Returns DataFrame with columns: Team, Abbr, W, L, WinPct (0..1)."""
    def extract_w_l_pct_from_entry(team_dict, stats_list, records_list):
        W = L = None
        WPCT = None
        stats = {}
        for s in stats_list or []:
            k = s.get("id") or s.get("name")
            if k:
                stats[k] = s

        def num(k):
            if k in stats:
                v = stats[k].get("value")
                if v is None:
                    dv = stats[k].get("displayValue")
                    if isinstance(dv, (int, float)): return dv
                    if isinstance(dv, str) and dv.isdigit(): return int(dv)
                if isinstance(v, (int, float)): return v
            return None

        W = num("wins") if W is None else W
        L = num("losses") if L is None else L
        WPCT = num("winPercent") if WPCT is None else WPCT
        if WPCT is None:
            WPCT = num("winPercentV2")

        if (W is None or L is None) and records_list:
            for rec in records_list:
                name = (rec.get("name") or rec.get("type") or "").lower()
                if any(k in name for k in ["overall", "total", "regular"]):
                    summary = rec.get("summary") or rec.get("displayValue") or ""
                    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)", summary)
                    if m:
                        W, L = int(m.group(1)), int(m.group(2))
                        break

        if (WPCT is None or WPCT == 0) and isinstance(W, int) and isinstance(L, int) and (W + L) > 0:
            WPCT = W / (W + L)

        W = int(W) if isinstance(W, (int, float)) else 0
        L = int(L) if isinstance(L, (int, float)) else 0
        WPCT = float(WPCT) if isinstance(WPCT, (int, float)) else (W / (W + L) if (W + L) > 0 else 0.0)
        return W, L, WPCT

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
                df["GP"] = df["W"] + df["L"]
                df = df.sort_values(["Team", "GP"], ascending=[True, False]).drop_duplicates("Team", keep="first")
                df = df.drop(columns=["GP"], errors="ignore")
                return df.sort_values("Team").reset_index(drop=True)
        except Exception:
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
    return styler.format({"P%": "{:.1f}"})

def add_index(df):
    if df.empty:
        return df
    df = df.copy()
    df.insert(0, "#", range(1, len(df) + 1))
    return df

def compact_cols_config_player(include_tmf_width=220):
    return {
        "#":    column_config.NumberColumn("#", width=40),
        "PLYR": column_config.TextColumn("PLYR", width=40),   # now left of GP
        "GP":   column_config.NumberColumn("GP", width=40),
        "P":    column_config.NumberColumn("P", width=30),
        "NP":   column_config.NumberColumn("NP", width=30),
        "P%":   column_config.NumberColumn("P%", width=40, format="%.1f"),
        "TMF":  column_config.TextColumn("TMF", width=include_tmf_width),
    }

def compact_cols_config_perteam(include_tmf_width=220):
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
        non_points = L if pt == "Wins" else W
        tm_abbr = (r.get("TeamAbbr", "") or "").strip() or abbr_map.get(team_key, "")

        rows.append({
            "PLYR": plyr,
            "P_FULL": player,
            "TM": tm_abbr,
            "PT": pt_short,
            "P": int(points),
            "NP": int(non_points),
            "P%": round((points / GP) * 100, 1) if GP else 0.0,
            "W": int(W),
            "L": int(L),
            "W%": round((W / GP) * 100, 1) if GP else 0.0,
            "GP": int(GP),
            "TMF": team,
        })

    per_team_df = pd.DataFrame(rows)

    if per_team_df.empty:
        player_table = pd.DataFrame(columns=["PLYR", "GP", "P", "NP", "P%", "TMF"])
    else:
        agg = (per_team_df.groupby(["PLYR"], as_index=False)
               .agg(P=("P", "sum"), NP=("NP", "sum"), GP=("GP", "sum")))
        agg["P%"] = round((agg["P"] / agg["GP"].replace(0, pd.NA)) * 100, 1).fillna(0.0)

        tm_list = (per_team_df.groupby("PLYR")["TM"]
                   .apply(lambda s: ", ".join([t for t in s.tolist() if t]))
                   .reset_index(name="TMF"))
        player_table = agg.merge(tm_list, on="PLYR", how="left").fillna({"TMF": ""})
        player_table = player_table.sort_values(["P%", "P", "GP"], ascending=[False, False, False]).reset_index(drop=True)

    if not per_team_df.empty:
        per_team_df = per_team_df.sort_values(["P%", "P", "W"], ascending=[False, False, False]).reset_index(drop=True)

    return player_table, per_team_df

# ----------------------------
# History (weekly snapshots)
# ----------------------------
def week_start_monday_utc(ts_utc: datetime) -> str:
    p = pd.Timestamp(ts_utc).to_period("W-MON")
    return p.start_time.strftime("%Y-%m-%d")

def read_history(gc) -> pd.DataFrame:
    ws = ensure_history_tab(gc)
    rows = ws.get_all_records()
    if not rows:
        return pd.DataFrame(columns=["DateUTC", "WeekStart", "PLYR", "P", "GP", "NP", "P%"])
    return pd.DataFrame(rows)

def upsert_history(gc, player_table: pd.DataFrame):
    if player_table.empty:
        return
    ws = ensure_history_tab(gc)
    hist = read_history(gc)

    now_utc = datetime.now(timezone.utc)
    week_start = week_start_monday_utc(now_utc)

    new_rows = player_table[["PLYR", "P", "GP", "NP", "P%"]].copy()
    new_rows.insert(0, "WeekStart", week_start)
    new_rows.insert(0, "DateUTC", now_utc.strftime("%Y-%m-%d %H:%M:%S"))

    if not hist.empty:
        key = ["WeekStart", "PLYR"]
        hist = hist.drop_duplicates(subset=key, keep="last")
        cur = pd.concat([hist, new_rows], ignore_index=True)
        cur = cur.sort_values(key + ["DateUTC"]).drop_duplicates(subset=key, keep="last")
    else:
        cur = new_rows

    values = [list(cur.columns)] + cur.astype(str).values.tolist()
    ws.clear()
    ws.update(values)

# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="NBA Draft Tracker", page_icon="🏀", layout="wide")
st.title("🏀 NBA Wins/Losses Pool Tracker")
st.caption(f"Version: {APP_VERSION}")
st.caption(
    "Draft data is stored in your **Google Sheet** (tab 'Draft'). "
    "Each team earns points based on its 'PointType': 1 per Win or 1 per Loss. "
    "Standings update live from ESPN."
)

# Hard refresh
colR, colBlank = st.columns([1, 9])
with colR:
    if st.button("🔧 Hard refresh (no cache)"):
        fetch_nba_standings.clear()
        force_rerun()

# Fetch standings
try:
    standings_df = fetch_nba_standings()
    team_list = standings_df["Team"].tolist()
    st.success(f"✅ Live standings loaded for {SEASON}")
except Exception as e:
    st.error(f"❌ Could not load standings: {e}")
    st.stop()

# Sidebar refresh
with st.sidebar:
    if st.button("🔄 Refresh data (clear cache)"):
        fetch_nba_standings.clear()
        force_rerun()

# Sheets + Draft
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

# Diagnostics
diag_cols = st.columns([1,1,1,5])
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

# Mismatch table
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

# Sidebar editor
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

# ----------------------------
# Build tables
# ----------------------------
player_table_raw, per_team_table_raw = calc_tables(editable_df, standings_df)

# Colors for legend
plyr_order = player_table_raw["PLYR"].tolist() if not player_table_raw.empty else editable_df["PLYR"].tolist()
cmap = build_player_palette(plyr_order)

# Legend
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

def display_with_index(df, col_cfg, colorize=True):
    df = df.copy()
    df.insert(0, "#", range(1, len(df) + 1))
    styled = style_by_plyr(df, "PLYR", cmap) if colorize and "PLYR" in df.columns else df
    st.dataframe(styled, use_container_width=True, hide_index=True, column_config=col_cfg)

# ---- Player Standings (PLYR left of GP; then P, NP, P%, TMF) ----
st.divider()
st.subheader("🏆 Player Standings")
pt_display = player_table_raw[["PLYR", "GP", "P", "NP", "P%", "TMF"]].copy()
display_with_index(pt_display, compact_cols_config_player(include_tmf_width=220))

# ---- Update History + Weekly trend chart (first 3 weeks; per-week points) ----
try:
    # Snapshot weekly totals
    upsert_history(gc, player_table_raw)
    hist_df = read_history(gc)
    if not hist_df.empty:
        for c in ["P", "GP", "NP", "P%"]:
            hist_df[c] = pd.to_numeric(hist_df[c], errors="coerce")
        # compute per-week points = P(current week) - P(previous week) per PLYR
        hist_df = hist_df.sort_values(["PLYR", "WeekStart"])
        hist_df["PrevP"] = hist_df.groupby("PLYR")["P"].shift(1).fillna(0)
        hist_df["P_week"] = (hist_df["P"] - hist_df["PrevP"]).clip(lower=0)

        # restrict to the FIRST three weeks present in History
        weeks_sorted = sorted(hist_df["WeekStart"].unique().tolist())
        first_three = weeks_sorted[:3]
        hist3 = hist_df[hist_df["WeekStart"].isin(first_three)].copy()

        if len(first_three) < 3:
            st.info(f"History has {len(first_three)} week(s). The chart will expand as new weeks appear.")

        # Plot per-week points by PLYR
        base = alt.Chart(hist3).encode(
            x=alt.X("WeekStart:T", title="Week (Mon start)"),
            y=alt.Y("P_week:Q", title="Points (this week)"),
            color=alt.Color("PLYR:N", legend=alt.Legend(orient="top", title=None)),
            tooltip=["WeekStart:T", "PLYR:N", "P_week:Q", "P:Q", "GP:Q", "NP:Q", "P%:Q"]
        )
        st.subheader("Weekly Points (first 3 weeks)")
        st.altair_chart(base.mark_line() + base.mark_point(), use_container_width=True)
    else:
        st.info("History tab initialized. A weekly snapshot will be written automatically and the trend chart will appear on next load.")
except Exception as e:
    st.warning(f"History logging skipped: {e}")

# ---- Per-team breakdown ----
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
        full_to_plyr = editable_df.set_index("Player")["PLYR"].to_dict()
        sel_plyr = full_to_plyr.get(who, None)
        if sel_plyr:
            ptm = ptm[ptm["PLYR"] == sel_plyr]
        else:
            ptm = ptm.iloc[0:0]
    display_with_index(ptm, compact_cols_config_perteam(include_tmf_width=220))
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
- **Tabs:** `{DRAFT_TAB}` and `{HISTORY_TAB}` (auto).
- **Player Standings:** PLYR, GP, **P**, **NP**, P%, TMF.
- **NP:** For Wins-picks it counts that team’s losses; for Losses-picks it counts that team’s wins.
- **History & chart:** snapshots weekly totals; chart shows **per-week points** and is limited to the first 3 weeks available.
- **Season:** {SEASON} • Source: ESPN (cached ~15 min).
""")