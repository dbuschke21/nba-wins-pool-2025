# app.py — NBA Wins/Losses Pool Tracker
# -------------------------------------
# Features:
# - Reads/writes draft data from Google Sheets
# - Uses ESPN API for live NBA standings
# - Scoring: 1 point per Win (or Loss) based on your draft designation
# - Displays Player Standings and Per-Team Breakdown

import pandas as pd
import requests
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# ----------------------------
# Configuration
# ----------------------------
SEASON = "2025-26"  # update each year
SHEET_ID = st.secrets["SHEET_ID"]
DRAFT_TAB = "Draft"  # Worksheet tab in your Google Sheet

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
        ws = sh.add_worksheet(title=DRAFT_TAB, rows=200, cols=3)
        ws.update([["Player", "Team", "PointType"]])
    return ws

def read_draft(gc) -> pd.DataFrame:
    ws = ensure_draft_tab(gc)
    rows = ws.get_all_records()
    df = pd.DataFrame(rows, columns=["Player", "Team", "PointType"])
    if not df.empty:
        df["PointType"] = df["PointType"].fillna("Wins").astype(str)
        df["PointType"] = df["PointType"].apply(
            lambda x: "Wins" if x.lower().startswith("win") else "Losses"
        )
    return df

def write_draft(gc, entries):
    ws = ensure_draft_tab(gc)
    values = [["Player", "Team", "PointType"]]
    for e in entries:
        values.append([e["Player"], e["Team"], e["PointType"]])
    ws.clear()
    ws.update(values)

# ----------------------------
# ESPN Standings fetch
# ----------------------------
@st.cache_data(ttl=900)
def fetch_nba_standings() -> pd.DataFrame:
    """
    Uses ESPN's public JSON API to get live standings.
    Returns DataFrame with columns: Team, Abbr, W, L, WinPct
    """
    url = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    children = data.get("children", [])
    standings_block = None
    for ch in children:
        if "standings" in ch:
            standings_block = ch["standings"]
            break

    if standings_block is None:
        raise RuntimeError("ESPN standings format not found")

    rows = []
    for entry in standings_block.get("entries", []):
        team = entry.get("team", {})
        name = team.get("displayName") or team.get("name")
        abbr = team.get("abbreviation") or team.get("shortDisplayName")

        stats = {s.get("id"): s.get("value") for s in entry.get("stats", []) if "id" in s}
        w = int(stats.get("wins", 0) or 0)
        l = int(stats.get("losses", 0) or 0)
        gp = w + l
        winpct = float(stats.get("winPercent", 0) or (w / gp if gp else 0))

        rows.append({"Team": name, "Abbr": abbr, "W": w, "L": l, "WinPct": winpct})

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No standings rows parsed from ESPN")

    df["Team"] = df["Team"].astype(str)
    df["W"] = df["W"].astype(int)
    df["L"] = df["L"].astype(int)
    df["WinPct"] = df["WinPct"].astype(float)
    return df.sort_values("Team").reset_index(drop=True)

# ----------------------------
# Scoring + aggregation
# ----------------------------
def safe_div(num, den, ndigits=3):
    if den == 0:
        return 0.0
    return round(num / den, ndigits)

def calc_tables(draft_df: pd.DataFrame, standings: pd.DataFrame):
    team_stats = standings.set_index("Team")[["W", "L", "WinPct"]].to_dict(orient="index")

    rows = []
    for _, r in draft_df.iterrows():
        player = r["Player"]
        team = r["Team"]
        pt = r.get("PointType", "Wins")
        s = team_stats.get(team)
        if s is None:
            W = L = 0
        else:
            W, L = s["W"], s["L"]
        GP = W + L
        points = W if pt == "Wins" else L
        rows.append(
            {
                "Player": player,
                "Team": team,
                "Point Type": pt,
                "Points": points,
                "W": W,
                "L": L,
                "GP": GP,
                "Point %": safe_div(points, GP),
                "Win %": safe_div(W, GP),
            }
        )

    per_team_df = pd.DataFrame(rows)
    if not per_team_df.empty:
        per_team_df = per_team_df.sort_values(
            ["Player", "Points", "Point %", "W"],
            ascending=[True, False, False, False]
        ).reset_index(drop=True)

    if per_team_df.empty:
        player_table = pd.DataFrame(columns=["Player", "Points", "GP", "Point %", "Wins", "Losses", "Win %"])
    else:
        agg = (
            per_team_df.groupby("Player", as_index=False)
            .agg(Points=("Points", "sum"), GP=("GP", "sum"), Wins=("W", "sum"), Losses=("L", "sum"))
        )
        agg["Point %"] = agg.apply(lambda r: safe_div(r["Points"], r["GP"]), axis=1)
        agg["Win %"]   = agg.apply(lambda r: safe_div(r["Wins"], r["Wins"] + r["Losses"]), axis=1)
        player_table = agg.sort_values(["Points", "Point %", "GP"], ascending=[False, False, False]).reset_index(drop=True)

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

# Google Sheets connection
gc = get_sheets_client()
draft_df = read_draft(gc)

DEFAULT_PLAYERS = ["Alice", "Bob", "Charlie", "Dana", "Evan"]
if draft_df.empty:
    draft_df = pd.DataFrame({
        "Player": DEFAULT_PLAYERS,
        "Team": ["" for _ in DEFAULT_PLAYERS],
        "PointType": ["Wins"] * len(DEFAULT_PLAYERS)
    })

with st.sidebar:
    st.header("Draft Editor (saves to Google Sheets)")
    st.write("Edit rows or add new ones below. Columns: **Player, Team, PointType (Wins/Losses)**")

    editable_df = st.data_editor(
        draft_df,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True
    )

    if st.button("💾 Save Draft to Google Sheets"):
        entries = editable_df[["Player", "Team", "PointType"]].fillna("").to_dict(orient="records")
        write_draft(gc, entries)
        st.success("Draft saved to Google Sheets ✅")
        draft_df = read_draft(gc)  # refresh

# Calculate standings
player_table, per_team_table = calc_tables(draft_df, standings_df)

# Main content
st.divider()
st.subheader("🏆 Player Standings (Points-Based)")
st.dataframe(
    player_table[["Player", "Points", "GP", "Point %", "Wins", "Losses", "Win %"]],
    use_container_width=True
)

st.divider()
col1, col2 = st.columns([1, 3])
with col1:
    st.subheader("Per-Team Breakdown")
    players = ["All"] + sorted(draft_df["Player"].dropna().unique().tolist())
    who = st.selectbox("Filter by player", players, index=0)
with col2:
    st.write("")

if not per_team_table.empty:
    show_df = per_team_table if who == "All" else per_team_table[per_team_table["Player"] == who]
    st.dataframe(
        show_df[["Player", "Team", "Point Type", "Points", "W", "L", "GP", "Point %", "Win %"]],
        use_container_width=True
    )
else:
    st.info("Add draft rows (Player, Team, PointType) to your Google Sheet to see results.")

st.divider()
with st.expander("ℹ️ Notes / Tips"):
    st.markdown(f"""
- **Google Sheet Tab:** `{DRAFT_TAB}`
- **Columns Required:** Player, Team, PointType (`Wins` or `Losses`)
- **Sheet ID:** `{SHEET_ID}`
- **Season:** {SEASON}
- **Standings Source:** ESPN public API (cached 15 min)
- **Persistence:** Draft data is stored permanently in your Google Sheet
""")
