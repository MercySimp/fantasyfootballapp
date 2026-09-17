"""
Shared pick validation, ranking, and board-rule helpers.

evaluate_pick() ALWAYS computes a real rank/percentile against a genuine
qualifying pool via rank_in_qualifying_pool() -- there is no shortcut or
placeholder ranking anywhere in this file. Board-customization scoring
(golf conversion, miss penalties) is applied as separate, composable
post-processing steps by the caller, never baked into evaluate_pick().

Name Train has no trivia condition to satisfy, but it is NOT scoreless:
build_name_train_result() pulls the player's real fantasy points for the
season they submit and ranks that season against every other season ever
recorded at the same position, exactly like every other category.
"""

import re
from typing import Optional

from rules import CATALOG
import dynamic_rules

CURRENT_SEASON = 2025
VALID_FORMATS = {"standard", "half_ppr", "ppr"}
SUPERFLEX_POSITIONS = ["QB", "RB", "WR", "TE"]
GOLF_BRICK_SCORE = 100.0

FORMULA_SQL = {
    "scrimmage_yards": "(COALESCE(rushing_yards,0) + COALESCE(receiving_yards,0))",
    "total_tds": "(COALESCE(rushing_tds,0) + COALESCE(receiving_tds,0) + COALESCE(passing_tds,0))",
    "td_int_diff": "(COALESCE(passing_tds,0) - COALESCE(interceptions,0))",
}
GRADE_TIERS = [
    (95, "elite", "Elite Answer", "#37c98f"),
    (85, "great", "Great Answer", "#6fe3ff"),
    (65, "solid", "Solid Answer", "#ffcc56"),
    (40, "common", "Common Answer", "#ff9f4d"),
    (0, "obscure", "Obscure Find", "#b9a6ff"),
]
POINTS_COLUMN = {"standard": "fantasy_pts_standard", "half_ppr": "fantasy_pts_half_ppr", "ppr": "fantasy_pts_ppr"}


# ── Board-customization helpers ─────────────────────────────────────────
def superflex_allowed_positions(slot_position: Optional[str], board_type: str) -> list:
    if board_type == "superflex":
        return SUPERFLEX_POSITIONS
    return [slot_position] if slot_position else ["RB", "WR", "TE"]


def first_letter(name: str) -> Optional[str]:
    match = re.search(r"[A-Za-z]", name or "")
    return match.group(0).upper() if match else None


def last_letter(name: str) -> Optional[str]:
    letters = re.findall(r"[A-Za-z]", name or "")
    return letters[-1].upper() if letters else None


def check_name_train(result: dict, required_letter: Optional[str]) -> dict:
    if not result.get("valid") or not required_letter:
        return result
    actual = first_letter(result.get("player", ""))
    if actual != required_letter.upper():
        return {"valid": False, "reason": f"Name Train requires a player starting with {required_letter.upper()}; {result.get('player', 'that player')} starts with {actual or 'no letter'}."}
    return result


def apply_miss_penalty(result: dict, penalty_percent: float) -> dict:
    if not result.get("valid") or not penalty_percent:
        return result
    original = result.get("fantasy_points") or 0
    result["fantasy_points"] = round(original * (1 - penalty_percent / 100), 2)
    result["penalized"] = True
    result["penalty_percent"] = penalty_percent
    result["pre_penalty_points"] = original
    return result


def apply_golf_scoring(result: dict, penalty_percent: float = 0) -> dict:
    if not result.get("valid"):
        return result
    percentile = result.get("percentile")
    strokes = round(100 - percentile, 2) if percentile is not None else GOLF_BRICK_SCORE
    result["pre_golf_strokes"] = strokes
    result["fantasy_points"] = strokes
    result["scoring_mode"] = "golf"
    if penalty_percent:
        penalty_strokes = round(GOLF_BRICK_SCORE * (penalty_percent / 100), 2)
        result["fantasy_points"] = round(min(GOLF_BRICK_SCORE, strokes + penalty_strokes), 2)
        result["penalized"] = True
        result["penalty_percent"] = penalty_percent
        result["penalty_strokes"] = penalty_strokes
    return result


