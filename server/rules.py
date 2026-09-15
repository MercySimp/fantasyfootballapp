"""
Trivia rule catalog for the fantasy football draft app.
100 unique rules across 9 categories, tagged with difficulty (easy/medium/hard)
and scope (season vs career).

Note on ERA rules: these are ELIGIBILITY gates, not scoring constraints.
"Draft a QB with a season in the last 5 years" means the player must have
AT LEAST ONE qualifying season in that window -- the season you actually
submit for scoring can be any season on their record, including one outside
the era window (e.g. their best career season).
"""

POSITIONS = ["QB", "RB", "WR", "TE"]
CATALOG = []


def _add(rule):
    CATALOG.append(rule)


STAT_LABELS = {
    "passing_yards": "passing yards", "passing_tds": "passing touchdowns",
    "rushing_yards": "rushing yards", "rushing_tds": "rushing touchdowns",
    "receiving_yards": "receiving yards", "receiving_tds": "receiving touchdowns",
    "receptions": "receptions", "interceptions": "interceptions",
}

SEASON_STAT_DEFS = {
    "QB": [
        ("passing_yards", [3000, 4000, 5000]),
        ("passing_tds", [20, 30, 40]),
        ("rushing_yards", [300, 500, 750]),
        ("interceptions", [15, 10, 5]),
    ],
    "RB": [
        ("rushing_yards", [800, 1200, 1600]),
        ("rushing_tds", [8, 12, 16]),
        ("receptions", [30, 50, 70]),
        ("receiving_yards", [300, 500, 700]),
    ],
    "WR": [
        ("receiving_yards", [800, 1100, 1400]),
        ("receptions", [60, 80, 100]),
        ("receiving_tds", [6, 9, 12]),
    ],
    "TE": [
        ("receiving_yards", [500, 800, 1000]),
        ("receptions", [40, 60, 80]),
        ("receiving_tds", [5, 8, 10]),
    ],
}
DIFF_TIER = ["easy", "medium", "hard"]

for pos, defs in SEASON_STAT_DEFS.items():
    for stat, thresholds in defs:
        label = STAT_LABELS[stat]
        for i, t in enumerate(thresholds):
            diff = DIFF_TIER[i]
            if stat == "interceptions":
                rid = f"season_{pos.lower()}_{stat}_max_{t}"
                _add({
                    "id": rid, "category": "SEASON_STAT", "positions": [pos], "scope": "season",
                    "difficulty": diff,
                    "title_tpl": f"Draft a {{position}} with {t} or fewer interceptions in a season",
                    "desc_tpl": f"Chosen season's interceptions must be {t} or fewer (min 8 games played).",
                    "params": {"stat": stat, "op": "lte", "threshold": t, "min_games": 8},
                })
            else:
                rid = f"season_{pos.lower()}_{stat}_{t}"
                _add({
                    "id": rid, "category": "SEASON_STAT", "positions": [pos], "scope": "season",
                    "difficulty": diff,
                    "title_tpl": f"Draft a {{position}} with {t}+ {label} in a season",
                    "desc_tpl": f"Chosen season's {label} must be {t} or more.",
                    "params": {"stat": stat, "op": "gte", "threshold": t},
                })

for pos, thresholds in [("RB", [1200, 1800]), ("WR", [1000, 1400]), ("TE", [700, 1000])]:
    for i, t in enumerate(thresholds):
        diff = "easy" if i == 0 else "hard"
        _add({
            "id": f"combo_{pos.lower()}_scrimmage_{t}", "category": "SEASON_COMBO",
            "positions": [pos], "scope": "season", "difficulty": diff,
            "title_tpl": f"Draft a {{position}} with {t}+ yards from scrimmage in a season",
            "desc_tpl": f"Rushing yards + receiving yards must total {t} or more in the chosen season.",
            "params": {"formula": "scrimmage_yards", "op": "gte", "threshold": t},
        })

