"""
Dynamic trivia rules -- questions built live from real data rather than
fixed thresholds.

Categories:
  TEAMMATE       - "Draft a RB who was ever teammates with [player]"
                   (or, when you have prior picks: "...with your RB1 pick,
                   [Name]" -- cross-references your own draft)
  COMPARE_STAT   - "Draft a WR with more receiving yards in a season than
                   [player] had in [year], but fewer than [player] had in
                   [year]"
  COLLEGE        - "Draft a player who went to [random qualifying college]"
  COLLEGE_MATCH  - "Draft a player who went to the same college as
                   [player]" (or your own prior pick, same cross-reference)
  EITHER_OR      - "Draft a RB with EITHER 100+ receptions OR 1,000+ rushing
                   yards -- but NOT both -- in a season between 2011-2019"
  TEAM_STAT      - "Draft a RB with 1,600+ rushing yards in a season -- but
                   ONLY while playing for the Dallas Cowboys" -- restricts a
                   hard-tier stat threshold to one specific team (verified
                   to actually have a qualifying player before generating),
                   so the globally-best all-time player usually can't be
                   used as a lazy default answer.

Anchors are picked with ORDER BY RANDOM() directly from the database, or
(for TEAMMATE/COLLEGE_MATCH) preferentially drawn from YOUR OWN prior picks
in the current draft when available, rather than a fixed curated name list.

RULE_ID FORMAT: fields are joined with "|" (pipe), never underscore, since
stat column names like "receiving_yards" already contain underscores.

    dyn|teammate|{position}|{anchor_player_id}
    dyn|comparestat|{position}|{stat_col}|{low_id}|{low_season}|{high_id}|{high_season}
    dyn|college|{position}|{college_name}
    dyn|collegematch|{position}|{anchor_player_id}
    dyn|eitheror|{position}|{statA}|{threshA}|{statB}|{threshB}|{yearLow}|{yearHigh}
    dyn|teamstat|{position}|{stat}|{threshold}|{team_abbr}

Everything needed to re-validate a pick is encoded in the rule_id, and
re-verified fresh against the database at validation time.
"""

import random

DIFFICULTY_BY_CATEGORY = {
    "COLLEGE": "easy",
    "TEAMMATE": "medium",
    "COLLEGE_MATCH": "medium",
    "COMPARE_STAT": "hard",
    "EITHER_OR": "hard",
    "TEAM_STAT": "hard",
}

STAT_BY_POSITION = {
    "QB": ("passing_yards", "passing yards"),
    "RB": ("rushing_yards", "rushing yards"),
    "WR": ("receiving_yards", "receiving yards"),
    "TE": ("receiving_yards", "receiving yards"),
}

STAT_LABELS = {
    "passing_yards": "passing yards", "passing_tds": "passing touchdowns",
    "rushing_yards": "rushing yards", "rushing_tds": "rushing touchdowns",
    "receiving_yards": "receiving yards", "receiving_tds": "receiving touchdowns",
    "receptions": "receptions",
}

EITHER_OR_PAIRS = {
    "QB": [("passing_yards", 4000, "rushing_yards", 400),
           ("passing_tds", 30, "interceptions", 15)],
    "RB": [("receptions", 60, "rushing_yards", 1200),
           ("rushing_tds", 12, "receptions", 50)],
    "WR": [("receiving_yards", 1200, "receptions", 100),
           ("receiving_tds", 10, "receiving_yards", 1000)],
    "TE": [("receiving_yards", 900, "receptions", 75),
           ("receiving_tds", 8, "receiving_yards", 700)],
}

# Hard-tier single-stat thresholds used by TEAM_STAT -- deliberately the
# same tier that tends to always resolve to the same all-time-great player
# when unrestricted. Restricting by team breaks that pattern.
TEAM_STAT_DEFS = {
    "QB": [("passing_yards", 4500), ("passing_tds", 35)],
    "RB": [("rushing_yards", 1400), ("rushing_tds", 14)],
    "WR": [("receiving_yards", 1300), ("receptions", 90)],
    "TE": [("receiving_yards", 900), ("receiving_tds", 8)],
}

TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

YEAR_WINDOW_LENGTH = {"easy": 12, "medium": 8, "hard": 5}

