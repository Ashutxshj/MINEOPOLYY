# Mineopoly

Live app: https://mineopolyy.onrender.com/

Mineopoly is a real-time multiplayer territory game built with FastAPI, WebSockets, and a browser canvas UI.

## Stack

- Python
- FastAPI
- WebSockets
- HTML, CSS, JavaScript
- Canvas
- In-memory state layer

## How It Works

- One player creates a lobby and shares the link.
- Up to 4 players join the match.
- Extra users join as spectators.
- All 4 players must `Ready Up` before the countdown starts.
- Players click empty cells to capture territory.
- Hidden mines can wipe nearby claimed cells.
- The player with the highest score when the timer ends wins.

## Features

- Real-time lobby and match updates
- Spectator mode
- Shareable lobby links
- Countdown start flow
- Capture cooldown
- Hidden mine mechanic
- Hall of Fame leaderboard
- Pan, zoom, and hover tooltips
- Multiple map sizes

## Map Sizes

- `small`: 20 x 15, 15 mines, 300 seconds
- `medium`: 40 x 30, 60 mines, 600 seconds
- `large`: 60 x 45, 135 mines, 900 seconds

## Run Locally

Requirements:

- Python 3.11+

Install:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start:

```powershell
python main.py
```

Open `http://localhost:8000`

## Deploy on Render

This repo includes `render.yaml`, so the easiest path is:

1. Push the repo to GitHub.
2. In Render, create a new `Blueprint` from the repo.
3. Deploy.

If you create the service manually, use:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Runtime: `python`

## Project Files

- `main.py` - FastAPI app and WebSocket entrypoint
- `lobby.py` - lobby creation, joins, readiness, spectators
- `game.py` - grid setup, captures, mines, timer
- `leaderboard.py` - in-game and global leaderboard logic
- `redis_client.py` - in-memory shared state and pub/sub-style fanout
- `static/index.html` - frontend UI

## Limitation

State is stored in memory. If the server restarts, active lobbies, current matches, and leaderboard data reset. It also means the app is meant to run as a single instance unless the state layer is replaced with Redis or a database.
