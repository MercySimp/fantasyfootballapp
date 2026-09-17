"""Fetch projection CSVs from FootballGuys (anonymous) and normalize them.
Saves a consolidated CSV at data/projections.csv (rest-of-season / full season
horizon) with columns:
player_id,player_name,position,team,projection_standard,projection_half_ppr,projection_ppr,dynasty_value

Also saves data/projections_weekly.csv (short-term, single upcoming week
horizon) with the same columns, for trade/roster decisions where near-term
value matters more than full-season value (e.g. evaluating a trade right
before a bye week or a tough playoff schedule stretch).

This is best-effort: when player_id is not available the normalized player_name+team is used as a stable id.

Fix notes (2026-09-17 review):
- Anonymous FootballGuys downloads are not authenticated and may silently
  return a login/paywall HTML page with an HTTP 200 status. We now sniff the
  response body and reject anything that looks like HTML, and require a
  minimum row count plus a recognizable player-name column before treating a
  download as valid.
- A source with zero real projections rows is no longer counted as
  "successful" just because the HTTP request didn't raise an exception.
- The default week now advances automatically based on the current date
  instead of being pinned to Week 2 all season.
- Before overwriting either existing CSV, we compare the new row count
  against the current file. If the new fetch would replace a healthy file
  with a drastically smaller one, we refuse to save and report an error
  instead of silently degrading the data.
- 2026-09-17 (weekly-projections request): previously the "weekly" and
  "restofseason" variants were merged into a single aggregated dict with
  restofseason always winning, so short-term/single-week projections were
  fetched but then silently discarded. Now both horizons are tracked and
  saved separately -- data/projections.csv stays rest-of-season (used for
  season-long trade value), and the new data/projections_weekly.csv carries
  the single-upcoming-week numbers for short-term decisions.
- Known follow-up (not fixed here): the fallback player_id scheme
  (normalized name + team) does not match the PFR-style IDs used elsewhere
  in this app's players table / data/projections.csv. Confirm the real ID
  scheme used by the `players` table and align `_make_player_id` to it, or
  rely exclusively on name-based matching in trade_analyzer.py until then.
"""

import csv
import io
import os
import re
import time
import urllib.request
from datetime import date

DEFAULT_DOWNLOAD_ROOT = "https://www.footballguys.com/projections/download"
DEFAULT_YEAR = str(date.today().year)
MIN_VALID_ROWS = 5
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
DEFAULT_VARIANTS = ("weekly", "restofseason")


