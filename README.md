# Mineopoly
https://mineopolyy.onrender.com/?lobby=22SO

Mineopoly is a real-time multiplayer territory game built with FastAPI, WebSockets, and a browser canvas frontend.

One player creates a lobby and shares the generated link. Other users open that link in their own tabs, join the match if player slots are still open, or spectate if the lobby is full or the game is already underway. Every valid capture is broadcast to all connected clients immediately, so the map, leaderboard, timer, and activity feed stay in sync for players and spectators.

## What the app does

- Shows a large grid with hundreds or thousands of cells depending on map size.
- Lets players claim unowned cells by clicking the map.
- Pushes updates live to every connected client through WebSockets.
- Supports multiple concurrent players in the same match without Redis.
- Includes spectators, a lobby flow, countdown start, cooldowns, mines, and score tracking.

## Current architecture

### Backend

- `main.py`
  - Starts the FastAPI app.
  - Serves `static/index.html`.
  - Exposes `/leaderboard` for the global hall of fame.
  - Handles the `/ws` WebSocket connection for all real-time game actions.
- `lobby.py`
  - Creates lobbies, joins players, joins spectators, tracks readiness, and manages lobby snapshots.
- `game.py`
  - Initializes the grid.
  - Handles cell captures.
  - Applies cooldowns.
  - Triggers and respawns mines.
  - Runs the game timer and ends the match.
- `leaderboard.py`
  - Tracks in-game scores and cumulative global stats.
- `redis_client.py`
  - Despite the filename, this now provides an in-memory shared state layer.
  - It emulates the small Redis feature set this project uses: hashes, sets, lists, sorted scores, TTLs, and pub/sub-style fanout.
  - It also includes a process-wide async lock so simultaneous captures do not corrupt state.

### Frontend

- `static/index.html`
  - Single-page UI for landing, lobby, player view, spectator view, game over screen, and hall of fame.
  - Uses a canvas to render large maps efficiently.
  - Supports pan, zoom, hover tooltips, live leaderboard updates, countdowns, and activity feed messages.

## Game flow

1. A player enters a username and creates a lobby.
2. The host chooses a map size: `small`, `medium`, or `large`.
3. The host shares the generated lobby link.
4. Up to 4 players join from that link.
5. When all 4 players are ready, the server starts a countdown.
6. The game begins and the grid becomes interactive.
7. Clicking an unclaimed block sends a `capture` message to the server.
8. Once a block is owned, it stays owned until a mine wipe removes it.
9. The server updates ownership and broadcasts the result to every connected client.
10. Scores are recalculated from owned territory plus cluster bonuses.
11. When the timer reaches zero, the match ends and final results are published.

## How to play

### Start a match

1. Open the app in your browser.
2. Enter your username.
3. Click `Create Game`.
4. Pick a map size.
5. Click `Create Lobby`.
6. Copy the generated lobby link and send it to other players.

### Join a match

1. Open the shared lobby link.
2. Enter your username.
3. Click `Join Lobby`.
4. If the lobby already has 4 players or the game has started, use `Spectate Lobby` instead.

### Begin the game

1. Wait until 4 players have joined.
2. Each player clicks `Ready Up`.
3. After all 4 players are ready, the match countdown starts automatically.
4. When the countdown reaches `GO`, the grid becomes active.

### Claim blocks

1. Click an unclaimed block to capture it.
2. After a successful capture, that block belongs to you.
3. Owned blocks cannot be stolen by other players.
4. If you click too quickly again, the cooldown prevents another instant capture.

### Move around the map

1. Drag on the board to pan around the grid.
2. Use the mouse wheel or trackpad scroll to zoom in and out.
3. Hover over a claimed block to see who owns it.

### Understand mines

1. Some hidden cells are mines.
2. If you click a mine, it explodes and clears owned cells in a 3x3 area.
3. Cleared blocks become unclaimed again and can be captured later.
4. Mines respawn after a short delay while the match is still running.

### Win condition

1. The match ends when the timer reaches zero.
2. Your score is based on how many cells you own.
3. Large connected territory groups can add a small bonus.
4. The player with the highest final score finishes on top of the leaderboard.

### Spectating

