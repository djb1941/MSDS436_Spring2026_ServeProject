# Session 008 Summary — Serve Robotics Simulation

## Overview

This session focused on completing the analysis view, resolving three separate UI bugs, adding a startup loading overlay, and tracking down and fixing two interrelated bugs in the `post_restaurant` idle strategy that caused simulations to lock up permanently.

---

## 1. Analysis View Redesign (`frontend/app/main.py`)

Replaced the multi-chart grid layout with a panel + chart design:

- **Left panel (280 px):** Three sections stacked vertically — Simulations (checkboxes), X Axis (radio buttons: Sim Hour / Compare Simulations / By Robot), Metrics / Y Axis (checkboxes that change based on X selection). Update Chart button triggers export + query + render in a background thread.
- **Right area:** Single `chart_slot` container whose content is replaced on each Update Chart click, backed by a `chart_title` text reference above it.
- **Bottom:** Sortable `ft.DataTable` summary statistics.

### Flet API Fixes Applied During Rebuild

Several Flet controls used parameters that don't exist in the installed version:

| Broken usage | Fix |
|---|---|
| `ft.ChartAxis(title=ft.Text(...))` | Removed — `title` is not a valid param; axis labels use `labels_size` only |
| `ft.LineChart(expand=True, height=360)` | Removed `expand=True` — setting both conflicts; explicit `height` wins |
| `ft.BarChart(expand=True, ...)` | Same fix |
| `data_row_color={ft.MaterialState.HOVERED: BG_SURFACE}` | Changed to `{"hovered": BG_SURFACE}` — string key is the compatible form |
| `ft.Container(min_height=400)` | Changed to `height=400` — `min_height` is not a valid `Container` param |
| `ft.ChartGridLines(...)` | Removed entirely — class doesn't exist in this Flet version |

### Chart Container Overflow Fix

The old layout put a title `ft.Text` and a `ft.LineChart(height=360)` both inside a `ft.Container(height=400, padding=20)`. With 20 px top + 20 px bottom padding that leaves only 360 px of usable space, but the title text adds another ~20 px, overflowing the container. Fixed by:
- Splitting into `chart_title` (standalone `ft.Text` above) and `chart_slot` (container with only the chart)
- Using `ft.Padding(left=12, right=12, top=8, bottom=8)` on `chart_slot` instead of uniform 20 px

### Pruned `build_analysis_view_OLD`

Deleted the ~600-line old implementation (renamed to `_OLD` last session as a reference), removed via `sed -i '942,1546d'`.

---

## 2. Three UI Bugs Fixed

### Bug A — Start/Stop Buttons Don't Reset After Sim Completes

**Root cause:** `_start_ws_threads` is a module-level function but references `start_btn` and `stop_btn`, which are local variables inside `build_sim_view`. When the WebSocket delivery feed received a terminal `sim_status` (`"completed"` or `"failed"`), it attempted:

```python
start_btn.disabled = False   # ← NameError: name 'start_btn' is not defined
stop_btn.disabled  = True
page.update()
```

Python raised `NameError` because `start_btn` doesn't exist in the module's global scope. This exception was caught silently by `except Exception: pass`, so:
- `state.is_running` was set to `False` (line executed before the NameError)
- But `start_btn.disabled` was never updated and `page.update()` was never called
- Buttons stayed permanently stuck (Start disabled, Stop enabled)

**Fix:**
1. Added `start_btn` and `stop_btn` fields to `AppState.__init__` (initially `None`)
2. In `build_sim_view`, assigned them immediately after creation: `state.start_btn = start_btn; state.stop_btn = stop_btn`
3. In `_start_ws_threads`, replaced direct references with `state.start_btn`/`state.stop_btn`:

```python
if ws_sim_status in ("completed", "failed", "stopped") and state.is_running:
    state.is_running = False
    if state.start_btn:
        state.start_btn.disabled = False
        state.stop_btn.disabled  = True
    page.update()
```

4. Added a backup reset path inside `refresh_stats` (the 5-second REST poll) so buttons reset even if the WS delivery thread misses the terminal push:

```python
if sim_status in ("completed", "failed", "stopped") and state.is_running:
    state.is_running   = False
    start_btn.disabled = False
    stop_btn.disabled  = True
```

### Bug B — Post/Taxi Stand Markers Not Appearing on Map

**Root cause:** The `GET /api/v1/post_locations` endpoint in `web_api/app/main.py` uses:
```python
CONFIGS_DIR = "/app/delivery_configs"
```
But the web-api container only mounts `./web_api:/app`. The delivery configs live at `./app/delivery_configs/` on the host — which maps to `/app/delivery_configs/` inside the **python-app** container, not the web-api container. So the file path never exists inside web-api, the endpoint catches the `FileNotFoundError`, and returns `{"post_locations": []}` every time.

