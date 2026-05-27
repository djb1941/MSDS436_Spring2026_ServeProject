# Session 005 Summary — Model Details, Map Features & Robot Movement
**Date:** 2026-05-26  
**Project:** Serve Robotics Simulation (MSDS436 Term Project)

---

## What We Did This Session

Five distinct areas of work: robot speed modeling, a realistic simulation boundary,
map overlay and restaurant markers, a multi-iteration fix for robot position updates,
and a lat/lon coordinate bug introduced during manual edits.

---

## 1. Robot Speed Modeling

### Problem
`setup.py` downloaded the OSM road network with `network_type="drive"` and called
`ox.add_edge_speeds()`, which stamps **car speeds** (25–45 mph in Glendale) onto
every edge. `robot_agent.py` routed by `weight="travel_time"` and used those
car-derived times directly. A 2 km leg that should take ~20 minutes at 6 km/h was
completing in ~3 minutes — a 7× error that made the queuing dynamics meaningless.

### Fix
- Added `robot_speed_kmh: 6.0` to `app/delivery_configs/default.yaml` under
  `constraints` — one place to tune it per config.
- `robot_agent.py`: added `DEFAULT_ROBOT_SPEED_KMH = 6.0` constant and a
  `speed_kmh` constructor parameter (defaults to `DEFAULT_ROBOT_SPEED_KMH` so
  missing config doesn't crash). Stores `self._speed_ms = speed_kmh * 1000 / 3600`.
- `_route_info()`: changed routing weight from `"travel_time"` to `"length"`. At
  constant speed, shortest distance = shortest time, so the chosen path is still
  optimal. Travel time is now computed as `distance_m / self._speed_ms`.
- `app/main.py`: reads `robot_speed_kmh` from YAML config and passes it into each
  `RobotAgent` constructor. Falls back to `6.0` if the key is absent.

**Key concept:** At constant speed, routing by `length` gives the same path as
routing by `time`. We just needed to stop letting OSMnx's car-speed assumptions
influence our simulation timings.

---

## 2. Custom Simulation Boundary

### Change
Replaced the Glendale city administrative boundary (`ox.graph_from_place`) with a
custom polygon traced along three real geographic features:

| Edge | Feature |
|------|---------|
| North | CA-134 (Ventura Freeway), east-west |
| East  | CA-2 (Glendale Freeway), south from the 134 interchange |
| West  | Los Angeles River, south through the Glendale Narrows |

The triangle opens **downward** — two corners on CA-134, southern vertex where CA-2
and the LA River converge near Atwater Village.

### Files changed
- `setup.py`: imported `ShapelyPolygon`, defined `BOUNDARY_COORDS` (17 vertices,
  lon/lat for Shapely) and `SIMULATION_BOUNDARY`. Replaced all three OSMnx calls:
  - `graph_from_place` → `graph_from_polygon(SIMULATION_BOUNDARY)`
  - `features_from_place` (×2) → `features_from_polygon(SIMULATION_BOUNDARY)`
- Renamed cached file from `glendale_network.graphml` →
  `serve_boundary_network.graphml` so the old graph isn't reused.
- `init-db.sql`: no changes needed (PostGIS schema is boundary-agnostic).

### Operational note
The GraphML is cached on the Docker volume. Changing `BOUNDARY_COORDS` does NOT
trigger a re-download. To force one:
```bash
docker compose exec python-app rm /app/data/serve_boundary_network.graphml
docker compose restart python-app
# or full teardown:
docker compose down -v && docker compose up --build
```

---

## 3. Map Boundary Overlay

Added a `fmap.PolygonLayer` to the Live Map that draws the simulation boundary as a
teal-outlined polygon with a faint (~10%) teal fill.

### Layer order (bottom to top)
1. `TileLayer` — OSM base map
2. `boundary_polygon_layer` — simulation zone fill + outline
3. `restaurant_layer` — restaurant pins
4. `polyline_layer` — active robot routes
5. `marker_layer` — robot position dots

### Coordinate rule
`setup.py` stores `BOUNDARY_COORDS` as `(lon, lat)` — Shapely's x/y convention.
`fmap.MapLatitudeLongitude` takes `(lat, lon)` — the human convention. **They are
always in opposite order.** When copying coordinates between the two files, the pair
must be swapped.

### Bug fixed this session
After manually updating `BOUNDARY_POINTS` in the frontend, the overlay disappeared.
Cause: coordinates were pasted as `(lon, lat)` directly from `setup.py` without
swapping, placing every vertex near Antarctica. Fixed by reversing each pair.

---

## 4. Restaurant Map Markers

### New API endpoint
`GET /api/v1/restaurants` — returns all restaurants with `id`, `name`, `address`,
`lat`, `lon` from PostGIS. Called once on view load (static data).

### WebSocket extension
The robot WebSocket (`/ws/robots/{sim_id}`) now also queries restaurants with
pending deliveries (`picked_up_at IS NULL AND delivered_at IS NULL`, grouped by
`restaurant_id`) and includes `busy_restaurants: [{restaurant_id, pending_count}]`
in each push.

### Frontend markers
- Fetched once on view init in a background thread; stored in `state.restaurants`.
- `busy_restaurants` dict (`id → count`) updated each WS robot push.
- `refresh_map()` rebuilds `restaurant_layer` on every WS tick so busy colors stay live.
- **Gray square** = no active order; **orange square** = has pending request.
- Clicking a marker opens an `ft.AlertDialog` showing name, address, and pending
  delivery count (or "No active requests").
- Legend updated with both restaurant states.

---

## 5. Robot Position Updates — Three Iterations

This was the most conceptually interesting work of the session.

### Original bug
`robot_agent.py` called `yield self.env.timeout(leg_time / 60)` — a single SimPy
sleep for the entire leg. The DB only received a position write at the *start* and
*end* of each leg, so robots appeared to teleport between restaurant and residence.

### Iteration 1 — Node-by-node (discarded)
Added `_travel_leg()`: iterated through OSM path nodes, yielded one `env.timeout`
per edge, logged position at each intermediate node.

**Problem raised:** OSM edges can be hundreds of metres long on straight arterials.
A single edge with no intermediate nodes would still freeze the robot for the full
edge travel time.

### Iteration 2 — Sim-side interpolation (discarded)
Subdivided each edge into fixed `POSITION_UPDATE_INTERVAL_S = 10` second steps,
interpolated lat/lon within the edge at each step, logged to DB.

**Problem raised:** The robots already have a live `self.current_lat/lon` as they
travel. Why write hundreds of DB rows when the API could compute the same position
from math?

### Iteration 3 — WebSocket-side interpolation (final)

**Architecture:** The sim engine is still the only source of truth for robot
*state*, but the WebSocket computes robot *position* on the fly.

**How it works:**
1. When a leg starts, `update_route()` now stores `leg_started_at_sim_s` (the sim
   clock in seconds at leg start) in `active_routes`.
2. A new `sim_clock` SimPy process in `app/main.py` writes
   `simulations.current_sim_time_s` to the DB every 1 sim-minute. This is a single
   `UPDATE` on one row — very cheap.
3. The WebSocket queries both values alongside the existing robot status and route
   waypoints.
4. For each robot with `status = "traveling"`, it computes:
   ```
   elapsed_s  = current_sim_time_s − leg_started_at_sim_s
   distance_m = elapsed_s × ROBOT_SPEED_MS
   ```
   Then calls `interpolate_along_route(route_coords, distance_m)` — an
   equirectangular walk along the stored waypoints — to get the current `(lat, lon)`.
5. That computed position overrides the DB position in the WS payload.

**Why this is better:** The sim engine writes positions only on status changes
(same as the original). No continuous inserts. The interpolation logic lives where
it's used (the API), not scattered through the sim engine.

**Limitation:** Meaningful only in real-time mode. In fast-forward the sim races
ahead of the WS clock and smooth animation isn't the goal anyway.

### Schema changes
| Table | Column added |
|-------|-------------|
| `robotics.active_routes` | `leg_started_at_sim_s FLOAT` |
| `robotics.simulations`   | `current_sim_time_s FLOAT DEFAULT 0` |

Added to both `init-db.sql` (for fresh volumes) and `setup.py` `apply_migrations`
(for existing volumes).

---

## Key Concepts Covered This Session

- **OSMnx edge weights** — `add_edge_speeds()` and `add_edge_travel_times()` use
  OSM `maxspeed` tags (car speeds). Ignoring them and computing travel time from
  a fixed robot speed gives correct simulation timings.
- **`graph_from_polygon` vs `graph_from_place`** — OSMnx can clip the road network
  to any Shapely polygon, not just administrative boundaries.
- **Docker volume persistence** — `docker compose down` keeps volumes; `down -v`
  removes them. Cached files (GraphML) survive rebuilds unless the volume is cleared.
- **Shapely vs flet-map coordinate order** — Shapely uses `(lon, lat)` (x, y);
  `fmap.MapLatitudeLongitude` uses `(lat, lon)`. Always swap when copying.
- **SimPy `yield from`** — delegates a generator's yields to the parent SimPy
  process. Useful for sub-generators that produce SimPy events.
- **DB-only inter-container communication** — the sim engine and web API share no
  memory. All live state must go through PostgreSQL; the WS interpolation approach
  minimises how much that costs.
- **Equirectangular approximation** — for city-scale distances (< a few km),
  converting lat/lon degree differences to metres with `111,320 × cos(lat)` is
  accurate to < 0.1% and requires no trig beyond one `cos()` call.

---

## Files Modified This Session

| File | Changes |
|------|---------|
| `app/delivery_configs/default.yaml` | Added `robot_speed_kmh: 6.0` |
| `app/robot_agent.py` | Robot speed parameter; route by length; removed `_travel_leg`; passes `leg_started_at_sim_s` to `update_route()` |
| `app/main.py` | Reads `robot_speed_kmh` from config; added `sim_clock` SimPy process |
| `app/db_writer.py` | `update_route()` accepts `leg_started_at_sim_s`; added `update_sim_time()` |
| `app/setup.py` | Custom boundary polygon; `graph/features_from_polygon`; renamed GraphML; migrations for new columns |
| `init-db.sql` | `leg_started_at_sim_s` on `active_routes`; `current_sim_time_s` on `simulations` |
| `web_api/app/main.py` | `GET /api/v1/restaurants`; busy restaurants in WS; `interpolate_along_route()`; WS-side position interpolation |
| `frontend/app/main.py` | Boundary polygon overlay; restaurant markers + click dialog; `state.busy_restaurants`; corrected `BOUNDARY_POINTS` coordinate order; updated map center |

---

## Next Steps

- End-to-end test with `docker compose down -v && docker compose up --build`
- Verify boundary polygon renders correctly over the new coordinates
- Verify restaurant markers appear and toggle orange during a simulation run
- Verify robots move smoothly along routes in real-time mode
- Consider adding a `rush_hour.yaml` config to test peak-hour delivery dynamics