1. Spectators can watch the board, timer, activity feed, and leaderboard in real time.
2. Spectators cannot claim cells or ready up.

## Real-time multiplayer behavior

The multiplayer behavior the app currently guarantees in a single server process:

- Shared grid state for all connected users.
- Immediate broadcast of captures, mine effects, countdown ticks, and game-over events.
- Safe concurrent updates inside one Python process using an async lock around critical state changes.
- Spectators see the same live board updates as players.
- Owned cells are non-stealable; only unclaimed cells can be captured.

Important limitation:

- The current in-memory backend is single-process only.
- If you restart the server, all lobbies, matches, and leaderboard state reset.
- If you run multiple server instances, they will not share state with each other.

For local development and one deployed app instance, this is fine. For horizontal scaling or durable persistence, Redis or a database-backed event/state layer would be needed later.

## Grid sizes

- `small`: 20 x 15 = 300 cells, 15 mines, 300 seconds
- `medium`: 40 x 30 = 1200 cells, 60 mines, 600 seconds
- `large`: 60 x 45 = 2700 cells, 135 mines, 900 seconds

## Scoring

- Each owned cell is worth 1 point.
- Connected clusters of size 5 or more add a small bonus.
- Leaderboards update after captures and mine wipes.

## Special mechanics

### Cooldown

- Each successful capture applies a 1.5 second cooldown to that player.
- The client shows the remaining cooldown visually.

### Mines

- Hidden mines are seeded when the match starts.
- Clicking a mine wipes owned cells in a 3x3 area around that cell.
- Mines respawn after a delay if the match is still active.
- Mine wipes are the main way claimed cells become unclaimed again.

### Spectators

- Users can join as spectators if a lobby is full or already in progress.
- Spectators receive the same real-time board and leaderboard events, but cannot capture cells.

### Lobby links

- There is no manual lobby-code entry screen anymore.
- The host copies a shareable URL like `/?lobby=AB12`.
- Visitors opening that URL can join the active lobby directly from the landing screen.
- Shared-link visitors cannot create a new game from that link unless they are the original host in that browser session.

## Running locally

### Requirements

- Python 3.11+

### Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Start the server

```powershell
python main.py
```

Then open:

- `http://localhost:8000`

## Deploy on Render

This repo includes a [render.yaml](/C:/Users/Ashut/OneDrive/Desktop/Projects/Mineopoly/render.yaml) blueprint for Render.

### Quick deploy steps

1. Push the project to GitHub.
2. Sign in to Render.
3. Create a new Blueprint or Web Service from the repository.
4. If using the blueprint flow, Render will read `render.yaml` automatically.
5. Deploy the service.

### Render config used

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Plan: `free`

Important:

- This app uses in-memory state.
- If the free instance restarts or spins down, active lobbies and current game state are lost.

## WebSocket messages

Examples of message types used by the app:

- Client to server
  - `create_lobby`
  - `join_lobby`
  - `join_spectate`
  - `ready_up`
  - `capture`
- Server to client
  - `lobby_created`
  - `lobby_update`
  - `countdown`
  - `game_start`
  - `capture`
  - `mine_triggered`
  - `mine_respawned`
  - `cooldown`
  - `timer_tick`
  - `game_over`
  - `spectator_update`

## Files

- [main.py](/C:/Users/Ashut/OneDrive/Desktop/Projects/Mineopoly/main.py)
- [lobby.py](/C:/Users/Ashut/OneDrive/Desktop/Projects/Mineopoly/lobby.py)
- [game.py](/C:/Users/Ashut/OneDrive/Desktop/Projects/Mineopoly/game.py)
- [leaderboard.py](/C:/Users/Ashut/OneDrive/Desktop/Projects/Mineopoly/leaderboard.py)
- [redis_client.py](/C:/Users/Ashut/OneDrive/Desktop/Projects/Mineopoly/redis_client.py)
- [static/index.html](/C:/Users/Ashut/OneDrive/Desktop/Projects/Mineopoly/static/index.html)

## Next scaling step

If you later want persistence or multi-instance deployment, the cleanest path is to keep the current module boundaries and replace the in-memory state layer with Redis or another shared backend without rewriting the whole app.
