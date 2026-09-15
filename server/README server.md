# Fantasy Football Trivia — Web App

FastAPI backend + vanilla JS frontend, connected to the PostgreSQL database
built by `import_data.py`.

## Architecture

```
Browser (index.html)
      │  fetch()
      ▼
FastAPI (main.py)  ──►  PostgreSQL (fantasy_football DB)
```

- Single Python process serves both the API (`/api/...`) and the frontend (`/`).
- No build step, no Node.js required — just Python + your existing Postgres container.

---

## Prerequisites

You should already have from the previous step:
1. The `fantasy-football-db` folder with `docker-compose.yml` running PostgreSQL.
2. Data imported via `import_data.py` (teams, players, rosters, stats, fantasy scores).

Verify your database is up:
```bash
docker ps
```
You should see `fantasy_football_db` in the list. If not:
```bash
cd path/to/fantasy-football-db
docker compose up -d
```

---

## Folder Setup

Place this `app/` folder **next to** your `fantasy-football-db` folder, e.g.:

```
your-projects/
├── fantasy-football-db/     <- from previous step (docker-compose, import_data.py)
└── app/                     <- this new folder
    ├── main.py
    ├── requirements.txt
    └── static/
        └── index.html
```

They don't need to be nested — the app connects to Postgres over `localhost:5432`,
so as long as the DB container is running, location doesn't matter.

---

## 1. Install Dependencies

From inside the `app/` folder:

```bash
python -m pip install -r requirements.txt
```

## 2. Configure Database Connection (optional)

By default, `main.py` connects using the same credentials as `import_data.py`:

```
host=localhost  port=5432  dbname=fantasy_football
user=fantasy_admin  password=changeme_secure_password
```

If you changed the password in `docker-compose.yml`, set environment variables
before starting the app instead of editing the code:

**Windows PowerShell:**
```powershell
$env:DB_PASSWORD="your_new_password"
```

**Mac/Linux:**
```bash
export DB_PASSWORD="your_new_password"
```

## 3. Run the App

```bash
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Then open your browser to:

```
http://localhost:8000
```

You should see the trivia question load automatically, followed by a live,
clickable draft board.

---

## How It Works

1. **On page load**, the frontend calls `GET /api/trivia/random`, which picks
   a random position + season + rule from the database and returns a trivia prompt.
2. **Clicking "Waiting" in your (You) column** activates that roster slot.
3. **Typing in the search box** calls `GET /api/players/search`, filtered by
   the active slot's position and the trivia season.
4. **Clicking a search result** calls `POST /api/draft/pick`, which:
   - Looks up that player's real stats for the season from PostgreSQL
   - Validates the pick against the trivia rule (e.g. 1,000+ yards, position match)
   - Returns the computed PPR fantasy score if valid, or a rejection reason if not
5. The board re-renders with the new pick and updated team point total.

---

## API Reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/health` | Confirms DB connection + player count |
| GET | `/api/trivia/random?position=QB` | Get a random trivia prompt |
| GET | `/api/trivia/rules` | List all available trivia rule types |
| GET | `/api/players/search?q=mahomes&position=QB&season=2020` | Search players |
| GET | `/api/players/{player_id}/seasons` | All season stats for one player |
| GET | `/api/leaderboard?season=1999&position=QB&scoring_format=ppr` | Top players for a season |
| POST | `/api/draft/pick` | Validate + score a draft pick |

Test the health check directly:
```bash
curl http://localhost:8000/api/health
```
Expected response:
```json
{"status": "ok", "players_loaded": 25033}
```

---

## Troubleshooting

**"Could not reach backend" in the browser**
- Confirm `uvicorn` is still running in your terminal (no red errors).
- Confirm Docker's Postgres container is running (`docker ps`).

**`psycopg2.OperationalError: connection refused`**
- Postgres isn't running. Run `docker compose up -d` from the `fantasy-football-db` folder.

**Trivia question loads but search returns nothing**
- Double check `import_data.py` completed successfully and `player_stats_seasonal`
  has rows for the season shown in the trivia prompt (weekly data starts at 1999).

**CORS errors in browser console**
- Shouldn't happen since frontend and API share the same origin (`localhost:8000`),
  but if you split them onto different ports later, CORS is already enabled in `main.py`.

---

## Next Steps

- Add multiplayer (WebSocket-based turn order instead of a single local player)
- Add authentication so "You" isn't hardcoded to team index 0
- Expand `TRIVIA_RULES` with more prompt types (college, draft round, all-pro years, etc.)
- Deploy behind your existing Cloudflare Tunnel setup once ready for remote access