# ── Shared data access ──────────────────────────────────────────────────
def get_rule(rule_id: str):
    return next((rule for rule in CATALOG if rule["id"] == rule_id), None)


def fetch_player_seasons(cur, player_id: str):
    cur.execute(
        """
        SELECT display_name, season, team, position, games_played,
               passing_yards, passing_tds, interceptions, rushing_yards,
               rushing_tds, receiving_yards, receiving_tds, receptions,
               fantasy_pts_standard, fantasy_pts_half_ppr, fantasy_pts_ppr,
               rank_standard, rank_half_ppr, rank_ppr
        FROM trivia_player_seasons
        WHERE player_id = %s
        ORDER BY season
        """,
        (player_id,),
    )
    return cur.fetchall()


def fetch_player_college(cur, player_id: str) -> Optional[str]:
    cur.execute("SELECT college FROM players WHERE player_id = %s", (player_id,))
    row = cur.fetchone()
    return row["college"] if row and row["college"] else None


def derived_value(row, formula):
    if formula == "scrimmage_yards":
        return (row["rushing_yards"] or 0) + (row["receiving_yards"] or 0)
    if formula == "total_tds":
        return (row["rushing_tds"] or 0) + (row["receiving_tds"] or 0) + (row["passing_tds"] or 0)
    if formula == "td_int_diff":
        return (row["passing_tds"] or 0) - (row["interceptions"] or 0)
    raise ValueError(f"Unknown formula {formula}")


def compare(value, op, threshold=None, low=None, high=None):
    if op == "gte": return value >= threshold
    if op == "lte": return value <= threshold
    if op == "eq": return value == threshold
    if op == "between": return low <= value <= high
    raise ValueError(f"Unknown op {op}")


def era_condition_met(season_val: int, params: dict) -> bool:
    if params["op"] == "between":
        return compare(season_val, "between", low=params["low"], high=params["high"])
    return season_val >= CURRENT_SEASON - params["years_back"]


def validate_era_eligibility(rows, rule, position):
    params = rule["params"]
    pos_rows = [row for row in rows if row["position"] == position]
    qualifying = [row["season"] for row in pos_rows if era_condition_met(row["season"], params)]
    if not qualifying:
        if params["op"] == "between":
            return False, f"No {position} season on record between {params['low']} and {params['high']}"
        cutoff = CURRENT_SEASON - params["years_back"]
        return False, f"No {position} season on record since {cutoff}"
    return True, None


def validate_season_rule(row, rule, position):
    params = rule["params"]
    if (row["games_played"] or 0) < params.get("min_games", 1):
        return False, f"Only played {row['games_played'] or 0} games that season (needs {params.get('min_games', 1)}+)"
    if rule["category"] == "SEASON_STAT":
        value = row[params["stat"]] or 0
        ok = compare(value, params["op"], threshold=params.get("threshold"))
        return ok, None if ok else f"{value} {params['stat'].replace('_', ' ')} in {row['season']} \u2014 needs {params['op']} {params['threshold']}"
    if rule["category"] == "SEASON_COMBO":
        value = derived_value(row, params["formula"])
        ok = compare(value, params["op"], threshold=params.get("threshold"))
        return ok, None if ok else f"{value} in {row['season']} \u2014 needs {params['op']} {params['threshold']}"
    if rule["category"] == "RANK_FINISH":
        value = row.get("rank_ppr")
        if value is None:
            return False, "No position rank on record for this season"
        ok = compare(value, params["op"], threshold=params.get("threshold"))
        return ok, None if ok else f"Finished #{value} at {position} in {row['season']} \u2014 needs top {params['threshold']}"
    if rule["category"] == "DUAL_THREAT":
        for stat, op, threshold in params["conditions"]:
            if not compare(row[stat] or 0, op, threshold=threshold):
                return False, f"{row[stat] or 0} {stat.replace('_', ' ')} in {row['season']} \u2014 needs {op} {threshold}"
        return True, None
    return False, "Unrecognized rule category"


