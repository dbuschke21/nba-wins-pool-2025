# app.py — NBA Wins/Losses Pool Tracker (Google Sheets + ESPN)
# ------------------------------------------------------------
# Tweaks for presentation:
# - Index from 1 (# col)
# - Short headers: PLYR, TM, PT, P, P%, W, L, W%, GP, TMF
# - PT shows W/L
# - Optional TeamAbbr from Google Sheet (fallback to ESPN Abbr)
# - Fixed column widths (50 px)
# - Row color coding by player + legend
# - East+West standings

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

# Palette for players (extend if needed)
PLAYER_COLORS = [
    "#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2",
    "#F2CF5B", "#B279A2", "#FF9DA6", "#9D755D", "#BAB0AC",
]

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
        ws = sh.add_worksheet(title=DRAFT_TAB, rows=200, cols=4)
        ws.update([["Player", "Team", "PointType", "TeamAbbr"]])
    return ws

def read_draft(gc) -> pd.DataFrame:
    ws = ensure_draft_tab(gc)
    rows = ws.get_all_records()
    # Support optional TeamAbbr / Abbr
    df = pd.DataFrame(rows)
    for col in ["Player", "Team", "PointType", "TeamAbbr", "Abbr"]:
        if col not in df.columns:
            df[col] = ""
    df = df[["Player", "Team", "PointType", "TeamAbbr", "Abbr"]].rename(columns={"Abbr": "TeamAbbrSheet"})
    if not df.empty:
        df["Player"] = df["Player"].fillna("").astype(str)
        df["Team"] = df["Team"].fillna("").astype(str)
        df["PointType"] = df["PointType"].fillna("Wins").astype(str)
        df["PointType"] = df["PointType"].apply(
            lambda x: "Wins" if x.strip().lower().startswith("win") else "Losses"
        )
        # prefer explicit TeamAbbr, then Abbr, else blank (we'll fill from ESPN later)
        df["TeamAbbr"] = df["TeamAbbr"].replace("", pd.NA)
        df["TeamAbbrSheet"] = df["TeamAbbrSheet"].replace("", pd.NA)
        df["TeamAbbr"] = df["TeamAbbr"].fillna(df["TeamAbbrSheet"]).fillna("")
    return df[["Player", "Team", "PointType", "TeamAbbr"]]

def write_draft(gc, entries):
    ws = ensure_draft_tab(gc)
    values = [["Player", "Team", "PointType", "TeamAbbr"]]
    for e in entries:
        values.append([e.get("Player",""), e.get("Team",""), e.get("PointType",""), e.get("TeamAbbr","")])
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
@st.cache_data(ttl=900)
def fetch_nba_standings() -> pd.DataFrame:
    """
    Uses ESPN's public JSON API to get live standings across both conferences.
    Returns DataFrame with columns: Team, Abbr, W, L, WinPct
    """
    url = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    rows_map = {}  # team_id -> row dict

    def harvest(standings_block):
        for entry in standings_block.get("entries", []):
            team = entry.get("team", {}) or {}
            team_id = team.get("id") or team.get("uid") or (team.get("displayName") or team.get("name"))
            name = team.get("displayName") or team.get("name")
            abbr = team.get("abbreviation") or team.get("shortDisplayName")
            stats = {s.get("id"): s.get("value") for s in entry.get("stats", []) if "id" in s}
            w = int(stats.get("wins", 0) or 0)
            l = int(stats.get("losses", 0) or 0)
            gp = w + l
            winpct = float(stats.get("winPercent", 0) or (w / gp if gp else 0))
            rows_map[team_id] = {"Team": name, "Abbr": abbr, "W": w, "L": l, "WinPct": winpct}

    children = data.get("children", [])
    found_any = False
    for ch in children:
        if "standings" in ch:
            harvest(ch["standings"])
            found_any = True
    if not found_any and "standings" in data:
        harvest(data["standings"])

    if not rows_map:
        raise RuntimeError("No standings rows parsed from ESPN")

    df = pd.DataFrame(list(rows_map.values()))
    df["Team"] = df["Team"].astype(str)
    df["W"] = df["W"].astype(int)
    df["L"] = df["L"].astype(int)
    df["WinPct"] = df["WinPct"].astype(float)
    return df.sort_values("Team").reset_index(drop=True)

