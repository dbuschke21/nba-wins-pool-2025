# app.py — NBA Wins/Losses Pool Tracker (Google Sheets + ESPN)
# ------------------------------------------------------------
# Features:
# - Reads/writes draft data from Google Sheets (tab 'Draft')
# - Uses ESPN API for live NBA standings (East + West)
# - Scoring: 1 point per Win (or Loss) based on PointType
# - Player Standings, Per-Team Breakdown, Raw Standings
# - Refresh button, export Teams tab, mismatch detection, editor dropdowns
# - Wider "Team" column for readability

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
        df["Player"] = df["Player"].fillna("").astype(str)
        df["Team"] = df["Team"].fillna("").astype(str)
        df["PointType"] = df["PointType"].fillna("Wins").astype(str)
        df["PointType"] = df["PointType"].apply(
            lambda x: "Wins" if x.strip().lower().startswith("win") else "Losses"
        )
    return df

def write_draft(gc, entries):
    ws = ensure_draft_tab(gc)
    values = [["Player", "Team", "PointType"]]
    for e in entries:
        values.append([e["Player"], e["Team"], e["PointType"]])
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

    rows_map = {}  # team_id -> row dict (to avoid duplicates)

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
            winpct = float(stats.get("winPercent,") if "winPercent," in stats else stats.get("winPercent", 0) or (w / gp if gp else 0))
            rows_map[team_id] = {"Team": name, "Abbr": abbr, "W": w, "L": l, "WinPct": winpct}

    # Newer ESPN format usually has children for East/West; gather all that contain "standings"
    children = data.get("children", [])
    found_any = False
    for ch in children:
        if "standings" in ch:
            harvest(ch["standings"])
            found_any = True

    # Fallback: some shapes have standings at root
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
        "PointType": ["Wins"] * len(DEFAULT_PLAYERS)
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
    st.dataframe(bad, use_container_width=True,
                 column_config={"Team": column_config.TextColumn("Team", width="large")})

# In-app editor with dropdowns
st.sidebar.header("Draft Editor (saves to Google Sheets)")
team_options = sorted(team_list)
editable_df = st.sidebar.data_editor(
    draft_df,
    num_rows="dynamic",
    use_container_width=True,
    hide_index=True,
    column_config={
        "Player": column_config.TextColumn("Player"),
        "Team": column_config.SelectboxColumn("Team", options=[""] + team_options, required=False, width="large"),
        "PointType": column_config.SelectboxColumn("PointType", options=["Wins", "Losses"], required=True),
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
    entries = df_clean[["Player", "Team", "PointType"]].to_dict(orient="records")
    write_draft(gc, entries)
    st.sidebar.success("Draft saved to Google Sheets ✅")
    st.experimental_rerun()

# Calculate standings
player_table, per_team_table = calc_tables(editable_df, standings_df)

# Player standings
st.divider()
st.subheader("🏆 Player Standings (Points-Based)")
st.dataframe(
    player_table[["Player", "Points", "GP", "Point %", "Wins", "Losses", "Win %"]],
    use_container_width=True
)

# Per-team breakdown (with filter) — wider "Team" column
st.divider()
col1, col2 = st.columns([1, 3])
with col1:
    st.subheader("Per-Team Breakdown")
    players = ["All"] + sorted(editable_df["Player"].dropna().unique().tolist())
    who = st.selectbox("Filter by player", players, index=0)
with col2:
    st.write("")

if not per_team_table.empty:
    show_df = per_team_table if who == "All" else per_team_table[per_team_table["Player"] == who]
    st.dataframe(
        show_df[["Player", "Team", "Point Type", "Points", "W", "L", "GP", "Point %", "Win %"]],
        use_container_width=True,
        column_config={
            "Team": column_config.TextColumn("Team", width="large"),
            "Point Type": column_config.TextColumn("Point Type"),
        }
    )
else:
    st.info("Add draft rows (Player, Team, PointType) to your Google Sheet to see results.")

# Raw standings (league-wide) — wider "Team" column
st.divider()
st.subheader("NBA Standings (raw, from ESPN)")
st.dataframe(
    standings_df[["Team", "Abbr", "W", "L", "WinPct"]].rename(columns={"WinPct": "Win %"}),
    use_container_width=True,
    column_config={"Team": column_config.TextColumn("Team", width="large")}
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
- **Google Sheet Tab:** `{DRAFT_TAB}` • **Columns:** `Player, Team, PointType` (`Wins` or `Losses`)
- **Use exact team names** as shown in the raw standings above (or apply data validation using the exported `Teams` tab).
- **Season:** {SEASON} • **Standings source:** ESPN (cached 15 min, combined East + West)
- **Guards:** Max 6 teams per player; a team can't belong to multiple players.
""")
