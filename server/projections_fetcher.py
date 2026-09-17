"""Fetch projection CSVs from FootballGuys (anonymous) and normalize them.
Saves a consolidated CSV at data/projections.csv with columns:
player_id,player_name,position,team,projection_standard,projection_half_ppr,projection_ppr,dynasty_value

This is best-effort: when player_id is not available the normalized player_name+team is used as a stable id.
"""

import csv
import io
import os
import re
import time
import urllib.request
from datetime import date
from urllib.parse import urljoin

DEFAULT_DOWNLOAD_ROOT = "https://www.footballguys.com/projections/download"
DEFAULT_YEAR = str(date.today().year)
DEFAULT_WEEK = os.getenv("FOOTBALLGUYS_WEEK", "2")
DEFAULT_VARIANTS = ("weekly", "restofseason")
EXPECTED_COLUMNS = [
    "player_id",
    "player_name",
    "position",
    "team",
    "projection_standard",
    "projection_half_ppr",
    "projection_ppr",
    "dynasty_value",
]


def _normalize_name(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _make_player_id(name: str, team: str) -> str:
    key = (name or "").lower()
    key = re.sub(r"[^a-z0-9]", "", key)
    if team:
        key += f"_{team.lower()}"
    return key


def _download_urls(year=None, week=None, root=DEFAULT_DOWNLOAD_ROOT):
    """Return FootballGuys' direct CSV download URLs.

    The download endpoint returns CSV bytes without a .csv suffix, so scraping
    the projections landing page cannot discover these files reliably.
    """
    year = str(year or os.getenv("FOOTBALLGUYS_YEAR", DEFAULT_YEAR))
    week = str(week or os.getenv("FOOTBALLGUYS_WEEK", DEFAULT_WEEK))
    positions = os.getenv("FOOTBALLGUYS_POSITIONS", "all").split(",")
    return [
        (variant, position.strip().lower(),
         f"{root}/{variant}/{position.strip().lower()}/{year}/{week}")
        for variant in DEFAULT_VARIANTS
        for position in positions
        if position.strip()
    ]


def _read_csv_from_url(url: str):
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            # sniff encoding loosely, fall back to utf-8
            content = resp.read()
            text = content.decode("utf-8", errors="ignore")
            fh = io.StringIO(text)
            reader = csv.DictReader(fh)
            rows = list(reader)
            return rows, None
    except Exception as exc:
        return None, str(exc)


def _map_row_to_standard(row: dict):
    # Try to find name
    name_keys = [k for k in row.keys() if k.lower() in ("player", "player_name", "name", "display_name")]
    team_keys = [k for k in row.keys() if k.lower() in ("team", "team_abbr", "tm")] 
    pos_keys = [k for k in row.keys() if k.lower() in ("pos", "position")]

    # projection candidates
    ppr_keys = [k for k in row.keys() if "ppr" in k.lower() or "fantasy_pts_ppr" in k.lower()]
    half_keys = [k for k in row.keys() if "half" in k.lower() or "half_ppr" in k.lower()]
    std_keys = [k for k in row.keys() if "standard" in k.lower() or "std" == k.lower() or "fantasy_pts_standard" in k.lower()]
    generic_score_keys = [k for k in row.keys() if k.lower() in ("proj", "projection", "fantasy_pts", "fantasy_points", "fpts", "points")]

    name = _normalize_name(row.get(name_keys[0]) if name_keys else row.get(next(iter(row), "")))
    team = (row.get(team_keys[0]) if team_keys else "") if team_keys else ""
    pos = (row.get(pos_keys[0]) if pos_keys else "") if pos_keys else ""

    # find numeric projections
    def _num_from_keys(keys_list):
        for k in keys_list:
            v = row.get(k)
            if v is None:
                continue
            v = str(v).strip()
            if v == "":
                continue
            try:
                return float(v)
            except Exception:
                # remove non-numeric
                num = re.sub(r"[^0-9\.-]", "", v)
                try:
                    return float(num) if num not in ("", ".", "-") else None
                except Exception:
                    continue
        return None

    projection_ppr = _num_from_keys(ppr_keys) or _num_from_keys(generic_score_keys)
    projection_half = _num_from_keys(half_keys) or _num_from_keys(generic_score_keys)
    projection_std = _num_from_keys(std_keys) or _num_from_keys(generic_score_keys)

    # FootballGuys' direct files contain projected stat columns rather than
    # pre-scored fantasy points. Calculate the common scoring variants.
    def stat(*names):
        for key in names:
            value = _num_from_keys([key])
            if value is not None:
                return value
        return 0.0

    passing_yards = stat("pass-yds")
    passing_tds = stat("pass-td")
    interceptions = stat("pass-int")
    rushing_yards = stat("rush-yds")
    rushing_tds = stat("rush-td")
    receiving_yards = stat("rec-yds")
    receiving_tds = stat("rec-td")
    receptions = stat("rec-rec")
    fumbles_lost = stat("fum-lost")
    calculated = (
        passing_yards * 0.04
        + passing_tds * 4
        - interceptions * 2
        + rushing_yards * 0.1
        + rushing_tds * 6
        + receiving_yards * 0.1
        + receiving_tds * 6
        - fumbles_lost * 2
    )
    if projection_std is None:
        projection_std = calculated
    if projection_half is None:
        projection_half = calculated + receptions * 0.5
    if projection_ppr is None:
        projection_ppr = calculated + receptions

    # dynasty value might be present
    dynasty_val = None
    for k in row.keys():
        if "dynast" in k.lower() or "auction" in k.lower() or "value" == k.lower():
            try:
                dynasty_val = float(re.sub(r"[^0-9\.-]", "", str(row.get(k))))
                break
            except Exception:
                dynasty_val = None

    pid = (row.get("player_id") or row.get("id") or "")
    pid = pid.strip() if pid else pid
    if not pid:
        pid = _make_player_id(name, team)

    return {
        "player_id": pid,
        "player_name": name,
        "position": pos,
        "team": team,
        "projection_standard": projection_std,
        "projection_half_ppr": projection_half,
        "projection_ppr": projection_ppr,
        "dynasty_value": dynasty_val,
    }


def fetch_footballguys_and_normalize(year=None, week=None, root=DEFAULT_DOWNLOAD_ROOT):
    aggregated = {}
    sources = []
    for variant, position, url in _download_urls(year, week, root):
        rows, err = _read_csv_from_url(url)
        if err:
            sources.append({"url": url, "variant": variant, "position": position, "error": err})
            continue
        sources.append({"url": url, "variant": variant, "position": position, "count": len(rows)})
        for r in rows:
            mapped = _map_row_to_standard(r)
            pid = mapped["player_id"]
            existing = aggregated.get(pid)
            if not existing:
                aggregated[pid] = mapped
            else:
                # Prefer rest-of-season projections as the trade horizon, while
                # retaining weekly values if only the weekly file has a player.
                for k in ("player_name", "position", "team"):
                    if not existing.get(k) and mapped.get(k):
                        existing[k] = mapped[k]
                if variant == "restofseason" or existing.get("_variant") != "restofseason":
                    for k in ("projection_standard", "projection_half_ppr", "projection_ppr", "dynasty_value"):
                        if mapped.get(k) is not None:
                            existing[k] = mapped[k]
                    existing["_variant"] = variant
                aggregated[pid] = existing

    successful = [source for source in sources if "error" not in source]
    if not successful:
        return {"ok": False, "error": "All FootballGuys projection downloads failed", "sources": sources}
    return {"ok": True, "sources": sources, "aggregated": aggregated}


def save_aggregated(aggregated: dict, dest_path: str):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        for pid, row in aggregated.items():
            out = {k: row.get(k) for k in EXPECTED_COLUMNS}
            # ensure numeric fields are either blank or numeric strings
            for k in ("projection_standard", "projection_half_ppr", "projection_ppr", "dynasty_value"):
                v = out.get(k)
                out[k] = "" if v is None else str(v)
            writer.writerow(out)


def fetch_and_save(dest_path=None, year=None, week=None, root=DEFAULT_DOWNLOAD_ROOT):
    if not dest_path:
        dest_path = os.path.join(os.path.dirname(__file__), "data", "projections.csv")
    res = fetch_footballguys_and_normalize(year=year, week=week, root=root)
    if not res.get("ok"):
        return res
    aggregated = res.get("aggregated", {})
    try:
        save_aggregated(aggregated, dest_path)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to save: {exc}"}
    return {
        "ok": True,
        "saved_path": dest_path,
        "sources": res.get("sources", []),
        "count": len(aggregated),
        "projection_horizon": "restofseason preferred; weekly used when needed",
    }


def periodic_fetcher(interval_seconds=7 * 24 * 3600, year=None, week=None, root=DEFAULT_DOWNLOAD_ROOT):
    """Run an infinite loop fetching every interval_seconds. Exceptions are swallowed to keep the thread alive."""
    while True:
        try:
            print(f"[projections_fetcher] Running fetch at {time.ctime()}")
            r = fetch_and_save(year=year, week=week, root=root)
            print(f"[projections_fetcher] fetch result: {r}")
        except Exception as exc:
            print(f"[projections_fetcher] fetch failed: {exc}")
        time.sleep(interval_seconds)