# ----------------------------
# Scoring + helpers
# ----------------------------
def safe_div(num, den, ndigits=3):
    if den == 0:
        return 0.0
    return round(num / den, ndigits)

def build_player_palette(players):
    uniq = list(dict.fromkeys(players))  # stable order
    cmap = {p: PLAYER_COLORS[i % len(PLAYER_COLORS)] for i, p in enumerate(uniq)}
    return cmap

def style_by_player(df, player_col, cmap):
    # Highlight whole row with player's color (light tint)
    def _row_style(r):
        color = cmap.get(r[player_col], "#FFFFFF")
        # lighten color by mixing with white
        return [f"background-color: {color}22"] * len(r)  # add 0x22 alpha
    return df.style.apply(_row_style, axis=1)

# ----------------------------
# Calculations
# ----------------------------
def calc_tables(draft_df: pd.DataFrame, standings: pd.DataFrame):
    team_stats = standings.set_index("Team")[["W", "L", "WinPct"]].to_dict(orient="index")
    abbr_map = standings.set_index("Team")["Abbr"].to_dict()

    rows = []
    for _, r in draft_df.iterrows():
        player = r["Player"]
        team = r["Team"]
        pt = r.get("PointType", "Wins")
        pt_short = "W" if pt == "Wins" else "L"
        s = team_stats.get(team)
        if s is None:
            W = L = 0
            winpct = 0.0
        else:
            W, L, winpct = s["W"], s["L"], s["WinPct"]
        GP = W + L
        points = W if pt == "Wins" else L

        # TeamAbbr preference: sheet's TeamAbbr, else ESPN Abbr, else empty
        tm_abbr = r.get("TeamAbbr", "").strip() or abbr_map.get(team, "")

        rows.append(
            {
                "PLYR": player,
                "TM": tm_abbr,
                "PT": pt_short,
                "P": points,
                "P%": safe_div(points, GP),
                "W": W,
                "L": L,
                "W%": safe_div(W, GP),
                "GP": GP,
                "TMF": team,  # full team name (for per-team; also used to build player teams list)
            }
        )

    per_team_df = pd.DataFrame(rows)

    # Player standings aggregate
    if per_team_df.empty:
        player_table = pd.DataFrame(columns=["PLYR", "P", "GP", "P%", "W", "L", "W%", "TMF"])
    else:
        agg = (
            per_team_df.groupby("PLYR", as_index=False)
            .agg(
                P=("P", "sum"),
                GP=("GP", "sum"),
                W=("W", "sum"),
                L=("L", "sum"),
            )
        )
        agg["P%"] = agg.apply(lambda r: safe_div(r["P"], r["GP"]), axis=1)
        agg["W%"] = agg.apply(lambda r: safe_div(r["W"], r["W"] + r["L"]), axis=1)

        # Add TMF = comma-separated full team names (rightmost column as requested)
        tmf = (
            per_team_df.groupby("PLYR")["TMF"]
            .apply(lambda s: ", ".join(sorted([t for t in s.tolist() if t])))
            .reset_index(name="TMF")
        )
        player_table = agg.merge(tmf, on="PLYR", how="left").fillna({"TMF": ""})

        player_table = player_table.sort_values(["P", "P%", "GP"], ascending=[False, False, False]).reset_index(drop=True)

    # Sort per-team by player then points
    if not per_team_df.empty:
        per_team_df = per_team_df.sort_values(["PLYR", "P", "P%", "W"], ascending=[True, False, False, False]).reset_index(drop=True)

    return player_table, per_team_df

# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="NBA Draft Tracker", page_icon="🏀", layout="wide")
st.title("🏀 NBA Wins/Losses Pool Tracker (Google Sheets + ESPN Live Data)")
st.caption(
    "Draft data is stored in your **Google Sheet** (tab 'Draft'). "
    "Each team earns points based on its 'PointType': 1 per Win or 1 per Loss. "
    "Standings update live from ESPN."
)

