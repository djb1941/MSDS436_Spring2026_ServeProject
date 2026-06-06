# Session 009 Summary — Serve Robotics Simulation

## Overview

This session fixed the analysis-view charts that never rendered, added a
frontend "simulation may be stalled" warning, instrumented the simulation
engine to locate a hang, and — using the diagnostics — found and fixed the
**real** cause of the `post_restaurant` infinite loop that Session 008 only
partially addressed. The Session 008 fix removed the 100%-CPU symptom but the
simulation still deadlocked (simulated time frozen); this session fixes the
underlying cause.

---

## 1. Analysis Charts Never Generated (`frontend/app/main.py`)

**Symptom:** Clicking *Update Chart* in the Analysis view produced no chart,
even for completed simulations.

**Investigation:** Ruled out the data and render paths — the matplotlib render
(`_make_chart_image`) works fine from a background thread, the analytics
queries match the Postgres `robotics.*` schema, and `matplotlib`/`duckdb` are
installed in the `python-app` container. The defect was layout.

**Root cause:** `chart_slot` was declared with **both** `expand=True` and
`height=380`, nested inside an `expand=True` column inside a
`scroll=ft.ScrollMode.AUTO` (unbounded-height) parent. A flex child under
unbounded vertical constraints can't be laid out in Flutter/Flet, so the chart
subtree silently failed to render even though the PNG was generated. This is the
same `expand`+`height` conflict Session 008 flagged. The working live map in the
sim view uses `height` only (no `expand`) — the correct pattern.

**Fix:** Removed `expand=True` from `chart_slot`, keeping the fixed `height=380`
so it matches the working map container.

---

## 2. Stalled-Simulation Warning (`frontend/app/main.py`)

Added a visible warning so a backend hang surfaces in seconds instead of looking
like a silent freeze.

- New `stall_warning` text control placed in the map header row.
- Detection lives in `refresh_stats`: while a run is active and not finishing,
  if `sim_progress_pct` doesn't advance for `STALL_SECONDS` (30 s) it shows
  `⚠ Simulation may be stalled — no progress for Ns (stuck at X%)`.
- **Time-based, not poll-count-based**, because `refresh_stats` fires both from
  the 5-second REST poll and from every WebSocket delivery push; a counter would
  be inconsistent. The clock resets the moment progress moves or the run ends.

This is a symptom detector, not a fix — but it's what made the hang obvious.

---

## 3. Engine Instrumentation to Locate the Hang

Added three layered diagnostics (kept as permanent safety nets):

| Diagnostic | Location | Purpose |
|---|---|---|
| **Hang watchdog** | `app/main.py`, `run_simulation` | Daemon thread samples `env.now` every 2 s. If sim time is stuck for 15 wall-seconds, logs a dispatcher/robot state snapshot, then `faulthandler.dump_traceback(all_threads=True)` to show the exact spinning line. The main thread is stuck inside `env.run()` during a hang, so it can't report on itself — the watchdog dumps from outside. |
| **Zero-time loop guard** | `app/robot_agent.py`, top of `run()` | Counts loop iterations at the same `env.now`. 50 iterations without time advancing ⇒ a non-yielding/zero-delay reposition cycle. Logs full robot context and **raises**, converting a silent freeze into a clean, traceable failure (caught by the poller, marks the sim failed). |
| *(removed after diagnosis)* `ZERO-DIST REPOSITION` warning | `app/dispatcher.py` | Was coordinate-based; superseded by the node-aware fix below. |

---

## 4. The `post_restaurant` Hang — Real Root Cause and Fix

### What the logs showed (`debug_logs/20260605_0807.rtf`)

- `ZERO-TIME LOOP: robot 3 looped 50 times at t=0.7772 without advancing sim
  time. pos=(34.147375,-118.255685) node=11536116605`.
- Watchdog snapshot: robots **2 and 3 both on graph node `11536116605`** despite
  having different coordinates.
- The `ZERO-DIST REPOSITION` warning **never fired** — i.e. the target
  restaurant was *not* coordinate-close to the robot (tens of meters away), yet
  it still routed in zero time.

### Root cause

The dispatcher excluded repositioning candidates by **lat/lon coordinate**, but
travel time is computed on the road **graph by node**. Multiple restaurants (and
the robot's own spot) can have distinct coordinates yet snap to the **same OSM
node** via `ox.nearest_nodes()`. Sending a robot to a restaurant on its current
node makes `_route_info(node, node)` return zero, so `_reposition` yields a
zero-delay `env.timeout(0)`.

A zero-delay timeout *is* a yield (so Session 008's "always yield" change stopped
the CPU spin) **but it does not advance simulated time**. The run loop therefore
cycles forever at the same `env.now`, the SimPy clock freezes, and the delivery
generator / sim clock never get to run. The hang simply changed shape from
"100% CPU" to "frozen clock."

### Fix (node-aware repositioning)

- **`app/main.py`:** after loading post restaurants (and post locations),
  precompute each one's road-graph node (`loc.node = ox.nearest_nodes(...)`,
  per-item scalar call) so the dispatcher can reason about nodes.
- **`app/dispatcher.py` — `_nearest_unoccupied_restaurant`:** rewritten to two
  rules:
  1. If the robot is already standing on a post-restaurant node, it is already
     optimally posted → return `None` (stay and wait for work). This is the
     intended "post and wait" behavior and prevents the zero-distance loop.
  2. Otherwise pick the nearest restaurant whose node is neither the robot's own
     node nor occupied by another idle robot — guaranteeing a real,
     time-advancing move.
- **`_nearest_unoccupied_post_location`:** same node-aware logic applied so the
  identical bug isn't left in the `post_location` strategy.

This also fixes a behavioral subtlety: an earlier draft that merely excluded the
robot's own node (without rule 1) would have made robots hop between restaurants
forever (time-advancing but wrong). Rule 1 makes them post once and wait.

---

## Files Changed This Session

| File | Change |
|---|---|
| `frontend/app/main.py` | Removed `expand=True` from `chart_slot` (chart render fix); added `stall_warning` control + time-based stall detection in `refresh_stats` |
| `app/main.py` | Hang watchdog (faulthandler all-thread dump + state snapshot) in `run_simulation`; precompute graph nodes for post restaurants and post locations |
| `app/robot_agent.py` | Zero-time loop guard at top of `run()` (logs + raises on a frozen-clock spin) |
| `app/dispatcher.py` | Node-aware repositioning in `_nearest_unoccupied_restaurant` and `_nearest_unoccupied_post_location` ("stay if already on a post node", else nearest free-node post) |

---

## Verification Status

- All four files compile (`py_compile`).
- Matplotlib render-in-thread path verified in a sandbox.
- **Not yet run end-to-end** — no `osmnx`/Postgres in the dev sandbox. Needs a
  live run: start with `post_restaurant`, confirm the clock advances past
  `t≈0.78` and deliveries complete, and confirm robots post-and-stay rather than
  hop. Capture `docker compose logs python-app` to verify.

---

## Remaining Known Issues / Notes

- **Loop-guard behavior** — currently `raise`s (fails the run loudly). Could be
  softened to log-and-park if graceful degradation is preferred over fail-loud.
- **Post assignment clustering** — robots reposition to the nearest free-node
  restaurant by straight-line distance; several can still cluster. Spreading them
  for coverage is an optimization, not a bug.
- Carried over from Session 008: sim-time clock display, line-chart X-axis label
  ticks, `last_updated` timestamp shadowing, and depot location at Glendale PD.