for pos, thresholds in [("QB", [25, 40]), ("RB", [10, 15]), ("WR", [8, 12]), ("TE", [8, 10])]:
    for i, t in enumerate(thresholds):
        diff = "medium" if i == 0 else "hard"
        _add({
            "id": f"combo_{pos.lower()}_total_tds_{t}", "category": "SEASON_COMBO",
            "positions": [pos], "scope": "season", "difficulty": diff,
            "title_tpl": f"Draft a {{position}} with {t}+ total touchdowns in a season",
            "desc_tpl": f"Rushing + receiving + passing touchdowns must total {t} or more in the chosen season.",
            "params": {"formula": "total_tds", "op": "gte", "threshold": t},
        })

for t, diff in [(10, "medium"), (15, "hard")]:
    _add({
        "id": f"combo_qb_td_int_diff_{t}", "category": "SEASON_COMBO",
        "positions": ["QB"], "scope": "season", "difficulty": diff,
        "title_tpl": f"Draft a QB who threw {t}+ more TDs than interceptions in a season",
        "desc_tpl": f"Passing touchdowns minus interceptions must be {t} or greater in the chosen season.",
        "params": {"formula": "td_int_diff", "op": "gte", "threshold": t},
    })

CAREER_DEFS = {
    "QB": [("passing_yards", [20000, 35000]), ("passing_tds", [150, 250])],
    "RB": [("rushing_yards", [5000, 9000]), ("rushing_tds", [40, 70]), ("receptions", [200, 400])],
    "WR": [("receiving_yards", [6000, 10000]), ("receptions", [400, 700]), ("receiving_tds", [40, 70])],
    "TE": [("receiving_yards", [4000, 7000]), ("receptions", [300, 500])],
}
for pos, defs in CAREER_DEFS.items():
    for stat, thresholds in defs:
        label = STAT_LABELS[stat]
        for i, t in enumerate(thresholds):
            diff = "medium" if i == 0 else "hard"
            _add({
                "id": f"career_{pos.lower()}_{stat}_{t}", "category": "CAREER_TOTAL",
                "positions": [pos], "scope": "career", "difficulty": diff,
                "title_tpl": f"Draft a {{position}} with {t:,}+ career {label}",
                "desc_tpl": f"Sum of {label} across every regular season on record must be {t:,} or more.",
                "params": {"stat": stat, "op": "gte", "threshold": t},
            })

STREAK_DEFS = {"RB": ("rushing_yards", 1000), "WR": ("receiving_yards", 1000), "TE": ("receiving_yards", 700)}
for pos, (stat, per_season_threshold) in STREAK_DEFS.items():
    for count, diff in [(2, "medium"), (3, "hard")]:
        label = STAT_LABELS[stat]
        _add({
            "id": f"streak_{pos.lower()}_{stat}_{count}", "category": "STREAK",
            "positions": [pos], "scope": "career", "difficulty": diff,
            "title_tpl": f"Draft a {{position}} with {count}+ seasons of {per_season_threshold}+ {label}",
            "desc_tpl": f"Player needs at least {count} separate seasons with {per_season_threshold}+ {label}.",
            "params": {"stat": stat, "season_threshold": per_season_threshold, "count": count},
        })

_add({"id": "longevity_games_100", "category": "LONGEVITY", "positions": ["ANY"], "scope": "career",
      "difficulty": "hard", "title_tpl": "Draft a {position} who played 100+ career regular season games",
      "desc_tpl": "Sum of games played across all seasons must be 100 or more.",
      "params": {"stat": "games_played", "op": "gte", "threshold": 100}})
_add({"id": "longevity_seasons_10", "category": "LONGEVITY", "positions": ["ANY"], "scope": "career",
      "difficulty": "hard", "title_tpl": "Draft a {position} who played 10+ seasons",
      "desc_tpl": "Player must have at least 10 distinct regular seasons on record.",
      "params": {"stat": "season_count", "op": "gte", "threshold": 10}})
_add({"id": "longevity_seasons_5", "category": "LONGEVITY", "positions": ["ANY"], "scope": "career",
      "difficulty": "medium", "title_tpl": "Draft a {position} who played 5+ seasons",
      "desc_tpl": "Player must have at least 5 distinct regular seasons on record.",
      "params": {"stat": "season_count", "op": "gte", "threshold": 5}})

for n, diff in [(10, "easy"), (5, "medium"), (3, "hard")]:
    _add({
        "id": f"rank_finish_top{n}", "category": "RANK_FINISH", "positions": ["ANY"], "scope": "season",
        "difficulty": diff,
        "title_tpl": f"Draft a {{position}} who finished top {n} at their position in a season",
        "desc_tpl": f"Chosen season's position rank (PPR) must be {n} or better.",
        "params": {"stat": "position_rank", "op": "lte", "threshold": n},
    })