**Fix:** Added a read-only volume mount to the web-api service in `docker-compose.yml`:
```yaml
volumes:
  - ./web_api:/app
  - ./app/delivery_configs:/app/delivery_configs:ro   # ← added
```
No container rebuild required — `docker compose up` picks up volume changes.

### Bug C — `"stopped"` Status Not Resetting Buttons

The WS handler only checked `("completed", "failed")` for terminal statuses — missing `"stopped"`. Added `"stopped"` to both the WS handler and the `refresh_stats` fallback.

---

## 3. Startup Loading Overlay

**Problem:** `start.sh` ran `setup.py` (which downloads the OSM road network and seeds 100+ restaurants/residences) as a blocking step *before* starting Flet. Users navigating to `http://localhost:8501` during this phase got a connection-refused error for several minutes.

**Fix — `app/start.sh`:** Moved Flet startup before `setup.py`:
```bash
# Start Flet immediately (shows loading screen)
python3 /app/frontend/app/main.py &
FLET_PID=$!

# Blocking setup (downloads maps, loads DB)
python3 /app/setup.py

# Start sim engine after setup completes
python3 /app/main.py &
SIM_PID=$!
```

**Fix — `frontend/app/main.py`:** Added a full-screen loading overlay in `main()` that:
- Appears immediately over the UI on page load
- Polls `GET /api/v1/restaurants` every 3 seconds
- Shows one of three status messages:
  - **"Waiting for services to start…"** — web-api not yet reachable
  - **"Setting up simulation environment… Downloading road network and loading location data. This takes a few minutes on first run."** — API is up but the restaurants table is empty (setup.py still running)
  - *(dismissed)* — restaurants list is non-empty, setup complete
- Uses `page.overlay.append(loading_overlay)` so the underlying UI renders behind it during loading

---

## 4. `post_restaurant` Infinite Loop — Full Diagnosis and Fix

This is the most complex bug fixed in this session. It required two separate fixes, one for the delivery rejection cycle and one for the zero-distance repositioning loop.

### Background: How `post_restaurant` Works

When the `post_restaurant` idle strategy is active, robots that finish a delivery don't sit at the drop-off location. Instead, the dispatcher sends them to the nearest unoccupied restaurant so they're pre-positioned to reduce the first-leg travel time on the next delivery.

The lifecycle:
1. Robot finishes delivery → calls `dispatcher.robot_available(self)`
2. No pending work → dispatcher finds nearest unoccupied restaurant, puts an `IdlePostCommand` in the robot's inbox
3. Robot receives command → calls `_reposition(cmd)` → travels to restaurant
4. Robot arrives → loops back to top → calls `robot_available(self)` again

### Fix Part 1 — Delivery Rejection During Repositioning

**The problem:** When all robots are simultaneously repositioning (e.g., at sim start), `_idle_robots` is empty. The delivery generator calls `dispatcher.estimate_delivery_eta()` to decide whether to accept or reject an order:

```python
if self._idle_robots:
    # Fast path: use nearest idle robot
    robot_to_rest = travel_min(best.current_lat, ...)
    robot_wait    = 0.0
else:
    # Slow path: assume one full delivery cycle per queued delivery
    cycles     = 1 + len(self._pending)
    robot_wait = cycles * (rest_to_res + pickup_minutes)
```

With no idle robots, the ETA formula becomes: `robot_wait + 0 + pickup + rest_to_res = 2 × rest_to_res + 2`. For any delivery with a restaurant→residence travel time > 14 minutes, the estimated ETA exceeds `max_delivery_time_minutes: 30` and the delivery is **rejected**. During the repositioning warmup phase, this means every generated delivery goes to `rejected_deliveries`, `_pending` stays empty, and when robots finish repositioning there's nothing to pick up — so they immediately reposition again.

**Fix:** Added a third branch to `estimate_delivery_eta` in `dispatcher.py`:

```python
elif self._claimed_posts and not self._pending:
    # All robots are repositioning (not delivering), no deliveries queued yet.
    # Use the nearest claimed post destination as the robot's effective start
    # point — much more accurate than treating this as a "busy delivering" case.
    best_lat, best_lon = min(
        self._claimed_posts.values(),
        key=lambda c: _euclidean(c[0], c[1], restaurant_lat, restaurant_lon),
    )
    robot_to_rest = travel_min(best_lat, best_lon, restaurant_lat, restaurant_lon)
    robot_wait    = 0.0
```

This correctly reflects that robots will be available as soon as they arrive at their posts, producing realistic ETAs and allowing deliveries to queue normally during the warmup window.

### Fix Part 2 — Zero-Distance Repositioning Loop

Even with Fix Part 1, the sim still locked. Here's the precise trace:

**Step-by-step execution when a robot arrives at restaurant R1:**