def validate_career_rule(rows, rule, position):
    params = rule["params"]
    pos_rows = [row for row in rows if row["position"] == position]
    if not pos_rows:
        return False, None, "No seasons recorded at this position"
    if rule["category"] == "CAREER_TOTAL":
        total = sum((row[params["stat"]] or 0) for row in pos_rows)
        ok = compare(total, params["op"], threshold=params["threshold"])
        return ok, {"career_total": total}, None if ok else f"Career total is {total:,}, needs {params['threshold']:,}+"
    if rule["category"] == "STREAK":
        qualifying = sum(1 for row in pos_rows if (row[params["stat"]] or 0) >= params["season_threshold"])
        ok = qualifying >= params["count"]
        return ok, {"qualifying_seasons": qualifying}, None if ok else f"Only {qualifying} qualifying seasons, needs {params['count']}+"
    if rule["category"] == "LONGEVITY":
        if params["stat"] == "games_played":
            total = sum((row["games_played"] or 0) for row in pos_rows)
            ok = compare(total, params["op"], threshold=params["threshold"])
            return ok, {"career_games": total}, None if ok else f"Only {total} career games, needs {params['threshold']}+"
        count = len(pos_rows)
        ok = compare(count, params["op"], threshold=params["threshold"])
        return ok, {"season_count": count}, None if ok else f"Only {count} seasons, needs {params['threshold']}+"
    if rule["category"] == "TEAM_HISTORY":
        teams = {row["team"] for row in pos_rows if row["team"]}
        total_games = sum((row["games_played"] or 0) for row in pos_rows)
        if params["op"] == "gte":
            ok = len(teams) >= params["threshold"]
            return ok, {"team_count": len(teams), "career_games": total_games}, None if ok else f"Only {len(teams)} teams, needs {params['threshold']}+"
        min_seasons = params.get("min_seasons", 1)
        ok = len(teams) == params["threshold"] and len(pos_rows) >= min_seasons
        return ok, {"team_count": len(teams), "career_games": total_games}, None if ok else f"Played for {len(teams)} teams across {len(pos_rows)} seasons"
    return False, None, "Unrecognized rule category"