MIN_GAMES_THRESHOLD = 8
MIN_COLLEGE_POOL_SIZE = 3
CURRENT_SEASON = 2025
CROSS_PICK_PREFERENCE = 0.65  # chance to anchor on YOUR prior pick vs a random DB player


def _pick_random_player_season(cur, position, stat_col, exclude_ids=None):
    exclude_ids = exclude_ids or set()
    cur.execute(
        f"""
        SELECT player_id, display_name, season, {stat_col} AS val
        FROM trivia_player_seasons
        WHERE position = %s AND games_played >= %s AND {stat_col} > 0
        ORDER BY RANDOM()
        LIMIT 20
        """,
        (position, MIN_GAMES_THRESHOLD),
    )
    for row in cur.fetchall():
        if row["player_id"] not in exclude_ids:
            return row["player_id"], row["display_name"], row["season"], row["val"]
    return None, None, None, None


def _pick_random_player(cur, position, exclude_ids=None):
    exclude_ids = exclude_ids or set()
    cur.execute(
        """
        SELECT player_id, display_name FROM (
            SELECT DISTINCT player_id, display_name
            FROM trivia_player_seasons
            WHERE position = %s AND games_played >= %s
        ) AS distinct_players
        ORDER BY RANDOM()
        LIMIT 20
        """,
        (position, MIN_GAMES_THRESHOLD),
    )
    for row in cur.fetchall():
        if row["player_id"] not in exclude_ids:
            return row["player_id"], row["display_name"]
    return None, None


def _pick_random_player_any_position(cur):
    cur.execute(
        """
        SELECT s.player_id, p.display_name, p.college
        FROM trivia_player_seasons s
        JOIN players p ON p.player_id = s.player_id
        WHERE s.games_played >= %s AND p.college IS NOT NULL AND p.college != ''
        ORDER BY RANDOM()
        LIMIT 1
        """,
        (MIN_GAMES_THRESHOLD,),
    )
    row = cur.fetchone()
    if row:
        return row["player_id"], row["display_name"], row["college"]
    return None, None, None


def _get_player_display_name(cur, player_id):
    cur.execute("SELECT display_name FROM players WHERE player_id = %s", (player_id,))
    row = cur.fetchone()
    return row["display_name"] if row else None


def _pick_random_qualifying_college(cur, position):
    cur.execute(
        """
        SELECT p.college
        FROM players p
        JOIN trivia_player_seasons s ON s.player_id = p.player_id
        WHERE s.position = %s AND p.college IS NOT NULL AND p.college != ''
        GROUP BY p.college
        HAVING COUNT(DISTINCT p.player_id) >= %s
        ORDER BY RANDOM()
        LIMIT 1
        """,
        (position, MIN_COLLEGE_POOL_SIZE),
    )
    row = cur.fetchone()
    return row["college"] if row else None


def _pick_random_qualifying_college_check(cur, college, position, min_pool=2):
    cur.execute(
        """SELECT COUNT(DISTINCT p.player_id) AS c FROM players p
           JOIN trivia_player_seasons s ON s.player_id = p.player_id
           WHERE p.college ILIKE %s AND s.position = %s""",
        (f"%{college}%", position),
    )
    row = cur.fetchone()
    return (row["c"] or 0) >= min_pool


def _anchor_team_seasons(cur, player_id):
    cur.execute(
        "SELECT DISTINCT season, team FROM trivia_player_seasons WHERE player_id = %s AND team IS NOT NULL",
        (player_id,),
    )
    return {(r["season"], r["team"]) for r in cur.fetchall()}


def _anchor_college(cur, player_id):
    cur.execute("SELECT college FROM players WHERE player_id = %s", (player_id,))
    row = cur.fetchone()
    return row["college"] if row and row["college"] else None


def _stat_value_for_season(cur, player_id, position, stat_col, season):
    cur.execute(
        f"SELECT {stat_col} AS val FROM trivia_player_seasons WHERE player_id = %s AND position = %s AND season = %s",
        (player_id, position, season),
    )
    row = cur.fetchone()
    return row["val"] if row and row["val"] is not None else None


