# app.py
import json
from pathlib import Path
from typing import Dict, List, Any

import pandas as pd
import requests
import streamlit as st

CONFIG_PATH = Path("draft_config.json")
SEASON = "2025-26"  # <-- update each season

# ----------------------------
# Data source (NBA standings)
# ----------------------------
def fetch_nba_standings() -> pd.DataFrame:
    """
    Returns df with columns: Team, Abbr, W, L, WinPct
    """
    url = "https://stats.nba.com/stats/leaguestandingsv3"
    params = {"LeagueID": "00", "Season": SEASON, "SeasonType": "Regular Season"}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
    }
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    rs = data["resultSets"][0]
    headers = [h["name"] for h in rs["headers"]]
    rows = rs["rowSet"]

    raw = pd.DataFrame(rows, columns=headers)
    keep = raw.rename(
        columns={
            "TeamName": "Team",
            "TeamCity": "City",
            "TeamSlug": "Slug",
            "WINS": "W",
            "LOSSES": "L",
            "WinPCT": "WinPct",
            "TeamTricode": "Abbr",
        }
    )[["Team", "Abbr", "W", "L", "WinPct"]].copy()

    keep["Team"] = keep["Team"].astype(str)
    keep["W"] = keep["W"].astype(int)
    keep["L"] = keep["L"].astype(int)
    keep["WinPct"] = keep["WinPct"].astype(float)
    return keep.sort_values("Team").reset_index(drop=True)

# ----------------------------
# Draft config persistence
# ----------------------------
def default_config() -> Dict[str, List[Dict[str, str]]]:
    # 5 players; replace names as you like
    return {"Brobel": [], "Boner": [], "Snake": [], "Big Dog": [], "Buschkawatomi": []}

def normalize_entries(entries: Any) -> List[Dict[str, str]]:
    """
    Accepts either:
      - list[str] (legacy: just team names)  -> [{'Team': t, 'PointType': 'Wins'}, ...]
      - list[dict] with keys Team/PointType  -> returned as-is (validated)
    """
    out = []
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, str):
                out.append({"Team": item, "PointType": "Wins"})
            elif isinstance(item, dict):
                team = item.get("Team") or item.get("team")
                pt = (item.get("PointType") or item.get("point_type") or "Wins").capitalize()
                pt = "Wins" if pt.lower().startswith("win") else "Losses"
                if team:
                    out.append({"Team": str(team), "PointType": pt})
    return out

def load_config() -> Dict[str, List[Dict[str, str]]]:
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text())
        # migrate/normalize per player
        cfg = {}
        for player, entries in raw.items():
            cfg[player] = normalize_entries(entries)
        return cfg
    return default_config()

def save_config(cfg: Dict[str, List[Dict[str, str]]]):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ----------------------------
# Helper calculations
# ----------------------------
def safe_div(num, den, ndigits=3):
    if den == 0:
        return 0.0
    return round(num / den, ndigits)

def calc_tables(cfg: Dict[str, List[Dict[str, str]]], standings: pd.DataFrame):
    team_stats = standings.set_index("Team")[["W", "L", "WinPct"]].to_dict(orient="index")

    # ----- per-team breakdown -----
    rows = []
    for player, entries in cfg.items():
        for entry in entries:
            team = entry.get("Team")
            pt = entry.get("PointType", "Wins")
            s = team_stats.get(team)
            if s is None:
                # team not found due to naming mismatch, show zeros but keep row visible
                W = L = 0
                WinPct = 0.0
            else:
                W, L, WinPct = s["W"], s["L"], s["WinPct"]

            GP = W + L
            points = W if pt == "Wins" else L
            point_pct = safe_div(points, GP)
            win_pct = safe_div(W, GP)

            rows.append(
                {
                    "Player": player,
                    "Team": team,
                    "Point Type": pt,           # Wins | Losses
                    "Points": points,           # W or L depending on Point Type
                    "W": W,
                    "L": L,
                    "GP": GP,
                    "Point %": point_pct,       # points / GP
                    "Win %": win_pct,           # W / GP
                }
            )

    per_team_df = pd.DataFrame(rows)
    if not per_team_df.empty:
        per_team_df = per_team_df.sort_values(
            ["Player", "Points", "Point %", "W"],
            ascending=[True, False, False, False]
        ).reset_index(drop=True)

    # ----- player standings (aggregate) -----
    if per_team_df.empty:
        player_table = pd.DataFrame(columns=["Player", "Points", "GP", "Point %", "Wins", "Losses", "Win %"])
    else:
        agg = (
            per_team_df.groupby("Player", as_index=False)
            .agg(
                Points=("Points", "sum"),
                GP=("GP", "sum"),
                Wins=("W", "sum"),
                Losses=("L", "sum"),
            )
        )
        agg["Point %"] = agg.apply(lambda r: safe_div(r["Points"], r["GP"]), axis=1)
        agg["Win %"] = agg.apply(lambda r: safe_div(r["Wins"], r["Wins"] + r["Losses"]), axis=1)

        # sort primarily by Points, then Point %, then GP
        player_table = agg.sort_values(
            ["Points", "Point %", "GP"],
            ascending=[False, False, False]
        ).reset_index(drop=True)

    return player_table, per_team_df

# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="NBA Draft Tracker (Wins/Losses Scoring)", page_icon="🏀", layout="wide")
st.title("🏀 NBA Team Draft Tracker — Wins/Losses Scoring")

st.caption(
    "Assign each drafted team a **Point Type** (Wins or Losses). "
    "Scoring: if a team is set to *Wins*, it earns 1 point per win; if set to *Losses*, it earns 1 point per loss. "
    "Config is saved to **draft_config.json**."
)

# Load standings for team list + data
standings_df = None
team_list: List[str] = []
try:
    standings_df = fetch_nba_standings()
    team_list = standings_df["Team"].tolist()
    st.success(f"Live standings loaded for {SEASON}.")
except Exception as e:
    st.warning("Couldn’t load live standings. Upload a CSV with columns Team,W,L (WinPct optional).")
    up = st.file_uploader("Upload standings CSV", type=["csv"])
    if up is not None:
        tmp = pd.read_csv(up)
        if "WinPct" not in tmp.columns and {"W", "L"}.issubset(tmp.columns):
            tmp["WinPct"] = tmp["W"] / (tmp["W"] + tmp["L"]).replace({0: pd.NA})
        need = ["Team", "W", "L", "WinPct"]
        for c in need:
            if c not in tmp.columns:
                st.error(f"CSV missing required column: {c}")
                st.stop()
        standings_df = tmp[need].copy()
        team_list = standings_df["Team"].tolist()
        st.success("Standings loaded from CSV.")

if standings_df is None:
    st.stop()

# Load current (and migrate if needed)
cfg: Dict[str, List[Dict[str, str]]] = load_config()

with st.sidebar:
    st.header("Draft Setup")
    st.write("For each player, pick up to 6 teams and set **Point Type** to Wins or Losses.")

    for player in list(cfg.keys()):
        st.subheader(player)

        # Current teams for this player that still exist in team_list
        current_entries = [e for e in cfg[player] if e.get("Team") in team_list]
        current_teams = [e["Team"] for e in current_entries]

        # Multi-select teams (max 6)
        sel = st.multiselect(
            f"{player} — Teams (max 6)",
            options=team_list,
            default=current_teams,
            max_selections=6,
            key=f"teams_{player}",
        )

        # For each selected team, choose "Wins" or "Losses"
        new_entries: List[Dict[str, str]] = []
        for t in sel:
            # find previous PT if existed, else default to Wins
            prev_pt = next((e["PointType"] for e in current_entries if e["Team"] == t), "Wins")
            pt = st.selectbox(
                f"{t} point type",
                options=["Wins", "Losses"],
                index=0 if prev_pt == "Wins" else 1,
                key=f"pt_{player}_{t}",
            )
            new_entries.append({"Team": t, "PointType": pt})

        cfg[player] = new_entries

    if st.button("💾 Save Draft"):
        save_config(cfg)
        st.toast("Draft saved!", icon="✅")

st.divider()
st.subheader("Player Standings (Points-based)")

player_table, per_team_table = calc_tables(cfg, standings_df)
st.dataframe(
    player_table[["Player", "Points", "GP", "Point %", "Wins", "Losses", "Win %"]],
    use_container_width=True
)

st.divider()
col1, col2 = st.columns([1, 3])
with col1:
    st.subheader("Per-Team Breakdown")
    players = ["All"] + list(cfg.keys())
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
    st.info("Assign teams and point types in the sidebar to see the breakdown.")

st.divider()
with st.expander("Notes / Tips"):
    st.markdown(f"""
- **Season:** Currently set to `{SEASON}`. Update the `SEASON` constant at the top for a new year.
- **Scoring logic:** If **Point Type = Wins**, Points = W; if **Losses**, Points = L. `Point % = Points / GP`.
- **Ties/PPD games:** NBA has no ties; GP = W + L from standings.
- **Persistence:** `draft_config.json` is local/ephemeral on Streamlit Cloud. For durable shared storage, we can swap this to Google Sheets / Supabase / Postgres / S3.
- **Legacy configs:** Old configs with only team names get auto-migrated with `PointType = "Wins"` by default.
""")
