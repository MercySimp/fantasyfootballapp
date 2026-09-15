# Fantasy Football Database

PostgreSQL database for the fantasy football trivia app.  
Data sourced from [nflverse](https://nflverse.com/) via `nflreadpy`.

---

## Quick Start

### 1. Start the Database
```bash
docker compose up -d
```
- PostgreSQL → `localhost:5432`
- pgAdmin (browser UI) → `http://localhost:5050`
  - Login: `admin@local.dev` / `changeme_secure_password`

The schema is auto-created on first boot via `init/01_schema.sql`.

### 2. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 3. Import Data

**Full import (all seasons, 1999–present):**
```bash
python import_data.py
```
> ⏱ Expect ~10–20 minutes for a full import. nflreadpy caches downloads locally.

**Specific seasons only:**
```bash
python import_data.py --seasons 1995 1996 1997 1998 1999 2000
```
> ⚠️ Weekly stats only go back to 1999 in nflverse. For 1997–1998, see below.

**Skip weekly stats (faster, seasonal totals only):**
```bash
python import_data.py --skip-weekly
```

**Recalculate fantasy scores without re-importing:**
```bash
python import_data.py --recalc-scores
```

---

## Pre-1999 Data (1997, 1998, etc.)

nflverse weekly stats start in 1999. For earlier seasons, use the free CSV files  
from [Fantasy Football Data Pros](https://www.fantasyfootballdatapros.com/csv_files)  
which have yearly data back to 1970.

Download the yearly CSV for the season you need, then run:
```sql
-- After loading the CSV into a staging table, copy into player_stats_seasonal:
INSERT INTO player_stats_seasonal (player_id, season, ...)
SELECT ...
FROM your_staging_table;
```
You will also need to match player IDs manually or by name — a helper script for  
this is planned for a future update.

---

## Database Schema

| Table | Description |
|---|---|
| `teams` | All NFL franchises with colors and logos |
| `players` | All-time player registry (IDs, position, bio, cross-reference IDs) |
| `rosters` | Player ↔ team per season |
| `player_stats_weekly` | Week-level offense stats (1999–present) |
| `player_stats_seasonal` | Full-season aggregated totals |
| `scoring_formats` | Standard / Half-PPR / PPR formulas |
| `fantasy_scores_seasonal` | Precomputed fantasy totals + position ranks |
| `trivia_player_seasons` | **VIEW** — everything joined, used by trivia queries |

---

## Scoring Formats

| Format | Reception Bonus | Pass TD | Rush TD | Rec TD |
|---|---|---|---|---|
| `standard` | 0 pts | 4 | 6 | 6 |
| `half_ppr` | 0.5 pts | 4 | 6 | 6 |
| `ppr` | 1.0 pts | 4 | 6 | 6 |

All formats: 1 pt / 25 pass yds · 1 pt / 10 rush or rec yds · −2 fumble lost · −2 INT

---

## Example Trivia Query
```sql
-- "Draft a QB from 1997" — top options by PPR score
SELECT display_name, team, passing_yards, passing_tds, fantasy_pts_ppr
FROM trivia_player_seasons
WHERE season = 1997 AND position = 'QB'
ORDER BY fantasy_pts_ppr DESC NULLS LAST
LIMIT 10;
```
See `example_queries.sql` for more.

---

## Self-Hosting

This stack runs on any machine with Docker. To expose it for remote access via  
your existing Cloudflare Tunnel setup, only expose the **web app** port — never  
expose PostgreSQL (port 5432) directly to the internet.

```
your-domain.com  →  web app (e.g. port 8080)
                       ↓
                   fantasy_football (postgres, internal only)
```

---

## Changing the DB Password

Edit `docker-compose.yml` and `import_data.py` — both have `changeme_secure_password`.  
Do this before first run.