ERA_RANGES = [("2000s", 2000, 2009), ("2010s", 2010, 2019), ("2020s", 2020, 2029)]
for label, lo, hi in ERA_RANGES:
    _add({
        "id": f"era_{label}", "category": "ERA", "positions": ["ANY"], "scope": "season",
        "difficulty": "easy",
        "title_tpl": f"Draft a {{position}} who had a season in the {label}",
        "desc_tpl": f"Player must have at least one season between {lo} and {hi}. You may submit any of their seasons for scoring.",
        "params": {"stat": "season", "op": "between", "low": lo, "high": hi},
    })
_add({"id": "era_last_5_years", "category": "ERA", "positions": ["ANY"], "scope": "season",
      "difficulty": "medium", "title_tpl": "Draft a {position} with a season in the last 5 years",
      "desc_tpl": "Player must have at least one season within the last 5 completed seasons. You may submit any of their seasons for scoring.",
      "params": {"stat": "season", "op": "recent", "years_back": 5}})

_add({"id": "team_history_3plus", "category": "TEAM_HISTORY", "positions": ["ANY"], "scope": "career",
      "difficulty": "medium", "title_tpl": "Draft a {position} who played for 3+ different teams",
      "desc_tpl": "Player must have recorded stats for 3 or more distinct teams across their career.",
      "params": {"stat": "team_count", "op": "gte", "threshold": 3}})
_add({"id": "team_history_one_team", "category": "TEAM_HISTORY", "positions": ["ANY"], "scope": "career",
      "difficulty": "hard", "title_tpl": "Draft a {position} who played their entire career for one team",
      "desc_tpl": "Player must have recorded stats for exactly 1 team across their entire career (3+ seasons).",
      "params": {"stat": "team_count", "op": "eq", "threshold": 1, "min_seasons": 3}})

_add({"id": "dual_qb_4000_30", "category": "DUAL_THREAT", "positions": ["QB"], "scope": "season",
      "difficulty": "hard", "title_tpl": "Draft a QB with 4,000+ passing yards AND 30+ passing TDs in a season",
      "desc_tpl": "Both thresholds must be met in the same chosen season.",
      "params": {"conditions": [("passing_yards", "gte", 4000), ("passing_tds", "gte", 30)]}})
_add({"id": "dual_rb_1000_50rec", "category": "DUAL_THREAT", "positions": ["RB"], "scope": "season",
      "difficulty": "hard", "title_tpl": "Draft a RB with 1,000+ rushing yards AND 50+ receptions in a season",
      "desc_tpl": "Both thresholds must be met in the same chosen season (dual-threat back).",
      "params": {"conditions": [("rushing_yards", "gte", 1000), ("receptions", "gte", 50)]}})
_add({"id": "dual_wr_100rec_1200", "category": "DUAL_THREAT", "positions": ["WR"], "scope": "season",
      "difficulty": "hard", "title_tpl": "Draft a WR with 100+ receptions AND 1,200+ receiving yards in a season",
      "desc_tpl": "Both thresholds must be met in the same chosen season.",
      "params": {"conditions": [("receptions", "gte", 100), ("receiving_yards", "gte", 1200)]}})
_add({"id": "dual_te_70rec_700", "category": "DUAL_THREAT", "positions": ["TE"], "scope": "season",
      "difficulty": "hard", "title_tpl": "Draft a TE with 70+ receptions AND 700+ receiving yards in a season",
      "desc_tpl": "Both thresholds must be met in the same chosen season.",
      "params": {"conditions": [("receptions", "gte", 70), ("receiving_yards", "gte", 700)]}})


def rules_for_position(position, difficulty=None, scope=None):
    pool = [r for r in CATALOG if position in r["positions"] or "ANY" in r["positions"]]
    if difficulty:
        pool = [r for r in pool if r["difficulty"] == difficulty]
    if scope and scope != "mixed":
        pool = [r for r in pool if r["scope"] == scope]
    return pool


if __name__ == "__main__":
    print("Total rules:", len(CATALOG))
