# app.py — NBA Wins/Losses Pool Tracker (Google Sheets + ESPN)
# ------------------------------------------------------------
# This build:
# - Title -> "25–26 NBA Wins Pool"
# - Removes top diagnostics/QA widgets
# - Weekly time-series uses a high-contrast player palette (and matches table player colors)
# - Bottom section split into two tables:
#     * Loss-scoring teams (PT=L) sorted by L desc, with PLYR first
#     * Win-scoring teams  (PT=W) sorted by W desc, with PLYR first
# - Keeps robust ESPN parsing & weekly History logging

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

APP_VERSION = "build-standings-split-2025-11-08"

# ----------------------------
# Configuration
# ----------------------------
SEASON = "2025-26"          # update each year
SHEET_ID = st.secrets["SHEET_ID"]
DRAFT_TAB = "Draft"         # Worksheet for draft data
HISTORY_TAB = "History"     # Worksheet for weekly snapshots

# High-contrast player colors (distinct hues)
PLAYER_COLORS = [
    "#1f77b4",  # strong blue
    "#d62728",  # red
    "#2ca02c",  # green
    "#9467bd",  # purple
    "#ff7f0e",  # orange
    "#17becf",  # cyan
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#7f7f7f",  # gray
    "#bcbd22",  # olive
]

# ---- Streamlit rerun compatibility
def force_rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    else:
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
        "PLYR": column_config.TextColumn("PLYR", width=60),
        "GP":   column_config.NumberColumn("GP", width=40),
        "P":    column_config.NumberColumn("P", width=40),
        "NP":   column_config.NumberColumn("NP", width=40),
        "P%":   column_config.NumberColumn("P%", width=50, format="%.1f"),
        "TMF":  column_config.TextColumn("TMF", width=include_tmf_width),
    }

def compact_cols_config_perteam(include_tmf_width=220):
    return {
        "#":   column_config.NumberColumn("#", width=40),
        "PLYR":column_config.TextColumn("PLYR", width=60),
        "Team":column_config.TextColumn("Team", width=200),
        "Abbr":column_config.TextColumn("Abbr", width=45),
        "PT":  column_config.TextColumn("PT", width=30),
        "W":   column_config.NumberColumn("W", width=40),
        "L":   column_config.NumberColumn("L", width=40),
    }

# ----------------------------
# Calculations
# ----------------------------
def calc_tables(draft_df: pd.DataFrame, standings: pd.DataFrame):
    standings["TeamNorm"] = standings["Team"].apply(normalize_team_name)
    team_stats = standings.set_index("TeamNorm")[["W", "L", "WinPct", "Abbr"]].to_dict(orient="index")

    rows = []
    for _, r in draft_df.iterrows():
        player = r["Player"]
        plyr   = r["PLYR"] or (player[:6] if player else "")
        team   = r["Team"]
        team_key = normalize_team_name(team)
        pt     = r.get("PointType", "Wins")
        pt_short = "W" if pt == "Wins" else "L"

        s = team_stats.get(team_key, {"W":0,"L":0,"WinPct":0.0,"Abbr":""})
        W, L, wpct, abbr = int(s["W"]), int(s["L"]), float(s["WinPct"]), s["Abbr"]

        GP = W + L
        points     = W if pt == "Wins" else L
        non_points = L if pt == "Wins" else W

        rows.append({
            "PLYR": plyr,
            "P_FULL": player,
            "Team": team,          # full team name
            "Abbr": abbr,          # ESPN abbr
            "PT": pt_short,        # W/L scoring type
            "P": int(points),
            "NP": int(non_points),
            "P%": round((points / GP) * 100, 1) if GP else 0.0,
            "W": int(W),
            "L": int(L),
            "W%": round((W / GP) * 100, 1) if GP else 0.0,
            "GP": int(GP),
        })

    per_team_df = pd.DataFrame(rows)

    # ---- Player-level table
    if per_team_df.empty:
        player_table = pd.DataFrame(columns=["PLYR", "GP", "P", "NP", "P%","TMF"])
    else:
        agg = (per_team_df.groupby(["PLYR"], as_index=False)
               .agg(P=("P","sum"), NP=("NP","sum"), GP=("GP","sum")))
        agg["P%"] = round((agg["P"] / agg["GP"].replace(0, pd.NA)) * 100, 1).fillna(0.0)
        tm_list = (per_team_df.groupby("PLYR")["Abbr"]
                   .apply(lambda s: ", ".join([t for t in s.tolist() if t]))
                   .reset_index(name="TMF"))
        player_table = agg.merge(tm_list, on="PLYR", how="left").fillna({"TMF":""})
        player_table = player_table.sort_values(["P%","P","GP"], ascending=[False,False,False]).reset_index(drop=True)

    # ---- Per-team sort (for other uses)
    if not per_team_df.empty:
        per_team_df = per_team_df.sort_values(["P%","P","W"], ascending=[False,False,False]).reset_index(drop=True)

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
        key = ["WeekStart","PLYR"]
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
st.set_page_config(page_title="25–26 NBA Wins Pool", page_icon="🏀", layout="wide")
st.title("25–26 NBA Wins Pool")
st.caption(f"Version: {APP_VERSION}")

# Refresh controls
colR, colBlank = st.columns([1, 9])
with colR:
    if st.button("🔧 Hard refresh (no cache)"):
        fetch_nba_standings.clear()
        force_rerun()