# Fetch standings
try:
    standings_df = fetch_nba_standings()
    team_list = standings_df["Team"].tolist()
    st.success(f"✅ Live standings loaded for {SEASON}")
except Exception as e:
    st.error(f"❌ Could not load standings: {e}")
    st.stop()

# Sidebar: refresh
with st.sidebar:
    if st.button("🔄 Refresh data (clear cache)"):
        fetch_nba_standings.clear()
        st.experimental_rerun()

# Google Sheets connection + read
gc = get_sheets_client()
try:
    draft_df = read_draft(gc)
except Exception as e:
    st.error(
        "Google Sheets permission error.\n\n"
        "Make sure this Sheet is shared with:\n"
        f"  **{st.secrets.get('gcp_service_account', {}).get('client_email', '(service-account)')}** (Editor)\n\n"
        f"Details: {e}"
    )
    st.stop()

DEFAULT_PLAYERS = ["Alice", "Bob", "Charlie", "Dana", "Evan"]
if draft_df.empty:
    draft_df = pd.DataFrame({
        "Player": DEFAULT_PLAYERS,
        "Team": ["" for _ in DEFAULT_PLAYERS],
        "PointType": ["Wins"] * len(DEFAULT_PLAYERS),
        "TeamAbbr": ["" for _ in DEFAULT_PLAYERS],
    })

# Mismatch detection (team names vs ESPN)
team_set = set(team_list)
bad = draft_df[
    (draft_df["Team"].fillna("") != "") & (~draft_df["Team"].isin(team_set))
][["Player", "Team"]].copy()
if not bad.empty:
    bad["Suggestions"] = bad["Team"].apply(
        lambda t: ", ".join(difflib.get_close_matches(t, team_list, n=3, cutoff=0.6)) or "(no close match)"
    )
    st.warning("Some team names in your Draft sheet don’t match ESPN’s names. Fix them in the Sheet or via the editor below.")
    st.dataframe(bad, use_container_width=True)

# In-app editor with dropdowns (keeps full names in selector; optional TeamAbbr free text)
st.sidebar.header("Draft Editor (saves to Google Sheets)")
team_options = sorted(team_list)
editable_df = st.sidebar.data_editor(
    draft_df,
    num_rows="dynamic",
    use_container_width=True,
    hide_index=True,
    column_config={
        "Player": column_config.TextColumn("Player", width=50),
        "Team": column_config.SelectboxColumn("Team (full)", options=[""] + team_options, required=False, width=200),
        "PointType": column_config.SelectboxColumn("PointType", options=["Wins", "Losses"], required=True, width=80),
        "TeamAbbr": column_config.TextColumn("TeamAbbr (TM)", width=80),
    }
)

# Save with guards (max 6 per player, no duplicate team across players)
if st.sidebar.button("💾 Save Draft to Google Sheets"):
    df_clean = editable_df.copy().fillna("")
    df_clean = df_clean[df_clean["Team"] != ""]
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
    entries = df_clean[["Player", "Team", "PointType", "TeamAbbr"]].to_dict(orient="records")
    write_draft(gc, entries)
    st.sidebar.success("Draft saved to Google Sheets ✅")
    st.experimental_rerun()

# Calculate standings
player_table_raw, per_team_table_raw = calc_tables(editable_df, standings_df)

# Color map by player
players_order = player_table_raw["PLYR"].tolist() if not player_table_raw.empty else editable_df["Player"].tolist()
cmap = build_player_palette(players_order)

def add_index(df):
    if df.empty:
        return df
    df = df.copy()
    df.insert(0, "#", range(1, len(df) + 1))
    return df

# --- Player Standings (top chart) ---
st.divider()
st.subheader("🏆 Player Standings (Points-Based)")

# Build display table with short headers + TMF at far right
pt_display = player_table_raw[["PLYR", "P", "P%", "W", "L", "W%", "GP", "TMF"]].copy()
pt_display = add_index(pt_display)