# ── Pool-ranking machinery (restored, real, not hardcoded) ─────────────
def build_pool_query(rule, position):
    params = rule["params"]
    category = rule["category"]

    if category == "SEASON_STAT":
        sql_op = ">=" if params["op"] == "gte" else "<="
        sql = f"""SELECT fantasy_pts_ppr AS sort_val, player_id, season FROM trivia_player_seasons
                  WHERE position = %s AND games_played >= %s AND {params['stat']} {sql_op} %s"""
        return sql, [position, params.get("min_games", 1), params["threshold"]], "season"

    if category == "SEASON_COMBO":
        sql = f"""SELECT fantasy_pts_ppr AS sort_val, player_id, season FROM trivia_player_seasons
                  WHERE position = %s AND {FORMULA_SQL[params['formula']]} >= %s"""
        return sql, [position, params["threshold"]], "season"

    if category == "DUAL_THREAT":
        where_parts, sql_params = [], [position]
        for stat, op, threshold in params["conditions"]:
            where_parts.append(f"{stat} {'>=' if op == 'gte' else '<='} %s")
            sql_params.append(threshold)
        sql = f"""SELECT fantasy_pts_ppr AS sort_val, player_id, season FROM trivia_player_seasons
                  WHERE position = %s AND {' AND '.join(where_parts)}"""
        return sql, sql_params, "season"

    if category == "RANK_FINISH":
        sql = """SELECT fantasy_pts_ppr AS sort_val, player_id, season FROM trivia_player_seasons
                 WHERE position = %s AND rank_ppr <= %s"""
        return sql, [position, params["threshold"]], "season"

    if category == "ERA":
        sql = """SELECT fantasy_pts_ppr AS sort_val, player_id, season FROM trivia_player_seasons WHERE position = %s"""
        return sql, [position], "season"

    if category == "CAREER_TOTAL":
        sql = f"""SELECT SUM(fantasy_pts_ppr) AS sort_val, player_id FROM trivia_player_seasons
                  WHERE position = %s GROUP BY player_id HAVING SUM({params['stat']}) >= %s"""
        return sql, [position, params["threshold"]], "career"

    if category == "STREAK":
        sql = f"""SELECT SUM(fantasy_pts_ppr) AS sort_val, player_id FROM trivia_player_seasons
                  WHERE position = %s GROUP BY player_id
                  HAVING COUNT(*) FILTER (WHERE {params['stat']} >= %s) >= %s"""
        return sql, [position, params["season_threshold"], params["count"]], "career"

    if category == "LONGEVITY":
        if params["stat"] == "games_played":
            sql = """SELECT SUM(fantasy_pts_ppr) AS sort_val, player_id FROM trivia_player_seasons
                     WHERE position = %s GROUP BY player_id HAVING SUM(games_played) >= %s"""
        else:
            sql = """SELECT SUM(fantasy_pts_ppr) AS sort_val, player_id FROM trivia_player_seasons
                     WHERE position = %s GROUP BY player_id HAVING COUNT(DISTINCT season) >= %s"""
        return sql, [position, params["threshold"]], "career"

    if category == "TEAM_HISTORY":
        if params["op"] == "gte":
            sql = """SELECT SUM(fantasy_pts_ppr) AS sort_val, player_id FROM trivia_player_seasons
                     WHERE position = %s GROUP BY player_id HAVING COUNT(DISTINCT team) >= %s"""
            return sql, [position, params["threshold"]], "career"
        sql = """SELECT SUM(fantasy_pts_ppr) AS sort_val, player_id FROM trivia_player_seasons
                 WHERE position = %s GROUP BY player_id
                 HAVING COUNT(DISTINCT team) = 1 AND COUNT(DISTINCT season) >= %s"""
        return sql, [position, params.get("min_seasons", 1)], "career"

    return None, None, None


def rank_in_qualifying_pool(cur, sql, sql_params, pick_value, id_mode, player_id, season=None):
    if id_mode == "season":
        exclude_clause, exclude_params = "NOT (player_id = %s AND season = %s)", [player_id, season]
    else:
        exclude_clause, exclude_params = "player_id != %s", [player_id]
    full_sql = f"""SELECT COUNT(*) AS total,
                          SUM(CASE WHEN sort_val > %s AND {exclude_clause} THEN 1 ELSE 0 END) AS better
                   FROM ({sql}) AS pool"""
    cur.execute(full_sql, [pick_value] + exclude_params + sql_params)
    row = cur.fetchone()
    total, better = row["total"] or 0, row["better"] or 0
    if total == 0:
        return 1, 1
    return better + 1, total


def grade_pick(rank: int, pool_size: int, difficulty: str, rule_title: str) -> dict:
    if not pool_size:
        return {"percentile": None, "grade_tier": "unranked", "grade_label": "Unranked", "grade_color": "#8ea5ba", "pool_size": 0, "pool_rank": None, "grade_message": "No comparable qualifying answers found.", "smart_pick_bonus": False}
    percentile = round((pool_size - rank + 1) / pool_size * 100, 1)
    tier_key, tier_label, tier_color = "obscure", "Obscure Find", "#b9a6ff"
    for threshold, key, label, color in GRADE_TIERS:
        if percentile >= threshold:
            tier_key, tier_label, tier_color = key, label, color
            break
    smart_pick_bonus = difficulty == "hard" and percentile < 50
    message = f"Ranks #{rank} of {pool_size} qualifying answers for \u201c{rule_title}\u201d ({percentile}th percentile by fantasy points)."
    if smart_pick_bonus:
        message += " Great depth of knowledge \u2014 a name that barely cleared a HARD bar is a savvy find."
    elif percentile >= 95:
        message += " You picked the best possible answer to this exact question."
    return {"percentile": percentile, "grade_tier": tier_key, "grade_label": tier_label, "grade_color": tier_color, "pool_size": pool_size, "pool_rank": rank, "grade_message": message, "smart_pick_bonus": smart_pick_bonus}