def _current_nfl_week(today=None):
    """Best-effort estimate of the current NFL week based on the calendar.

    This is a heuristic (regular season kicks off roughly the first Thursday
    after Labor Day) and should be treated as an approximation, not a source
    of truth. It exists only so the fetcher doesn't stay pinned to Week 2 for
    the entire season.
    """
    today = today or date.today()
    season_start = date(today.year, 9, 5)
    if today < season_start:
        return 1
    delta_days = (today - season_start).days
    week = (delta_days // 7) + 1
    return max(1, min(week, 18))


DEFAULT_WEEK = os.getenv("FOOTBALLGUYS_WEEK", str(_current_nfl_week()))


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

    NOTE: this URL pattern has not been independently verified against a live
    authenticated FootballGuys account. Treat downloads from it as untrusted
    until confirmed, which is why _read_csv_from_url() below validates the
    response shape before accepting it.
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


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip().lower()
    return stripped.startswith("<!doctype") or stripped.startswith("<html") or "<body" in stripped[:2000]


def _looks_like_valid_projection_csv(rows, headers):
    if not headers or not rows:
        return False, "empty response (no headers or no rows)"
    lowered = [h.strip().lower() for h in headers if h]
    has_name_col = any(h in ("player", "player_name", "name", "display_name") for h in lowered)
    if not has_name_col:
        return False, f"no recognizable player-name column in headers: {headers}"
    if len(rows) < MIN_VALID_ROWS:
        return False, f"only {len(rows)} rows returned (expected at least {MIN_VALID_ROWS})"
    return True, None


def _read_csv_from_url(url: str):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as resp:
            content = resp.read()
            text = content.decode("utf-8", errors="ignore")
            if _looks_like_html(text):
                return None, "response body looks like an HTML page (likely a login/paywall redirect), not CSV"
            fh = io.StringIO(text)
            reader = csv.DictReader(fh)
            rows = list(reader)
            headers = reader.fieldnames or []
            valid, reason = _looks_like_valid_projection_csv(rows, headers)
            if not valid:
                return None, f"response did not look like a valid projections CSV: {reason}"
            return rows, None
    except Exception as exc:
        return None, str(exc)


def _map_row_to_standard(row: dict):
    name_keys = [k for k in row.keys() if k.lower() in ("player", "player_name", "name", "display_name")]
    team_keys = [k for k in row.keys() if k.lower() in ("team", "team_abbr", "tm")]
    pos_keys = [k for k in row.keys() if k.lower() in ("pos", "position")]

    ppr_keys = [k for k in row.keys() if "ppr" in k.lower() or "fantasy_pts_ppr" in k.lower()]
    half_keys = [k for k in row.keys() if "half" in k.lower() or "half_ppr" in k.lower()]
    std_keys = [k for k in row.keys() if "standard" in k.lower() or "std" == k.lower() or "fantasy_pts_standard" in k.lower()]
    generic_score_keys = [k for k in row.keys() if k.lower() in ("proj", "projection", "fantasy_pts", "fantasy_points", "fpts", "points")]

    name = _normalize_name(row.get(name_keys[0]) if name_keys else row.get(next(iter(row), "")))
    team = (row.get(team_keys[0]) if team_keys else "") if team_keys else ""
    pos = (row.get(pos_keys[0]) if pos_keys else "") if pos_keys else ""

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
                num = re.sub(r"[^0-9\.-]", "", v)
                try:
                    return float(num) if num not in ("", ".", "-") else None
                except Exception:
                    continue
        return None

    projection_ppr = _num_from_keys(ppr_keys) or _num_from_keys(generic_score_keys)
    projection_half = _num_from_keys(half_keys) or _num_from_keys(generic_score_keys)
    projection_std = _num_from_keys(std_keys) or _num_from_keys(generic_score_keys)

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
    """Fetches both the weekly (single upcoming week) and restofseason
    (full-season horizon) variants and keeps them as SEPARATE aggregated
    dicts -- previously these were merged into one dict with restofseason
    always winning, silently discarding the weekly numbers entirely.
    """
    aggregated = {"weekly": {}, "restofseason": {}}
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
            existing = aggregated[variant].get(pid)
            if not existing:
                aggregated[variant][pid] = mapped
            else:
                for k in ("player_name", "position", "team"):
                    if not existing.get(k) and mapped.get(k):
                        existing[k] = mapped[k]
                for k in ("projection_standard", "projection_half_ppr", "projection_ppr", "dynasty_value"):
                    if mapped.get(k) is not None:
                        existing[k] = mapped[k]
                aggregated[variant][pid] = existing

    # A source only counts as "successful" if it actually returned rows we
    # were able to parse into real projection data, not merely a 200 status.
    successful = [source for source in sources if "error" not in source and source.get("count", 0) > 0]
    if not successful or not (aggregated["weekly"] or aggregated["restofseason"]):
        return {
            "ok": False,
            "error": "All FootballGuys projection downloads failed or returned no usable rows",
            "sources": sources,
        }
    return {"ok": True, "sources": sources, "aggregated": aggregated}


def save_aggregated(aggregated: dict, dest_path: str):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        for pid, row in aggregated.items():
            out = {k: row.get(k) for k in EXPECTED_COLUMNS}
            for k in ("projection_standard", "projection_half_ppr", "projection_ppr", "dynasty_value"):
                v = out.get(k)
                out[k] = "" if v is None else str(v)
            writer.writerow(out)


def _existing_row_count(dest_path: str) -> int:
    if not dest_path or not os.path.exists(dest_path):
        return 0
    try:
        with open(dest_path, newline='', encoding='utf-8') as fh:
            return max(0, sum(1 for _ in fh) - 1)
    except OSError:
        return 0


def _safe_save(aggregated_variant: dict, dest_path: str, label: str):
    existing_count = _existing_row_count(dest_path)
    if existing_count >= 20 and len(aggregated_variant) < existing_count * 0.5:
        return {
            "ok": False,
            "error": (
                f"Refusing to overwrite existing {label} ({existing_count} rows) "
                f"with a much smaller fetch result ({len(aggregated_variant)} rows) -- this "
                f"usually means the FootballGuys fetch was blocked, redirected, or unauthenticated."
            ),
        }
    try:
        save_aggregated(aggregated_variant, dest_path)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to save {label}: {exc}"}
    return {"ok": True, "saved_path": dest_path, "count": len(aggregated_variant)}


def fetch_and_save(dest_path=None, weekly_dest_path=None, year=None, week=None, root=DEFAULT_DOWNLOAD_ROOT):
    if not dest_path:
        dest_path = os.path.join(os.path.dirname(__file__), "data", "projections.csv")
    if not weekly_dest_path:
        weekly_dest_path = os.path.join(os.path.dirname(__file__), "data", "projections_weekly.csv")

    res = fetch_footballguys_and_normalize(year=year, week=week, root=root)
    if not res.get("ok"):
        return res

    aggregated = res.get("aggregated", {})
    restofseason_result = _safe_save(aggregated.get("restofseason", {}), dest_path, "projections.csv")
    weekly_result = _safe_save(aggregated.get("weekly", {}), weekly_dest_path, "projections_weekly.csv")

    ok = restofseason_result.get("ok") or weekly_result.get("ok")
    return {
        "ok": ok,
        "restofseason": restofseason_result,
        "weekly": weekly_result,
        "sources": res.get("sources", []),
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
