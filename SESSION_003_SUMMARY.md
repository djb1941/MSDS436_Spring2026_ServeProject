# Session 003 Summary — SimPy Simulation Engine
**Date:** 2026-05-25  
**Project:** Serve Robotics Simulation (MSDS436 Term Project)

---

## What Was Built This Session

### 1. Database Schema Updates (`init-db.sql`)

- Swapped the PostgreSQL image from `postgis/postgis:16-3.4` to `mobilitydb/mobilitydb:1.1.1-pg16-postgis34` to enable the MobilityDB extension
- Added `robotics.simulations` table (new) — one row per simulation run, acts as the job queue between web-api and the engine
- Added `duration_hours`, `real_time`, `speed_factor` columns to `simulations`
- Added `simulation_id` FK to `robotics.deliveries` and `robotics.robot_locations`
- Renamed `deliveries` columns to align with sim engine: `start_time` → `requested_at`, `end_time` → `delivered_at`, added `picked_up_at`
- Added `robotics.active_routes` table — one row per robot (upserted), stores current leg waypoints as JSONB for live map polylines
- Fixed `started_at` to be nullable (no default) — it is set by the engine when it claims a job, not at insert time
- Added composite index `(simulation_id, timestamp)` on `robot_locations`

**Critical:** Requires `docker compose down -v && docker compose up --build` to apply schema changes since PostgreSQL only runs `init-db.sql` on a fresh volume.

### 2. Simulation Engine Files (all in `app/`)

#### `db_writer.py` — `SimulationDatabaseWriter`
- Takes `(db_url, sim_id, num_robots)` — uses an already-created simulation row (created by web-api)
- Pre-inserts N robots into `robotics.robots` at startup so FK constraints are satisfied
- **Buffered location writes:** `log_movement()` queues rows; background thread flushes every 5s OR when buffer hits 100 rows (dual-trigger)
- Uses `psycopg2.extras.execute_values` for batch inserts — much faster than `executemany`
- **Immediate delivery writes:** `create_delivery()`, `mark_picked_up()`, `mark_delivered()`
- **Route tracking:** `update_route()` upserts current leg waypoints to `active_routes`; `clear_route()` removes the row when a robot goes idle
- `close()` flushes buffer, marks simulation completed, closes connection

#### `dispatcher.py` — `Dispatcher`
- Central coordinator — robots do NOT pull from a shared queue; the dispatcher assigns deliveries based on proximity
- `request_delivery(request)`: if idle robots exist, assigns to the closest one (Euclidean distance to restaurant); otherwise holds in `_pending`
- `robot_available(robot)`: if pending requests exist, assigns the closest restaurant to the robot's current position; otherwise adds robot to idle pool
- Uses simple Euclidean distance on lat/lon for dispatch decisions (fast); actual routing uses NetworkX

#### `delivery_generator.py` — `DeliveryGenerator`
- Runs as a SimPy process; calls `dispatcher.request_delivery()` on each generated request
- Poisson arrival process: `random.expovariate(rate)` gives exponentially-distributed inter-arrival gaps
- Peak-hour multipliers applied by checking sim time against parsed YAML windows
- Loads restaurants and residences from PostGIS at startup (`ST_Y`/`ST_X` to extract lat/lon)
- SimPy time unit is **minutes**; DB timestamps are in seconds (multiply `env.now * 60`)

#### `robot_agent.py` — `RobotAgent`
- Each robot has a personal `simpy.Store(env, capacity=1)` inbox — dispatcher puts assignments here
- `run()` loop: signal dispatcher → block on `inbox.get()` → create delivery in DB → travel to restaurant → pick up → travel to residence → mark delivered → repeat
- `_route_info(origin, dest)` returns `(travel_time_s, distance_m, path_node_ids)` using `nx.shortest_path(..., weight='travel_time')`
- `_path_waypoints(path)` converts OSM node IDs to `(lat, lon)` using `G.nodes[node]['y']` and `G.nodes[node]['x']`
- Calls `db_writer.update_route()` after each `nx.shortest_path()` call, `db_writer.clear_route()` on idle
- `G[path[i]][path[i+1]][0]` — the `[0]` is required because OSMnx graphs are `MultiDiGraph`s

#### `main.py` — Polling loop
- Polls `robotics.simulations` for `status='pending'` rows using `FOR UPDATE SKIP LOCKED` (atomic claim, safe for multiple workers)
- Claims a job → sets `status='running'` → runs simulation → marks `status='completed'` or `'failed'`
- `run_simulation()` wires all components: loads GraphML, creates `SimulationDatabaseWriter`, `Dispatcher`, `DeliveryGenerator`, N `RobotAgent`s, then calls `env.run(until=duration_min)`
- **Real-time mode:** uses `simpy.rt.RealtimeEnvironment(factor=1.0/speed_factor, strict=False)` instead of `simpy.Environment()`
  - `factor` = wall-clock seconds per simulated minute
  - `speed_factor` = simulated minutes per real second (user-facing parameter)
  - `strict=False` — if a route calculation takes longer than the timeout, SimPy catches up gracefully

#### `delivery_configs/default.yaml` — Default config
```yaml
simulation:
  name: "Default Scenario"
  duration_hours: 24
  start_time: "06:00"
delivery_generation:
  type: "poisson"
  rate: 0.5          # deliveries/minute
  peak_hours:
    - hours: "11:00-13:00"
      multiplier: 3.0
    - hours: "17:00-19:00"
      multiplier: 2.5
constraints:
  robots_available: 5
  max_delivery_distance_km: 8.0
```

### 3. Web API (`web_api/app/main.py`)