def _data_year_bounds(cur):
    cur.execute("SELECT MIN(season) AS lo, MAX(season) AS hi FROM trivia_player_seasons")
    row = cur.fetchone()
    lo = row["lo"] if row and row["lo"] else 1970
    hi = row["hi"] if row and row["hi"] else CURRENT_SEASON
    return lo, hi


def _roll_year_window(cur, difficulty):
    lo, hi = _data_year_bounds(cur)
    length = YEAR_WINDOW_LENGTH.get(difficulty, 8)
    if hi - lo <= length:
        return lo, hi
    start = random.randint(lo, hi - length)
    return start, start + length


def _pool_size_for_xor(cur, position, stat_a, thresh_a, stat_b, thresh_b, year_lo, year_hi, min_pool=3):
    cur.execute(
        f"""
        SELECT COUNT(*) AS c FROM trivia_player_seasons
        WHERE position = %s AND season BETWEEN %s AND %s
          AND (({stat_a} >= %s) != ({stat_b} >= %s))
        """,
        (position, year_lo, year_hi, thresh_a, thresh_b),
    )
    row = cur.fetchone()
    return (row["c"] or 0) >= min_pool


def _pick_random_qualifying_team(cur, position, stat_col, threshold, min_pool=1):
    """Finds teams that actually have at least one qualifying player, so the
    generated question is never impossible to answer."""
    cur.execute(
        f"""
        SELECT team, COUNT(*) AS c FROM trivia_player_seasons
        WHERE position = %s AND {stat_col} >= %s AND team IS NOT NULL
        GROUP BY team
        HAVING COUNT(*) >= %s
        ORDER BY RANDOM()
        LIMIT 1
        """,
        (position, threshold, min_pool),
    )
    row = cur.fetchone()
    return row["team"] if row else None


