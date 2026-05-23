# Session 002 Summary — Flet Frontend + Data Setup
**Date:** 2026-05-23  
**Project:** Serve Robotics Simulation (MSDS436 Term Project)

---

## What Was Built This Session

### 1. Flet Frontend (`frontend/app/main.py`)
A full web UI served on port 8501 with five views connected via a left `NavigationRail` sidebar.

| View | Description | Data source |
|------|-------------|-------------|
| Controls | Form to configure and launch a simulation run | REST polling |
| Dashboard | Live stat cards (deliveries, robots, avg time) | REST polling every 5s |
| Robot Status | Per-robot live table (ID, status, lat, lon, delivery) | WebSocket |
| Delivery Log | Scrollable feed of in-flight and completed deliveries | WebSocket |
| Live Map | Interactive OSM tile map with robot markers | WebSocket |

**Key patterns:**
- `AppState` — shared Python object that holds live data caches (`robots`, `delivery_summary`, `recent_deliveries`) and callback hooks (`on_robot_update`, `on_delivery_update`). Views register callbacks; WebSocket threads call them to trigger redraws.
- Polling uses `httpx` in daemon threads. WebSockets use the `websockets` library, each in its own daemon thread running `asyncio.run()`.
- The delivery log and map hooks are **chained** — each view preserves the previous hook and calls it before its own, so all views update from a single WebSocket event.

### 2. FastAPI WebSocket Endpoints (`web_api/app/main.py`)
Two new WebSocket routes added alongside the existing REST endpoints:

- `WS /ws/robots/{sim_id}` — pushes robot positions every 1s  
- `WS /ws/deliveries/{sim_id}` — pushes delivery summary + recent list every 2s

New REST endpoints added:
- `POST /api/v1/simulations/start` — launches a simulation, returns `simulation_id`
- `POST /api/v1/simulations/{id}/stop`
- `GET  /api/v1/simulations` — lists past runs
- `GET  /api/v1/simulations/{id}/results` — final stats after run completes

All WS endpoints have `# TODO` stubs where real DB queries will replace the empty payloads once the SimPy engine is writing to PostgreSQL.

### 3. Container Consolidation
Flet was initially given its own container (`flet-frontend`) then merged into `python-app` to simplify the stack while the simulation engine is still a stub.

`start.sh` launch order inside `python-app`:
```
python3 /app/setup.py      # blocks until complete
flet run --web ... &       # background
python3 /app/main.py &     # background
wait -n                    # exit if either child dies
```

### 4. OSM Data Setup Script (`app/setup.py`)
Idempotent script that runs once at container startup and skips steps already done.

**Step 1 — Road network:**
- Downloads Glendale, CA drive network via `osmnx`
- Adds edge speeds and travel times (`ox.add_edge_speeds`, `ox.add_edge_travel_times`)
- Saves as GraphML to `/app/data/glendale_network.graphml` (Docker volume, persists across restarts)
- GraphML chosen over pickle: human-readable, stable across Python versions, natively supported by osmnx/NetworkX

**Step 2 — Restaurants:**
- Queries OSM for `amenity=restaurant` in Glendale
- Converts all geometries to centroids (some OSM entries are building outlines)
- Inserts into `robotics.restaurants` via `ST_GeomFromEWKT` with `SRID=4326`
- Caps at 500 rows

**Step 3 — Residences:**
- Queries OSM for `building=residential`
- Polygon footprints converted to centroid Points before insert
- Inserts into `robotics.residences`, caps at 2,000 rows

**Why setup.py talks directly to PostgreSQL (bypasses web-api):**  
It is a backend infrastructure script, not a user-facing operation. The web-api layer exists to serve the Flet frontend. setup.py is equivalent to a database migration runner — direct DB access is correct and standard for this role.

### 5. Map View — flet-map replacing flet.canvas
The original canvas-based view drew dots on a blank dark grid with manual Mercator projection math. Replaced with `flet-map` (`flet_map`) which wraps Flutter's `flutter_map`:

- `fmap.TileLayer` — fetches OSM raster tiles on demand from `tile.openstreetmap.org`
- `fmap.MarkerLayer` — list of `fmap.Marker` objects, each pinned to a real lat/lon
- `marker_layer` reference kept so `refresh_map()` can swap in a new markers list in place without re-fetching tiles
- Map opens centered on Glendale (34.1425, -118.2551) at zoom 13

---

## Architecture as of End of Session