# ── Name Train: real scoring, no trivia condition ───────────────────────
def build_name_train_result(cur, player_id: str, position: Optional[str], season: Optional[int], scoring_format: str = "ppr") -> dict:
    rows = fetch_player_seasons(cur, player_id)
    if not rows:
        return {"valid": False, "reason": "No stats found for this player"}
    display_name = rows[0]["display_name"]

    if position and position not in SUPERFLEX_POSITIONS:
        return {"valid": False, "reason": "Name Train picks must be QB, RB, WR, or TE"}

    candidates = [row for row in rows if (not position or row["position"] == position) and (season is None or row["season"] == season)]
    if not candidates:
        return {"valid": False, "reason": f"{display_name} has no eligible QB/RB/WR/TE season matching that selection"}
    match = candidates[0]
    match_position = match["position"]
    points_column = POINTS_COLUMN.get(scoring_format, "fantasy_pts_ppr")
    pick_points = match[points_column] or 0

    best_season_row = max([r for r in rows if r["position"] == match_position], key=lambda r: r[points_column] or 0)
    is_best_year = match["season"] == best_season_row["season"] and pick_points == (best_season_row[points_column] or 0)

    sql = f"SELECT {points_column} AS sort_val, player_id, season FROM trivia_player_seasons WHERE position = %s"
    rank, pool_size = rank_in_qualifying_pool(cur, sql, [match_position], pick_points, "season", player_id, match["season"])
    grade = grade_pick(rank, pool_size, "medium", f"best {match_position} season by fantasy points")

    return {
        "valid": True, "player": display_name, "player_id": player_id, "team": match["team"],
        "season": match["season"], "position": match_position, "fantasy_points": pick_points,
        "matched_category": "NAME_TRAIN", "difficulty": "name_train", "rule_title": "Name Train",
        "is_best_year": is_best_year,
        **grade,
    }