def try_generate_dynamic_question(cur, position: str, difficulty: str, prior_picks: dict = None):
    """
    prior_picks: optional dict of {slot_key: player_id} for picks already
    made THIS draft (e.g. {"QB": "00-0031234", "RB1": "00-0029876"}).
    When present, TEAMMATE/COLLEGE_MATCH generation prefers to anchor on
    one of these rather than a random DB player.
    """
    prior_picks = prior_picks or {}
    eligible_categories = [c for c, d in DIFFICULTY_BY_CATEGORY.items() if d == difficulty]
    if not eligible_categories:
        return None
    category = random.choice(eligible_categories)

    if category == "TEAMMATE":
        use_cross_pick = prior_picks and random.random() < CROSS_PICK_PREFERENCE
        if use_cross_pick:
            slot_key, anchor_id = random.choice(list(prior_picks.items()))
            anchor_name = _get_player_display_name(cur, anchor_id)
            if anchor_name:
                team_seasons = _anchor_team_seasons(cur, anchor_id)
                if team_seasons:
                    rule_id = f"dyn|teammate|{position}|{anchor_id}"
                    return {
                        "slot_key": None, "rule_id": rule_id, "category": "TEAMMATE",
                        "scope": "season", "difficulty": difficulty, "position": position,
                        "title": f"Draft a {position} who was ever teammates with your {slot_key} pick, {anchor_name}",
                        "description": f"Player must have shared a team AND season with {anchor_name} (your {slot_key} pick) at some point in their career.",
                    }
        anchor_id, anchor_name = _pick_random_player(cur, position)
        if not anchor_id:
            return None
        team_seasons = _anchor_team_seasons(cur, anchor_id)
        if not team_seasons:
            return None
        rule_id = f"dyn|teammate|{position}|{anchor_id}"
        return {
            "slot_key": None, "rule_id": rule_id, "category": "TEAMMATE",
            "scope": "season", "difficulty": difficulty, "position": position,
            "title": f"Draft a {position} who was ever teammates with {anchor_name}",
            "description": f"Player must have shared a team AND season with {anchor_name} at some point in their career.",
        }

    if category == "COLLEGE_MATCH":
        use_cross_pick = prior_picks and random.random() < CROSS_PICK_PREFERENCE
        if use_cross_pick:
            slot_key, anchor_id = random.choice(list(prior_picks.items()))
            anchor_name = _get_player_display_name(cur, anchor_id)
            college = _anchor_college(cur, anchor_id) if anchor_name else None
            if college and _pick_random_qualifying_college_check(cur, college, position, min_pool=2):
                rule_id = f"dyn|collegematch|{position}|{anchor_id}"
                return {
                    "slot_key": None, "rule_id": rule_id, "category": "COLLEGE_MATCH",
                    "scope": "season", "difficulty": difficulty, "position": position,
                    "title": f"Draft a {position} who went to the same college as your {slot_key} pick, {anchor_name}",
                    "description": f"Player's college must match {anchor_name}'s college (your {slot_key} pick, and a different player).",
                }
        for _ in range(8):
            anchor_id, anchor_name, college = _pick_random_player_any_position(cur)
            if not anchor_id or not college:
                continue
            if not _pick_random_qualifying_college_check(cur, college, position):
                continue
            rule_id = f"dyn|collegematch|{position}|{anchor_id}"
            return {
                "slot_key": None, "rule_id": rule_id, "category": "COLLEGE_MATCH",
                "scope": "season", "difficulty": difficulty, "position": position,
                "title": f"Draft a {position} who went to the same college as {anchor_name}",
                "description": f"Player's college must match {anchor_name}'s college (a different player, not {anchor_name} themselves).",
            }
        return None

    if category == "COMPARE_STAT":
        stat_col, stat_label = STAT_BY_POSITION.get(position, ("fantasy_pts_ppr", "fantasy points"))
        for _ in range(8):
            a_id, a_name, a_season, a_val = _pick_random_player_season(cur, position, stat_col)
            if not a_id:
                continue
            b_id, b_name, b_season, b_val = _pick_random_player_season(cur, position, stat_col, exclude_ids={a_id})
            if not b_id:
                continue
            if abs(a_val - b_val) < 200:
                continue
            if a_val < b_val:
                low_id, low_name, low_season, low_val = a_id, a_name, a_season, a_val
                high_id, high_name, high_season, high_val = b_id, b_name, b_season, b_val
            else:
                low_id, low_name, low_season, low_val = b_id, b_name, b_season, b_val
                high_id, high_name, high_season, high_val = a_id, a_name, a_season, a_val
            rule_id = f"dyn|comparestat|{position}|{stat_col}|{low_id}|{low_season}|{high_id}|{high_season}"
            return {
                "slot_key": None, "rule_id": rule_id, "category": "COMPARE_STAT",
                "scope": "season", "difficulty": difficulty, "position": position,
                "title": (
                    f"Draft a {position} with more {stat_label} in a season than "
                    f"{low_name} had in {low_season}, but fewer than {high_name} had in {high_season}"
                ),
                "description": f"Chosen season's {stat_label} must fall strictly between those two real seasons.",
            }
        return None

    if category == "COLLEGE":
        college = _pick_random_qualifying_college(cur, position)
        if not college:
            return None
        rule_id = f"dyn|college|{position}|{college}"
        return {
            "slot_key": None, "rule_id": rule_id, "category": "COLLEGE",
            "scope": "season", "difficulty": difficulty, "position": position,
            "title": f"Draft a {position} who went to {college}",
            "description": f"Player's college must be {college}.",
        }

    if category == "EITHER_OR":
        pairs = EITHER_OR_PAIRS.get(position, [])
        if not pairs:
            return None
        stat_a, thresh_a, stat_b, thresh_b = random.choice(pairs)
        for _ in range(6):
            year_lo, year_hi = _roll_year_window(cur, difficulty)
            if _pool_size_for_xor(cur, position, stat_a, thresh_a, stat_b, thresh_b, year_lo, year_hi):
                label_a = STAT_LABELS.get(stat_a, stat_a.replace("_", " "))
                label_b = STAT_LABELS.get(stat_b, stat_b.replace("_", " "))
                rule_id = f"dyn|eitheror|{position}|{stat_a}|{thresh_a}|{stat_b}|{thresh_b}|{year_lo}|{year_hi}"
                return {
                    "slot_key": None, "rule_id": rule_id, "category": "EITHER_OR",
                    "scope": "season", "difficulty": difficulty, "position": position,
                    "title": (
                        f"Draft a {position} with EITHER {thresh_a:,}+ {label_a} OR {thresh_b:,}+ {label_b} "
                        f"\u2014 but NOT both \u2014 in a season between {year_lo} and {year_hi}"
                    ),
                    "description": (
                        f"Chosen season (between {year_lo}-{year_hi}) must clear exactly ONE of these two bars. "
                        f"Clearing both, or neither, counts as a miss."
                    ),
                }
        return None

    if category == "TEAM_STAT":
        defs = TEAM_STAT_DEFS.get(position, [])
        if not defs:
            return None
        random.shuffle(defs)
        for stat, threshold in defs:
            team = _pick_random_qualifying_team(cur, position, stat, threshold)
            if not team:
                continue
            label = STAT_LABELS.get(stat, stat.replace("_", " "))
            team_name = TEAM_NAMES.get(team, team)
            rule_id = f"dyn|teamstat|{position}|{stat}|{threshold}|{team}"
            return {
                "slot_key": None, "rule_id": rule_id, "category": "TEAM_STAT",
                "scope": "season", "difficulty": difficulty, "position": position,
                "title": f"Draft a {position} with {threshold:,}+ {label} in a season \u2014 but ONLY while playing for the {team_name}",
                "description": f"Chosen season must clear {threshold:,}+ {label} AND the player's team that season must be the {team_name}.",
            }
        return None

    return None