```
docker compose up --build
    ├── postgres (postgis/postgis:16-3.4)       port 5432
    │     └── init-db.sql creates schema + tables on first run
    ├── web-api (Dockerfile.webapi / FastAPI)    port 8000
    │     ├── REST endpoints (polling)
    │     └── WebSocket endpoints /ws/robots/ /ws/deliveries/
    └── python-app (Dockerfile.python / Fedora 40)  port 8501
          ├── setup.py        ← OSM download + PostGIS population (blocking)
          ├── main.py         ← SimPy simulation engine (stub, background)
          └── frontend/app/main.py  ← Flet web UI (background, port 8501)
```

**Data flow:**
```
OSM (internet)
    → setup.py
        → glendale_network.graphml  (Docker volume /app/data)
        → robotics.restaurants      (PostGIS)
        → robotics.residences       (PostGIS)

SimPy engine  →  robotics.robot_locations / deliveries  (PostgreSQL)
FastAPI        →  queries PostgreSQL, streams via WebSocket
Flet UI        →  REST polls + WebSocket → renders map + tables + stats
```

---

## Files Created / Modified This Session

| File | Status | Notes |
|------|--------|-------|
| `frontend/app/main.py` | Created | Full Flet app, 5 views |
| `web_api/app/main.py` | Modified | Added WS + new REST endpoints |
| `app/setup.py` | Created | OSM download + PostGIS population |
| `start.sh` | Created | Process launcher for python-app container |
| `docker-compose.yml` | Modified | Merged flet into python-app; added port 8501, env vars, volume mounts |
| `Dockerfile.python` | Modified | Copies frontend/, start.sh; exposes 8501 |
| `requirements-python.txt` | Modified | Added flet, flet-map, httpx, websockets, osmnx, networkx |
| `Dockerfile.postgres` | Deleted | Was a mis-named duplicate of Dockerfile.python |
| `Dockerfile.flet` | Deleted | Orphaned after container merge |
| `requirements-flet.txt` | Deleted | Orphaned after container merge |

---

## Key Concepts Covered This Session

- **`docker compose up --build` vs `docker compose create`** — `create` sets up containers without starting them; `up --build` rebuilds images and starts everything. Always use `--build` after changing Dockerfiles or requirements.
- **Ports** — 80/443 are HTTP/HTTPS defaults, not requirements. Dev servers use unprivileged ports (>1023) like 8000, 8501. WebSockets upgrade from HTTP on the same port — no separate port needed.
- **WebSockets vs HTTP** — WebSocket starts as an HTTP request with `Upgrade: websocket` header, promoted to a persistent connection on the same port. FastAPI handles both on port 8000 with `@app.websocket()` routes.
- **osmnx** — downloads real-world geographic data from OpenStreetMap. Fetches road networks, POIs (restaurants, residences) by OSM tags. Returns NetworkX graphs and GeoDataFrames.
- **NetworkX** — general-purpose graph library with no geographic awareness. Receives the road network from osmnx as a graph object; runs Dijkstra's algorithm for shortest-path routing during simulation.
- **GraphML vs pickle** — GraphML is the recommended format for NetworkX graphs: human-readable XML, natively supported by osmnx, stable across Python versions. Pickle is opaque and Python-version-sensitive.
- **Map tiles** — raster images served on demand by a tile server (OSM). Not stored locally. In an offline deployment, swap the tile URL for a local TileServer GL container serving an MBTiles file.
- **flet-map vs flet.canvas** — canvas is a low-level 2D drawing surface requiring manual projection math. flet-map wraps flutter_map, handling tile fetching, zoom, pan, and lat/lon marker placement automatically.
- **setup.py as init container pattern** — backend infrastructure scripts talk directly to the DB, not through the API layer. Equivalent to Kubernetes init containers or Alembic migration runners in production.

---

## What's Not Yet Implemented (Next Steps)

1. **SimPy simulation engine** (`app/main.py`) — the actual discrete-event simulation. Robots, delivery events, NetworkX routing, real-time writes to `robotics.robot_locations` and `robotics.deliveries`.
2. **FastAPI DB queries** — all WebSocket and REST endpoints currently return stubs. Need real `psycopg2` queries against the populated tables.
3. **`robotics.simulations` table** — referenced in architecture design but not yet in `init-db.sql`. Needed to track simulation runs and support historical queries.
4. **Delivery config YAML** — pluggable delivery generator (Poisson process, peak hours, robot constraints) designed in `ARCHITECTURE_DESIGN.md` but not yet implemented.
5. **Robot agents** — NetworkX routing integrated with SimPy `timeout()` calls to simulate real travel times along Glendale streets.