```
Robot finishes _reposition() to R1
  → run() loops back to top
  → log_movement(status="idle")
  → clear_route()
  → dispatcher.robot_available(self)
      → _claimed_posts.pop(robot.index)     # R1 claim RELEASED
      → _pending is empty
      → _idle_post_command(robot) called
          → _nearest_unoccupied_restaurant(robot) called
              → _occupied_coords(robot)
                  # checks _idle_robots (other idle robots' positions)
                  # checks _claimed_posts (other robots' claims)
                  # does NOT include requesting robot's own current position
              → occupied = {R2, R3, R4, R5}   # other robots' claims
              → candidates = [R1]              # R1 not in occupied!
              → min(candidates, key=distance)  # distance to R1 = 0
              → returns IdlePostCommand(R1)
      → _claimed_posts[robot.index] = R1
      → robot.inbox.put(IdlePostCommand(R1))
  → item = yield self.inbox.get()    # item already there, returns immediately
  → isinstance(item, IdlePostCommand) = True
  → yield from self._reposition(IdlePostCommand(R1))
      → dest_node = ox.nearest_nodes(G, R1.lon, R1.lat)
      → leg_time, leg_dist, leg_path = _route_info(R1_node, R1_node)
          → origin_node == dest_node → return (0.0, 0.0, [])
      → if leg_path:  → False, skip route update
      → log_movement(status="repositioning")
      → if leg_time > 0:  → False, NO YIELD
      → current_lat/lon unchanged (already there)
      → generator returns without ever yielding
  → continue
[LOOP REPEATS IMMEDIATELY]
```

**Why this is catastrophic in SimPy:** SimPy is a cooperative multitasking framework. Processes only allow other processes to run when they `yield`. The `_reposition()` call with zero travel time produces a generator that *never yields*. `yield from self._reposition(item)` therefore runs synchronously and returns instantly. The robot process loops back and calls `dispatcher.robot_available()` again — which again puts R1 in the inbox — which again starts a zero-yield `_reposition()` — infinitely, with no SimPy events being processed. The delivery generator never runs, the sim clock never advances, and the Python process pegs the CPU at 100%.

**The fix:** `_nearest_unoccupied_restaurant` now adds the requesting robot's own current position to the `occupied` set before filtering candidates:

```python
robot_pos = (round(robot.current_lat, 6), round(robot.current_lon, 6))
occupied  = self._occupied_coords(robot)
occupied.add(robot_pos)   # ← prevents zero-distance self-repositioning

candidates = [
    r for r in self._post_restaurants
    if (round(r.lat, 6), round(r.lon, 6)) not in occupied
]
if not candidates:
    # All restaurants occupied including robot's own spot.
    # Return None → robot goes to _idle_robots, waits for a real delivery.
    return None
```

When `candidates` is empty (all restaurants occupied, including the robot's own spot), `_idle_post_command` returns `None`. The `robot_available` else-branch then executes:
```python
else:
    # "stay" or no suitable location — park in-place
    self._idle_robots.append(robot)
```

The robot enters `_idle_robots` and waits at the restaurant. The next delivery request from the generator will be immediately dispatched to this robot (it's already pre-positioned near restaurants). This is the correct behavior.

**The same fix was applied to `_nearest_unoccupied_post_location`** for `post_location` strategy — identical logic, same potential for zero-distance loops.

**Note on the earlier partial fix:** Claude Code had previously added logic to `_nearest_unoccupied_restaurant` to exclude the robot's own position from the *fallback* candidates list (when all other restaurants were claimed). This only caught the case where the initial `candidates` list was empty — meaning it only worked when there were fewer free restaurants than robots. With 5 robots and 5 restaurants and each robot claiming one, the initial `candidates` always contained R1 (the robot's own unclaimed spot) so the fallback never triggered.

---

## Files Changed This Session

| File | Change |
|---|---|
| `frontend/app/main.py` | Analysis view layout redesign; Flet API compatibility fixes; button reset via `state.start_btn`/`state.stop_btn`; `"stopped"` added to terminal statuses; loading overlay in `main()` |
| `app/start.sh` | Flet now starts before `setup.py` |
| `docker-compose.yml` | Added `./app/delivery_configs:/app/delivery_configs:ro` to web-api volumes |
| `app/dispatcher.py` | ETA estimation fix for repositioning phase; `_nearest_unoccupied_restaurant` includes robot's own position in occupied set; same fix for `_nearest_unoccupied_post_location` |

---

## Remaining Known Issues

- **Sim time display on dashboard** — `current_sim_time_s` is pushed via WS but not rendered as a clock / "time remaining" in the UI.
- **Line chart X-axis labels** — Auto-scaled, may show decimal hour numbers. Explicit `labels=[...]` on `bottom_axis` would give cleaner hourly ticks.
- **Last Updated timestamp shadowing** — Two `last_updated` variables in `build_sim_view`; the map section's reference is shadowed by the dashboard section's, so the map timestamp never updates.
- **Robot depot location** — All robots spawn at Glendale PD (34.1478, -118.2551, northeast corner). Early deliveries to southern residences are slower due to initial travel from the northeast. Consider a more central depot or distributed starting positions.