def is_dynamic_rule(rule_id: str) -> bool:
    return rule_id.startswith("dyn|")


def validate_dynamic_pick(cur, rule_id: str, match_row: dict, player_id: str, position: str):
    parts = rule_id.split("|")
    category = parts[1] if len(parts) > 1 else ""

    if category == "teammate":
        anchor_id = parts[3]
        team_seasons = _anchor_team_seasons(cur, anchor_id)
        pick_key = (match_row["season"], match_row["team"])
        if pick_key in team_seasons:
            return True, None
        return False, f"{match_row['team']} in {match_row['season']} was never a season/team shared with this anchor player"

    if category == "comparestat":
        stat_col = parts[3]
        low_id, low_season = parts[4], int(parts[5])
        high_id, high_season = parts[6], int(parts[7])
        low_val = _stat_value_for_season(cur, low_id, position, stat_col, low_season)
        high_val = _stat_value_for_season(cur, high_id, position, stat_col, high_season)
        if low_val is None or high_val is None:
            return False, "Could not re-verify anchor stats"
        pick_val = match_row.get(stat_col) or 0
        if low_val < pick_val < high_val:
            return True, None
        return False, f"{pick_val} {stat_col.replace('_',' ')} in {match_row['season']} is not strictly between {low_val} and {high_val}"

    if category == "college":
        college = parts[3]
        cur.execute("SELECT college FROM players WHERE player_id = %s", (player_id,))
        row = cur.fetchone()
        player_college = row["college"] if row else None
        if player_college and college.lower() in player_college.lower():
            return True, None
        return False, f"Player's college ({player_college or 'unknown'}) does not match {college}"

    if category == "collegematch":
        anchor_id = parts[3]
        if player_id == anchor_id:
            return False, "You must pick a different player, not the anchor player themselves"
        cur.execute("SELECT college FROM players WHERE player_id = %s", (anchor_id,))
        arow = cur.fetchone()
        anchor_college = arow["college"] if arow else None
        cur.execute("SELECT college FROM players WHERE player_id = %s", (player_id,))
        prow = cur.fetchone()
        player_college = prow["college"] if prow else None
        if anchor_college and player_college and anchor_college.lower() == player_college.lower():
            return True, None
        return False, f"Player's college ({player_college or 'unknown'}) does not match the anchor's college ({anchor_college or 'unknown'})"

    if category == "eitheror":
        stat_a, thresh_a = parts[3], int(parts[4])
        stat_b, thresh_b = parts[5], int(parts[6])
        year_lo, year_hi = int(parts[7]), int(parts[8])

        season = match_row["season"]
        if not (year_lo <= season <= year_hi):
            return False, f"{season} is outside the required window ({year_lo}-{year_hi})"

        val_a = match_row.get(stat_a) or 0
        val_b = match_row.get(stat_b) or 0
        cond_a = val_a >= thresh_a
        cond_b = val_b >= thresh_b

        label_a = STAT_LABELS.get(stat_a, stat_a.replace("_", " "))
        label_b = STAT_LABELS.get(stat_b, stat_b.replace("_", " "))

        if cond_a and cond_b:
            return False, f"Cleared BOTH bars ({val_a} {label_a}, {val_b} {label_b}) \u2014 this counts as a miss, needs exactly ONE"
        if not cond_a and not cond_b:
            return False, f"Cleared NEITHER bar ({val_a} {label_a}, {val_b} {label_b}) \u2014 needs exactly ONE"
        return True, None

    if category == "teamstat":
        # dyn|teamstat|{position}|{stat}|{threshold}|{team_abbr}
        stat = parts[3]
        threshold = int(parts[4])
        team_abbr = parts[5]

        val = match_row.get(stat) or 0
        if val < threshold:
            label = STAT_LABELS.get(stat, stat.replace("_", " "))
            return False, f"{val} {label} in {match_row['season']} \u2014 needs {threshold:,}+"
        if (match_row.get("team") or "").upper() != team_abbr.upper():
            team_name = TEAM_NAMES.get(team_abbr, team_abbr)
            return False, f"Played for {match_row.get('team')} in {match_row['season']}, not the {team_name}"
        return True, None

    return False, "Unrecognized dynamic rule"


