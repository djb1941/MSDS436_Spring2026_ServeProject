# Session 006 Summary — Serve Robotics Simulation

## Work Completed This Session

### 1. Polyline Alignment Fix (Road Geometry)
Robots now draw routes that follow actual street geometry rather than jumping between intersection nodes. The key insight: OSMnx stores intermediate road shape points on edge geometries (not as nodes), so `_path_waypoints()` in `robot_agent.py` was rewritten to iterate over edges and extract the full `geometry` LineString from each edge's data dictionary.

### 2. Railroad Barrier Fix (Network Type)
Switched `setup.py` from `network_type="drive"` to `network_type="all"`. The drive network doesn't include grade crossings, pedestrian paths, or service roads that connect across the BNSF/Metrolink railroad running through the simulation boundary. Robots were trapped on one side of the tracks. Also added largest-weakly-connected-component filtering to prevent `nearest_nodes()` from snapping locations to isolated subgraphs.

Also fixed a path mismatch bug: `setup.py` was saving to `serve_boundary_network.graphml` while `main.py` was loading `glendale_network.graphml`. Both now use `serve_boundary_network_all.graphml`.

### 3. Route Endpoint Projection (`_edge_stub`)
The prepend/append of raw building centroids drew straight lines through city blocks. Fixed with `_edge_stub()` in `robot_agent.py`:
- Uses `ox.nearest_edges()` to find the actual road edge the building is adjacent to
- Projects the building's point onto that edge using `shapely.ops.substring` + `line.project()`
- Extracts partial edge geometry from the building to the nearest route node

This means routes now emerge from the correct side of the building and hug the road geometry all the way from origin to destination.

### 4. House Icon Markers for Active Deliveries
Added green house markers to the map that appear only when a robot has a residence set as its current destination. Each marker shows the assigned robot's number and opens a dialog on click showing the delivery address and ETA for that robot's arrival.

### 5. Dispatcher FIFO Fix
The dispatcher's `robot_available()` method was using `_nearest_request()` — always assigning the backlogged delivery whose restaurant was closest to the robot's current position. This made it look like robots were just wandering to nearby restaurants, masking the Poisson-random order. Switched to FIFO (`self._pending.pop(0)`) so the random arrival order is preserved.

### 6. Delivery Visibility at Generation Time
Delivery rows were being written to the DB only when a robot claimed them. This meant pending orders (queued in the dispatcher, no robot yet assigned) were invisible to the dashboard count and the map's "pending" restaurant icons. Fixed by splitting the DB write:
- `delivery_generator.py` now calls `_write_pending_delivery()` immediately on generation (with `robot_id = NULL`)
- `robot_agent.py` calls `db_writer.claim_delivery(delivery_id, robot_index)` to fill in the robot assignment

Required: `ALTER COLUMN robot_id DROP NOT NULL` migration.

### 7. Max Delivery Time + Rejected Counter
Replaced `max_delivery_distance_km` in the YAML config with `max_delivery_time_minutes: 30`. The dispatcher estimates ETA (haversine × 1.4 road factor, accounting for robot queue wait) before dispatching. Deliveries that exceed 30 minutes are written to a new `rejected_deliveries` table and never dispatched. The dashboard now shows a red "Rejected" counter under the Performance section.

---

## Notes for Next Session

### 🚔 Police Station Depot Starting Position
All robots start at `DEPOT_LAT = 34.1478, DEPOT_LON = -118.2551` (Glendale PD, northeast corner of the boundary). This concentrates early coverage in the northeast and means the first few deliveries to southern residences take disproportionately long. Consider either:
- Moving the depot to a more central location within the boundary
- Distributing robots across multiple starting depots
- Accepting the warmup period but noting it in simulation output

### ⏰ Simulation Time Display
The dashboard does not currently show simulation time. Two things are needed:
1. **Time of day** — the simulation has a `start_time` in the YAML config and a `current_sim_time_s` column on `simulations`. The UI should convert this to a human-readable wall-clock time (e.g. "08:43 AM") based on `start_time + elapsed`.
2. **Time remaining** — the simulation has a `duration_hours` value. The UI should show how much sim time is left (e.g. "3h 17m remaining").

The WebSocket handler in `web_api/app/main.py` already pushes `current_sim_time_s` — the frontend just needs to render it.

### 🕐 "Last Updated" Time Appears Broken
The `build_sim_view()` function in `frontend/app/main.py` likely has two separate `last_updated = ft.Text(...)` assignments — one in the map section and one in the dashboard section. The second assignment shadows the first, so `refresh_stats()` updates the wrong reference and the displayed timestamp never changes. Fix: use `ft.Ref` consistently (or rename one of them) so both map and dashboard timestamps update correctly.