# Legend
with st.container():
    st.caption("Player colors")
    legend_cols = st.columns(min(len(cmap), 8) or 1)
    for i, (p, color) in enumerate(cmap.items()):
        with legend_cols[i % len(legend_cols)]:
            st.markdown(f"<div style='display:flex;align-items:center;gap:8px;'>"
                        f"<span style='display:inline-block;width:14px;height:14px;background:{color};border-radius:3px;'></span>"
                        f"<span style='font-size:0.9rem'>{p}</span></div>", unsafe_allow_html=True)

# Style rows by player
styled_pt = style_by_player(pt_display.merge(player_table_raw[["PLYR"]], left_on="PLYR", right_on="PLYR"), "PLYR", cmap)

st.dataframe(
    styled_pt,
    use_container_width=True,
    hide_index=True,
    column_config={
        "#": column_config.NumberColumn("#", width=50),
        "PLYR": column_config.TextColumn("PLYR", width=50),
        "P": column_config.NumberColumn("P", width=50),
        "P%": column_config.NumberColumn("P%", width=50, format="%.3f"),
        "W": column_config.NumberColumn("W", width=50),
        "L": column_config.NumberColumn("L", width=50),
        "W%": column_config.NumberColumn("W%", width=50, format="%.3f"),
        "GP": column_config.NumberColumn("GP", width=50),
        "TMF": column_config.TextColumn("TMF", width=200),  # full team names rightmost (wider for readability)
    }
)

# --- Per-team breakdown ---
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
    # Short headers already applied in calc; ensure order and add index
    cols = ["PLYR", "TM", "PT", "P", "P%", "W", "L", "W%", "GP", "TMF"]
    ptm = ptm[cols]
    if who != "All":
        ptm = ptm[ptm["PLYR"] == who]
    ptm = add_index(ptm)

    styled_ptm = style_by_player(ptm, "PLYR", cmap)

    st.dataframe(
        styled_ptm,
        use_container_width=True,
        hide_index=True,
        column_config={
            "#": column_config.NumberColumn("#", width=50),
            "PLYR": column_config.TextColumn("PLYR", width=50),
            "TM": column_config.TextColumn("TM", width=50),
            "PT": column_config.TextColumn("PT", width=50),
            "P": column_config.NumberColumn("P", width=50),
            "P%": column_config.NumberColumn("P%", width=50, format="%.3f"),
            "W": column_config.NumberColumn("W", width=50),
            "L": column_config.NumberColumn("L", width=50),
            "W%": column_config.NumberColumn("W%", width=50, format="%.3f"),
            "GP": column_config.NumberColumn("GP", width=50),
            "TMF": column_config.TextColumn("TMF", width=200),  # keep readable on breakdown too
        }
    )
else:
    st.info("Add draft rows (Player, Team, PointType) to your Google Sheet to see results.")

# --- Raw standings ---
st.divider()
st.subheader("NBA Standings (raw, from ESPN)")
raw = standings_df[["Team", "Abbr", "W", "L", "WinPct"]].rename(columns={"WinPct": "Win %"})
raw = raw.copy()
raw.insert(0, "#", range(1, len(raw) + 1))
st.dataframe(
    raw,
    use_container_width=True,
    hide_index=True,
    column_config={
        "#": column_config.NumberColumn("#", width=50),
        "Team": column_config.TextColumn("Team", width=200),
        "Abbr": column_config.TextColumn("Abbr", width=50),
        "W": column_config.NumberColumn("W", width=50),
        "L": column_config.NumberColumn("L", width=50),
        "Win %": column_config.NumberColumn("W%", width=50, format="%.3f"),
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
- **Google Sheet Tab:** `{DRAFT_TAB}` • **Columns:** `Player, Team, PointType, TeamAbbr (optional)`
- **TeamAbbr (TM):** If blank, app falls back to ESPN’s abbreviation.
- **Use exact full team names** in `Team` to match ESPN (or validate via the exported `Teams` tab).
- **Season:** {SEASON} • **Standings source:** ESPN (cached ~15 min)
- **Guards:** Max 6 teams per player; a team can't belong to multiple players.
""")