- **`POST /api/v1/simulations/start`** — inserts a `'pending'` row into `simulations` and returns `sim_id` immediately; accepts `config_name`, `num_robots`, `duration_hours`, `real_time`, `speed_factor`
- **`POST /api/v1/simulations/{id}/stop`** — updates status to `'stopped'`
- **`GET /api/v1/simulations`** — lists all runs, most recent first
- **`GET /api/v1/simulations/{id}/results`** — final stats for a completed run
- **`GET /api/v1/stats`** — aggregate stats for the most recent simulation
- **`WS /ws/robots/{sim_id}`** — pushes latest position per robot every 1s using `DISTINCT ON (robot_id)`, left-joined with `active_routes` to include `route_coords` and `leg_type`
- **`WS /ws/deliveries/{sim_id}`** — pushes delivery counts (total, completed, in_progress, pending) + 10 most recent completions every 2s

The `/sim_engine` volume mount was intentionally removed — web-api and python-app communicate through the DB only.

### 4. Flet Frontend — Controls View Updates (`frontend/app/main.py`)

- Added **Real-time mode** `ft.Switch` toggle (off by default)
- Added **Sim speed** `ft.TextField` (simulated minutes per real second) — disabled until toggle is on
- `start_simulation()` now includes `real_time` and `speed_factor` in the POST body

### 5. Flet Frontend — Live Map Updates (`frontend/app/main.py`)

- Added `fmap.PolylineLayer` to the map, drawn between TileLayer and MarkerLayer (so dots sit on top of lines)
- `refresh_map()` now rebuilds both markers and polylines from `state.robots`
- Route colors: orange (`to_restaurant`), green (`to_residence`) — matching marker status colors
- Legend updated to show both dot icons and line swatches

---

## Architecture as of End of Session

```
docker compose up --build
    ├── postgres (mobilitydb/mobilitydb:1.1.1-pg16-postgis34)   port 5432
    │     └── init-db.sql — full schema including active_routes
    ├── web-api (Dockerfile.webapi / FastAPI)                    port 8000
    │     ├── POST /api/v1/simulations/start → inserts 'pending' row
    │     ├── GET/WS endpoints → real DB queries
    │     └── No sim engine code — DB-only communication
    └── python-app (Dockerfile.python / Fedora 40)              port 8501
          ├── setup.py      ← OSM download + PostGIS population (blocking)
          ├── main.py       ← Polling loop: claims 'pending' jobs, runs SimPy
          └── frontend/app/main.py ← Flet web UI (background, port 8501)
```

**Inter-container communication pattern:**
```
Flet UI  →  POST /simulations/start  →  web-api
web-api  →  INSERT status='pending'  →  postgres
python-app  →  polls FOR UPDATE SKIP LOCKED  →  claims job
python-app  →  runs SimPy  →  writes robot_locations, deliveries, active_routes
web-api  →  queries postgres  →  WebSocket push  →  Flet UI
```

---

## Files Created / Modified This Session

| File | Status | Notes |
|------|--------|-------|
| `init-db.sql` | Modified | New tables: simulations, active_routes; updated deliveries + robot_locations |
| `docker-compose.yml` | Modified | New postgres image; removed dead /sim_engine mount |
| `app/db_writer.py` | Created | Buffered writer with route tracking |
| `app/dispatcher.py` | Created | Proximity-based delivery assignment |
| `app/delivery_generator.py` | Created | Poisson arrival process, YAML config |
| `app/robot_agent.py` | Created | SimPy process, NetworkX routing, route storage |
| `app/main.py` | Rewritten | DB polling loop replacing original stub |
| `app/delivery_configs/default.yaml` | Created | Default simulation config |
| `web_api/app/main.py` | Rewritten | Real DB queries replacing all stubs |
| `requirements-webapi.txt` | Modified | Cleaned up unused sim engine deps |
| `frontend/app/main.py` | Modified | Real-time toggle + polyline map layer |

---

## Key Concepts Covered This Session

- **SimPy discrete-event simulation** — `env.run()` executes all processes on a single thread; `yield env.timeout(t)` suspends a process; `yield store.get()` blocks until an item is available
- **SimPy time units** — unitless; we chose minutes. All DB timestamps convert with `env.now * 60`
- **`simpy.rt.RealtimeEnvironment`** — synchronizes sim time to wall clock; `factor` = wall seconds per sim unit; `strict=False` allows catch-up
- **Poisson arrivals** — inter-arrival times drawn from `random.expovariate(rate)`; rate × time = expected count
- **`FOR UPDATE SKIP LOCKED`** — atomically claim a DB row without locking others; standard pattern for job queues
- **`DISTINCT ON (col)`** — PostgreSQL-specific; gets one row per group efficiently without a subquery
- **`psycopg2.extras.execute_values`** — batches many rows into a single INSERT; much faster than one insert per row
- **Foreign key insert order** — simulations → robots → deliveries/robot_locations (each table must exist before rows that FK-reference it)
- **OSMnx node coordinates** — `G.nodes[node_id]['y']` = latitude, `G.nodes[node_id]['x']` = longitude
- **MultiDiGraph edge access** — `G[u][v][0]` gets the first edge between two nodes (OSMnx graphs can have parallel edges)

---

## Known Issues / Next Steps for Debug Session

- Program is not working as intended — debug session needed
- Likely areas to investigate:
  - `docker compose down -v` required before rebuild (schema changes don't apply to existing volumes)
  - `active_routes` table FK constraint requires robots to be pre-inserted before routes can be written
  - `simpy.rt.RealtimeEnvironment` import path (`import simpy.rt` required explicitly)
  - `delivery_configs/default.yaml` must be present at `/app/delivery_configs/default.yaml` inside the container (check volume mount in `docker-compose.yml`)
  - WebSocket `route_coords` arrives as a Python list (already parsed from JSON by psycopg2 JSONB handling) — confirm frontend isn't expecting a string