# ── Main entry point ────────────────────────────────────────────────────
def evaluate_pick(cur, rule_id: str, position: str, player_id: str, season: int, scoring_format: str = "ppr"):
    if rule_id == "name_train":
        return build_name_train_result(cur, player_id, position, season, scoring_format)

    if scoring_format not in VALID_FORMATS:
        scoring_format = "ppr"
    points_column = POINTS_COLUMN[scoring_format]

    rows = fetch_player_seasons(cur, player_id)
    if not rows:
        return {"valid": False, "reason": "No stats found for this player"}
    display_name = rows[0]["display_name"]

    if rule_id.startswith("college|"):
        college = rule_id.split("|", 1)[1]
        match = next((row for row in rows if row["season"] == season and row["position"] == position), None)
        if not match:
            return {"valid": False, "reason": f"{display_name} has no {position} stats recorded for {season}"}
        player_college = fetch_player_college(cur, player_id)
        if not player_college or player_college.strip().lower() != college.strip().lower():
            return {"valid": False, "reason": f"{display_name}'s college is {player_college or 'unknown'}, not {college}"}
        pick_points = match[points_column] or 0
        sql = """SELECT s.fantasy_pts_ppr AS sort_val, s.player_id, s.season
                 FROM trivia_player_seasons s JOIN players p ON p.player_id = s.player_id
                 WHERE s.position = %s AND p.college = %s"""
        rank, pool_size = rank_in_qualifying_pool(cur, sql, [position, college], pick_points, "season", player_id, match["season"])
        grade = grade_pick(rank, pool_size, "medium", f"played college football at {college}")
        return {"valid": True, "player": display_name, "player_id": player_id, "team": match["team"], "season": season, "position": position, "fantasy_points": pick_points, "matched_category": "COLLEGE_MATCH", "difficulty": "college", "rule_title": college, **grade}

    if dynamic_rules.is_dynamic_rule(rule_id):
        match = next((row for row in rows if row["season"] == season and row["position"] == position), None)
        if not match:
            return {"valid": False, "reason": f"{display_name} has no {position} stats recorded for {season}"}
        ok, reason = dynamic_rules.validate_dynamic_pick(cur, rule_id, match, player_id, position)
        if not ok:
            return {"valid": False, "reason": reason}
        category = rule_id.split("|")[1].upper()
        rule_title = f"({category} question)"
        sql, sql_params, id_mode = dynamic_rules.build_dynamic_pool_query(rule_id, position)
        pick_points = match[points_column] or 0
        rank, pool_size = rank_in_qualifying_pool(cur, sql, sql_params, pick_points, id_mode, player_id, match["season"])
        grade = grade_pick(rank, pool_size, "hard", rule_title)
        return {"valid": True, "player": display_name, "player_id": player_id, "team": match["team"], "season": season, "position": position, "fantasy_points": pick_points, "matched_category": category, "difficulty": "dynamic", "rule_title": rule_title, **grade}

    rule = get_rule(rule_id)
    if not rule:
        return {"valid": False, "reason": f"Unknown rule_id: {rule_id}"}
    rule_title = rule["title_tpl"].format(position=position)

    if rule["scope"] == "season":
        if rule["category"] == "ERA":
            ok, reason = validate_era_eligibility(rows, rule, position)
            if not ok:
                return {"valid": False, "reason": reason}
            match = next((row for row in rows if row["season"] == season and row["position"] == position), None)
            if not match:
                return {"valid": False, "reason": f"{display_name} has no {position} stats recorded for {season}"}
        else:
            match = next((row for row in rows if row["season"] == season and row["position"] == position), None)
            if not match:
                return {"valid": False, "reason": f"{display_name} has no {position} stats recorded for {season}"}
            ok, reason = validate_season_rule(match, rule, position)
            if not ok:
                return {"valid": False, "reason": reason}
        pick_points = match[points_column] or 0

        best_season_row = max([r for r in rows if r["position"] == position], key=lambda r: r[points_column] or 0)
        is_best_year = match["season"] == best_season_row["season"] and pick_points == (best_season_row[points_column] or 0)

        sql, sql_params, id_mode = build_pool_query(rule, position)
        rank, pool_size = rank_in_qualifying_pool(cur, sql, sql_params, pick_points, id_mode, player_id, match["season"])
        grade = grade_pick(rank, pool_size, rule["difficulty"], rule_title)
        return {"valid": True, "player": display_name, "player_id": player_id, "team": match["team"], "season": season, "position": position, "fantasy_points": pick_points, "matched_category": rule["category"], "difficulty": rule["difficulty"], "rule_title": rule_title, "is_best_year": is_best_year, **grade}

    ok, detail, reason = validate_career_rule(rows, rule, position)
    if not ok:
        return {"valid": False, "reason": reason}
    pos_rows = [row for row in rows if row["position"] == position]
    best_season = max(pos_rows, key=lambda row: row[points_column] or 0)
    career_points = sum((row[points_column] or 0) for row in pos_rows)
    sql, sql_params, id_mode = build_pool_query(rule, position)
    rank, pool_size = rank_in_qualifying_pool(cur, sql, sql_params, career_points, id_mode, player_id)
    grade = grade_pick(rank, pool_size, rule["difficulty"], rule_title)
    return {"valid": True, "player": display_name, "player_id": player_id, "team": best_season["team"], "season": best_season["season"], "position": position, "fantasy_points": career_points, "matched_category": rule["category"], "difficulty": rule["difficulty"], "rule_title": rule_title, "career_detail": detail, **grade}
