"""Question generation and board layout helpers for DraftForge."""

import random
from typing import Optional

from rules import rules_for_position
import dynamic_rules

CURRENT_SEASON = 2025
DYNAMIC_QUESTION_CHANCE = 0.35
COLLEGE_QUESTION_CHANCE = 0.20
MIN_COLLEGE_POOL_SIZE = 4

SLOT_ORDER = [
    {"key": "QB", "label": "QB", "position": "QB", "difficulty": "easy"},
    {"key": "RB1", "label": "RB", "position": "RB", "difficulty": "easy"},
    {"key": "RB2", "label": "RB", "position": "RB", "difficulty": "medium"},
    {"key": "WR1", "label": "WR", "position": "WR", "difficulty": "medium"},
    {"key": "WR2", "label": "WR", "position": "WR", "difficulty": "medium"},
    {"key": "TE", "label": "TE", "position": "TE", "difficulty": "hard"},
    {"key": "FLEX", "label": "FLEX", "position": None, "difficulty": "hard"},
]
FLEX_POSITIONS = ["RB", "WR", "TE"]
SUPERFLEX_POSITIONS = ["QB", "RB", "WR", "TE"]
DIFFICULTY_LEVELS = ["easy", "medium", "hard"]
VALID_SCOPES = {"mixed", "season", "career"}
BOARD_TYPES = {"standard", "superflex"}


def slot_display_label(slot: dict, board_type: str = "standard") -> str:
    return "SFLX" if board_type == "superflex" else slot["label"]


def build_name_train_turn(slot_key: str, board_type: str = "standard") -> dict:
    """Builds a no-trivia turn payload for Name Train mode."""
    slot = next((item for item in SLOT_ORDER if item["key"] == slot_key), None)
    if not slot:
        raise ValueError(f"Unknown slot: {slot_key}")
    return {
        "slot_key": slot_key,
        "slot_label": slot_display_label(slot, board_type),
        "rule_id": "name_train",
        "category": "NAME_TRAIN",
        "scope": "none",
        "difficulty": "name_train",
        "position": None,
        "title": "Name Train",
        "description": "Draft an unused player. Each successful player after the first must start with the final letter of the previous successful player.",
        "era_min_season": None,
        "era_max_season": None,
        "era_is_gate": False,
    }


def try_generate_college_question(cur, position: str, difficulty: str):
    cur.execute(
        """
        SELECT p.college, COUNT(*) AS cnt
        FROM trivia_player_seasons AS s
        JOIN players AS p ON p.player_id = s.player_id
        WHERE s.position = %s
          AND p.college IS NOT NULL
          AND p.college <> ''
        GROUP BY p.college
        HAVING COUNT(*) >= %s
        ORDER BY RANDOM()
        LIMIT 1
        """,
        (position, MIN_COLLEGE_POOL_SIZE),
    )
    row = cur.fetchone()
    if not row:
        return None
    college = row["college"]
    return {
        "rule_id": f"college|{college}",
        "category": "COLLEGE_MATCH",
        "scope": "season",
        "difficulty": difficulty,
        "position": position,
        "title": f"Draft a {position} who played college football at {college}",
        "description": f"The chosen season must belong to a player whose college is {college}.",
    }


def build_question_for_slot(
    cur,
    slot_key: str,
    difficulty_override: Optional[str] = None,
    scope_mode: str = "mixed",
    prior_picks: Optional[dict] = None,
    allow_college: bool = True,
    board_type: str = "standard",
    name_train: bool = False,
):
    """Build one normal trivia question, or a no-trivia Name Train turn."""
    slot = next((item for item in SLOT_ORDER if item["key"] == slot_key), None)
    if not slot:
        raise ValueError(f"Unknown slot: {slot_key}")

    if board_type not in BOARD_TYPES:
        board_type = "standard"
    if name_train:
        return build_name_train_turn(slot_key, board_type)

    position = random.choice(SUPERFLEX_POSITIONS) if board_type == "superflex" else (slot["position"] or random.choice(FLEX_POSITIONS))
    difficulty = difficulty_override or slot["difficulty"]
    if difficulty not in DIFFICULTY_LEVELS:
        raise ValueError(f"Invalid difficulty: {difficulty}")
    if scope_mode not in VALID_SCOPES:
        scope_mode = "mixed"

    if scope_mode != "career" and allow_college and random.random() < COLLEGE_QUESTION_CHANCE:
        college_question = try_generate_college_question(cur, position, difficulty)
        if college_question:
            college_question.update({
                "slot_key": slot_key,
                "slot_label": slot_display_label(slot, board_type),
                "era_min_season": None,
                "era_max_season": None,
                "era_is_gate": False,
            })
            return college_question

    if scope_mode != "career" and random.random() < DYNAMIC_QUESTION_CHANCE:
        dynamic_question = dynamic_rules.try_generate_dynamic_question(cur, position, difficulty, prior_picks or {})
        if dynamic_question:
            dynamic_question.update({
                "slot_key": slot_key,
                "slot_label": slot_display_label(slot, board_type),
                "era_min_season": None,
                "era_max_season": None,
                "era_is_gate": True,
            })
            return dynamic_question

    candidates = rules_for_position(position, difficulty, scope_mode)
    if not candidates:
        candidates = rules_for_position(position, None, scope_mode)
    if not candidates:
        candidates = rules_for_position(position)
    if not candidates:
        raise ValueError(f"No rules available for position {position}")

    rule = random.choice(candidates)
    era_min_season = None
    era_max_season = None
    if rule["category"] == "ERA":
        params = rule["params"]
        if params["op"] == "between":
            era_min_season = params["low"]
            era_max_season = params["high"]
        else:
            era_min_season = CURRENT_SEASON - params["years_back"]

    return {
        "slot_key": slot_key,
        "slot_label": slot_display_label(slot, board_type),
        "rule_id": rule["id"],
        "category": rule["category"],
        "scope": rule["scope"],
        "difficulty": rule["difficulty"],
        "position": position,
        "title": rule["title_tpl"].format(position=position),
        "description": rule["desc_tpl"],
        "era_min_season": era_min_season,
        "era_max_season": era_max_season,
        "era_is_gate": rule["category"] == "ERA",
    }