# Fetch standings
try:
    standings_df = fetch_nba_standings()
    team_list = standings_df["Team"].tolist()
except Exception as e:
    st.error(f"❌ Could not load ESPN standings: {e}")
    st.stop()

# Sidebar: refresh + editor
with st.sidebar:
    if st.button("🔄 Refresh data (clear cache)"):
        fetch_nba_standings.clear()
        force_rerun()

gc = get_sheets_client()
draft_df = read_draft(gc)

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

# Colors for legend & styling
plyr_order = player_table_raw["PLYR"].tolist() if not player_table_raw.empty else editable_df["PLYR"].tolist()
cmap = build_player_palette(plyr_order)

# Legend (matches chart & row shading)
if cmap:
    legend_html = "<div style='display:flex;gap:14px;flex-wrap:wrap;align-items:center;'>"
    for plyr, color in cmap.items():
        legend_html += (
            f"<div style='display:flex;align-items:center;gap:8px;'>"
            f"<span style='width:14px;height:14px;background:{color};display:inline-block;border-radius:3px;'></span>"
            f"<span style='font-size:0.95rem'>{plyr}</span></div>"
        )
    legend_html += "</div>"
    st.caption("Player colors")
    st.markdown(legend_html, unsafe_allow_html=True)

def display_with_index(df, col_cfg, colorize=True):
    df = add_index(df)
    styled = style_by_plyr(df, "PLYR", cmap) if colorize and "PLYR" in df.columns else df
    st.dataframe(styled, use_container_width=True, hide_index=True, column_config=col_cfg)

# ---- Player Standings ----
st.divider()
st.subheader("🏆 Player Standings")
pt_display = player_table_raw[["PLYR", "GP", "P", "NP", "P%", "TMF"]].copy()
display_with_index(pt_display, compact_cols_config_player(include_tmf_width=240))

# ---- Update History + Weekly time series (by player) ----
try:
    upsert_history(gc, player_table_raw)
    hist_df = read_history(gc)
    if not hist_df.empty:
        for c in ["P", "GP", "NP", "P%"]:
            hist_df[c] = pd.to_numeric(hist_df[c], errors="coerce")
        hist_df = hist_df.sort_values(["PLYR", "WeekStart"])
        hist_df["PrevP"] = hist_df.groupby("PLYR")["P"].shift(1).fillna(0)
        hist_df["P_week"] = (hist_df["P"] - hist_df["PrevP"]).clip(lower=0)

        # Altair color scale using our player palette (ensures contrast & matches table)
        color_domain = list(cmap.keys())
        color_range  = [cmap[p] for p in color_domain]

        st.subheader("Weekly Points by Player")
        chart = (
            alt.Chart(hist_df)
            .encode(
                x=alt.X("WeekStart:T", title="Week (Mon start)"),
                y=alt.Y("P_week:Q", title="Points (this week)"),
                color=alt.Color("PLYR:N",
                                legend=alt.Legend(orient="top", title=None),
                                scale=alt.Scale(domain=color_domain, range=color_range)),
                tooltip=["WeekStart:T", "PLYR:N", "P_week:Q", "P:Q", "GP:Q", "NP:Q", "P%:Q"],
            )
            .properties(height=320)
        )
        st.altair_chart(chart.mark_line() + chart.mark_point(), use_container_width=True)
except Exception as e:
    st.warning(f"History logging skipped: {e}")

# ----------------------------
# Bottom: split tables by scoring type
# ----------------------------
st.divider()
st.subheader("Teams Scoring on Losses (PT = L) — sorted by Losses")
loss_df = per_team_table_raw[per_team_table_raw["PT"] == "L"][["PLYR","Team","Abbr","PT","W","L"]].copy()
loss_df = loss_df.sort_values(["L","W","Team"], ascending=[False,False,True]).reset_index(drop=True)
if loss_df.empty:
    st.info("No teams configured with Losses scoring yet.")
else:
    # rename headers a bit tighter
    loss_df = loss_df.rename(columns={"Team":"Team","Abbr":"Abbr"})
    display_with_index(loss_df, compact_cols_config_perteam(include_tmf_width=220))

st.subheader("Teams Scoring on Wins (PT = W) — sorted by Wins")
win_df = per_team_table_raw[per_team_table_raw["PT"] == "W"][["PLYR","Team","Abbr","PT","W","L"]].copy()
win_df = win_df.sort_values(["W","L","Team"], ascending=[False,True,True]).reset_index(drop=True)
if win_df.empty:
    st.info("No teams configured with Wins scoring yet.")
else:
    display_with_index(win_df, compact_cols_config_perteam(include_tmf_width=220))

# ----------------------------
# (Optional) export team list for GSheets validation
# ----------------------------
st.divider()
colA, colB = st.columns([1, 3])
with colA:
    if st.button("📤 Export full team list to 'Teams' sheet"):
        export_teams_tab(gc, SHEET_ID, standings_df["Team"].unique().tolist())
        st.success("Wrote team list to 'Teams' sheet (use Data → Data validation → List from a range = Teams!A2:A).")
with colB:
    st.caption("Tip: use the exported list in your Draft tab to avoid name mismatches.")

# Footer
st.caption("Data: ESPN • Cache ~15 min")