def build_dynamic_pool_query(rule_id: str, position: str):
    parts = rule_id.split("|")
    category = parts[1] if len(parts) > 1 else ""

    if category == "teammate":
        anchor_id = parts[3]
        sql = """
            SELECT s.fantasy_pts_ppr AS sort_val, s.player_id, s.season
            FROM trivia_player_seasons s
            WHERE s.position = %s
              AND (s.season, s.team) IN (
                  SELECT DISTINCT season, team FROM trivia_player_seasons
                  WHERE player_id = %s AND team IS NOT NULL
              )
        """
        return sql, [position, anchor_id], "season"

    if category == "comparestat":
        stat_col = parts[3]
        low_id, low_season = parts[4], int(parts[5])
        high_id, high_season = parts[6], int(parts[7])
        sql = f"""
            SELECT fantasy_pts_ppr AS sort_val, player_id, season
            FROM trivia_player_seasons
            WHERE position = %s
              AND {stat_col} > (SELECT {stat_col} FROM trivia_player_seasons WHERE player_id = %s AND position = %s AND season = %s)
              AND {stat_col} < (SELECT {stat_col} FROM trivia_player_seasons WHERE player_id = %s AND position = %s AND season = %s)
        """
        return sql, [position, low_id, position, low_season, high_id, position, high_season], "season"

    if category == "college":
        college = parts[3]
        sql = """
            SELECT s.fantasy_pts_ppr AS sort_val, s.player_id, s.season
            FROM trivia_player_seasons s
            JOIN players p ON p.player_id = s.player_id
            WHERE s.position = %s AND p.college ILIKE %s
        """
        return sql, [position, f"%{college}%"], "season"

    if category == "collegematch":
        anchor_id = parts[3]
        sql = """
            SELECT s.fantasy_pts_ppr AS sort_val, s.player_id, s.season
            FROM trivia_player_seasons s
            JOIN players p ON p.player_id = s.player_id
            WHERE s.position = %s
              AND p.college ILIKE (SELECT '%%' || college || '%%' FROM players WHERE player_id = %s)
              AND s.player_id != %s
        """
        return sql, [position, anchor_id, anchor_id], "season"

    if category == "eitheror":
        stat_a, thresh_a = parts[3], int(parts[4])
        stat_b, thresh_b = parts[5], int(parts[6])
        year_lo, year_hi = int(parts[7]), int(parts[8])
        sql = f"""
            SELECT fantasy_pts_ppr AS sort_val, player_id, season
            FROM trivia_player_seasons
            WHERE position = %s AND season BETWEEN %s AND %s
              AND (({stat_a} >= %s) != ({stat_b} >= %s))
        """
        return sql, [position, year_lo, year_hi, thresh_a, thresh_b], "season"

    if category == "teamstat":
        stat = parts[3]
        threshold = int(parts[4])
        team_abbr = parts[5]
        sql = f"""
            SELECT fantasy_pts_ppr AS sort_val, player_id, season
            FROM trivia_player_seasons
            WHERE position = %s AND {stat} >= %s AND team = %s
        """
        return sql, [position, threshold, team_abbr], "season"

    return None, None, None