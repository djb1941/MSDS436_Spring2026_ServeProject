# Session 010 — Debugging Summary

Date: 2026-06-05

Four issues debugged and fixed this session: DuckDB export crashes, the live view
dying on second/subsequent simulations, and taxi-stand markers not rendering. Below
is the root cause and fix for each, plus the files touched.

---

## 1. DuckDB export — "Duplicate key violates primary key constraint"

**Symptom.** Every simulation's DuckDB export failed (non-fatal) with
`Constraint Error: Duplicate key "id: N" violates primary key constraint`, the
collision id climbing across runs (20 → 147 → 290).

**Root cause.** PostgreSQL (`postgres_data` volume) and the DuckDB analytics file
(`./data` bind mount) live on separate persistent volumes. When the Postgres
volume is wiped, its `SERIAL` ids restart at 1 while the DuckDB file still holds
rows from the previous database incarnation. The export's per-sim
`DELETE ... WHERE simulation_id = ?` only clears rows for the sim being
re-exported; stale rows from the prior incarnation carry different
`simulation_id`s, survive, and collide on the `INTEGER PRIMARY KEY`.

**Fix.**
- `app/analytics.py` — all 7 keyed `INSERT`s changed to `INSERT OR REPLACE`, so a
  primary-key collision overwrites the stale row instead of throwing.
- `app/analytics.py` — added `reset_store()` which drops all analytics tables.
- `app/setup.py` — added `reset_analytics_if_fresh_db()`, called after migrations;
  when `robotics.simulations` is empty (the signature of a fresh/wiped Postgres),
  it resets the DuckDB store so restarted ids can't collide with stale rows.

To clear an already-corrupted file once: stop the stack and delete
`serve/data/analytics.duckdb`.

---

## 2. Live view dead on the second (and later) simulation

**Symptom.** Sim 2+ ran and exported fine (engine logs confirmed), but the map
showed no robots and the delivery log/statuses never updated.

**Root cause.** `GET /api/v1/stats` returned the globally most-recent simulation
(`ORDER BY started_at DESC NULLS LAST LIMIT 1`). When a new sim is started its row
has `started_at = NULL`, so it sorts last and `/stats` returns the *previous*
(completed) sim. The frontend poll saw `sim_status == "completed"` and set
`is_running = False`, which made the freshly-spawned WebSocket threads break out
immediately. Nothing set `is_running` back to True, so the live feed stayed dead
for the rest of the run.

**Fix.**
- `web_api/app/main.py` — `/api/v1/stats` takes an optional `sim_id` and returns
  `simulation_id` in the payload (falls back to most-recent when omitted).
- `frontend/app/main.py` — `refresh_stats` passes `state.sim_id` and only resets
  `is_running` when the reported `simulation_id` matches the current run.
- `frontend/app/main.py` — WS threads are pinned to the sim they were spawned for
  (`my_sim_id`); they break when `state.sim_id` changes and ignore messages whose
  `simulation_id` doesn't match.
- `web_api/app/main.py` — both WS server loops (robots, deliveries) now send one
  final frame on a terminal status, then close, instead of looping forever on a
  finished run.

---

## 3. Taxi / post-stand markers not showing on the map

This took several passes; recording the full chain because each step was a real
(if partial) finding.

**Investigation path.**
1. Confirmed the backend `GET /api/v1/post_locations` returns all 5 stands
   (`curl` returned them), and the session-8 volume mount is present.
2. First suspected the marker structure: `post_stand_marker` wrapped its container
   in an `ft.Tooltip` control as `Marker.content`, unlike the working markers.
   Changed it — still no markers.
3. Routed stand rendering through `refresh_map` (the path restaurants/robots use),
   added a diagnostic `logger.info`. Log didn't show it — but discovered the
   frontend had **no `logging.basicConfig`**, so all `logger.info` was suppressed.
4. Added `logging.basicConfig(level=INFO)`. Next log confirmed INFO works (flet's
   own INFO appeared) but `load_post_stands` still never logged — proving the
   loader was **never being called**. The dropdown's `on_change` wasn't triggering
   it. The marker content was never the real problem.

**Root cause.** The taxi-stand loader was only wired to the idle-strategy
`Dropdown.on_change`, which wasn't reliably firing in this Flet version. Stands
were therefore never fetched or drawn.

**Fix.**
- `frontend/app/main.py` — stands now load as a **fallback in `start_simulation`**
  whenever `idle_strategy == "post_location"`, independent of the dropdown event.
- `frontend/app/main.py` — stands are stored in state and drawn inside
  `refresh_map` (single redraw path), so they persist across live updates.
- `frontend/app/main.py` — marker is now a plain `ft.Icon(ft.Icons.LOCAL_TAXI)`
  (matching flet-map's documented example); the map legend uses the same icon.
- `frontend/app/main.py` — added `logging.basicConfig` so frontend INFO logs are
  visible; added a diagnostic log in the idle-strategy handler.

**Outcome.** Taxi icons render on the map.

---

## Key conventions reinforced

- **All map marker updates must flow through `refresh_map()`** — it is the single
  redraw path the WebSocket loop calls. A standalone `MarkerLayer.markers = [...]`
  plus a bare `page.update()` does not reliably propagate in this Flet version.
- **Keep `logging.basicConfig(level=INFO)` in the frontend** so `logger.info`
  diagnostics actually appear in the python-app container logs.
- **Postgres `SERIAL` ids are not stable analytical keys** when the DB can be
  wiped independently of the DuckDB file; the export is now idempotent and the
  store resets with a fresh Postgres.

## Files changed

| File | Change |
|------|--------|
| `app/analytics.py` | `INSERT OR REPLACE` for all keyed tables; added `reset_store()` |
| `app/setup.py` | `reset_analytics_if_fresh_db()` after migrations |
| `web_api/app/main.py` | `/stats` optional `sim_id` + returns `simulation_id`; WS loops exit on terminal status |
| `frontend/app/main.py` | sim-scoped completion check; WS threads pinned to sim; stand loading via `refresh_map` + `start_simulation` fallback; `ft.Icon` marker + legend; `logging.basicConfig` |
