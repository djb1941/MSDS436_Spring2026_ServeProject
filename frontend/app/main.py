"""
Serve Robotics Simulation — Flet Frontend
==========================================

Runs in web mode inside the Docker container (port 8501).
Access it at: http://localhost:8501

Architecture recap
------------------
- REST polling  →  config loading, start/stop controls, final results
- WebSockets    →  real-time robot positions + delivery status during a run

The app is split into four views, all living in this single file for clarity:

  1. Simulation Controls   — configure and launch a sim run
  2. Dashboard / Stats     — live counters and summary cards
  3. Robot Status Table    — per-robot live status rows
  4. Delivery Log          — scrollable feed of in-flight and completed deliveries

Navigation is handled by a left sidebar.
"""

import asyncio
import datetime
import json
import os
import threading

import flet as ft
import flet_map as fmap
import httpx
import websockets


# ---------------------------------------------------------------------------
# Configuration — resolved from Docker environment variables set in
# docker-compose.yml, with localhost fallbacks for running outside Docker.
# ---------------------------------------------------------------------------

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
WS_BASE  = os.getenv("WS_BASE_URL",  "ws://localhost:8000")


# ---------------------------------------------------------------------------
# Colour palette (Serve-inspired teal + dark theme)
# ---------------------------------------------------------------------------

TEAL       = "#00C8AA"
TEAL_DARK  = "#009E87"
BG_DARK    = "#1A1A2E"
BG_CARD    = "#16213E"
BG_SURFACE = "#0F3460"
TEXT_MAIN  = "#E0E0E0"
TEXT_DIM   = "#888888"
RED        = "#E74C3C"
ORANGE     = "#F39C12"
GREEN      = "#2ECC71"

# ---------------------------------------------------------------------------
# Shared app state — a plain Python object passed into each view builder.
# Flet controls store references to the widgets they want to mutate; we keep
# those references here so the WebSocket callbacks (running in threads) can
# reach them and call page.update().
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self):
        self.sim_id: int | None = None
        self.is_running: bool = False
        self.ws_robots_task: asyncio.Task | None = None
        self.ws_deliveries_task: asyncio.Task | None = None

        # Live data caches updated by WebSocket callbacks
        self.robots: list[dict] = []
        self.delivery_summary: dict = {"total": 0, "completed": 0, "pending": 0}
        self.recent_deliveries: list[dict] = []

        # Callbacks registered by view builders so WS threads can trigger redraws
        self.on_robot_update    = None   # callable()
        self.on_delivery_update = None   # callable()


# ===========================================================================
# VIEW 1 — Simulation Controls
# ===========================================================================

def build_controls_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Form for configuring and launching a simulation run.

    REST endpoints used:
        POST /api/v1/simulations/start   — launch
        POST /api/v1/simulations/{id}/stop — stop
        GET  /api/v1/simulations         — list past runs
    """

    # --- Form fields --------------------------------------------------------

    config_name = ft.TextField(
        label="Config name",
        value="rush_hour",
        hint_text="Name of the delivery config (e.g. rush_hour)",
        color=TEXT_MAIN,
        bgcolor=BG_SURFACE,
        border_color=TEAL,
        focused_border_color=TEAL,
        width=300,
    )

    num_robots = ft.TextField(
        label="Number of robots",
        value="5",
        keyboard_type=ft.KeyboardType.NUMBER,
        color=TEXT_MAIN,
        bgcolor=BG_SURFACE,
        border_color=TEAL,
        focused_border_color=TEAL,
        width=300,
    )

    duration_hours = ft.TextField(
        label="Simulation duration (hours)",
        value="24",
        keyboard_type=ft.KeyboardType.NUMBER,
        color=TEXT_MAIN,
        bgcolor=BG_SURFACE,
        border_color=TEAL,
        focused_border_color=TEAL,
        width=300,
    )

    real_time_switch = ft.Switch(
        label="Real-time mode",
        value=False,
        active_color=TEAL,
    )

    speed_factor = ft.TextField(
        label="Sim speed (minutes/sec)",
        value="1",
        hint_text="e.g. 1 = 1 sim-min per sec, 10 = faster",
        keyboard_type=ft.KeyboardType.NUMBER,
        color=TEXT_MAIN,
        bgcolor=BG_SURFACE,
        border_color=TEAL,
        focused_border_color=TEAL,
        width=300,
        disabled=True,   # only active when real-time mode is on
    )

    def on_realtime_toggle(e):
        speed_factor.disabled = not real_time_switch.value
        page.update()

    real_time_switch.on_change = on_realtime_toggle

    status_text = ft.Text("", color=TEXT_DIM, size=13, italic=True)

    # --- Past simulations list (polled on load) ------------------------------

    past_sims_list = ft.Column(spacing=4)

    def load_past_simulations():
        """Poll GET /api/v1/simulations and populate the past runs panel."""
        try:
            resp = httpx.get(f"{API_BASE}/api/v1/simulations", timeout=5)
            sims = resp.json().get("simulations", [])
            past_sims_list.controls.clear()
            if not sims:
                past_sims_list.controls.append(
                    ft.Text("No past simulations yet.", color=TEXT_DIM, size=12)
                )
            else:
                for s in sims:
                    past_sims_list.controls.append(
                        ft.Text(
                            f"  #{s['id']}  {s.get('config_name','?')}  "
                            f"— {s.get('status','?')}",
                            color=TEXT_MAIN, size=12,
                        )
                    )
            page.update()
        except Exception as e:
            past_sims_list.controls.append(
                ft.Text(f"Could not load simulations: {e}", color=RED, size=12)
            )
            page.update()

    # --- Start / Stop logic --------------------------------------------------

    start_btn = ft.ElevatedButton(
        "▶  Start Simulation",
        bgcolor=TEAL, color="#000000",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
    )
    stop_btn = ft.ElevatedButton(
        "■  Stop Simulation",
        bgcolor=RED, color="#FFFFFF",
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        disabled=True,
    )

    def start_simulation(e):
        payload = {
            "config_name":  config_name.value.strip() or "default",
            "num_robots":   int(num_robots.value or 5),
            "duration_hours": float(duration_hours.value or 24),
            "real_time":    real_time_switch.value,
            "speed_factor": float(speed_factor.value or 1),
        }
        try:
            resp = httpx.post(f"{API_BASE}/api/v1/simulations/start",
                              json=payload, timeout=10)
            data = resp.json()
            state.sim_id = data["simulation_id"]
            state.is_running = True
            status_text.value = (
                f"✓  Simulation #{state.sim_id} started "
                f"({payload['num_robots']} robots, {payload['duration_hours']}h)"
            )
            status_text.color = GREEN
            start_btn.disabled = True
            stop_btn.disabled = False

            # Open WebSocket feeds in background threads
            _start_ws_threads(state, page)
        except Exception as ex:
            status_text.value = f"✗  Failed to start: {ex}"
            status_text.color = RED
        page.update()

    def stop_simulation(e):
        if state.sim_id is None:
            return
        try:
            httpx.post(f"{API_BASE}/api/v1/simulations/{state.sim_id}/stop",
                       timeout=5)
            state.is_running = False
            status_text.value = f"■  Simulation #{state.sim_id} stopped."
            status_text.color = ORANGE
            start_btn.disabled = False
            stop_btn.disabled = True
        except Exception as ex:
            status_text.value = f"✗  Stop failed: {ex}"
            status_text.color = RED
        page.update()

    start_btn.on_click = start_simulation
    stop_btn.on_click = stop_simulation

    # Load past sims once when the view is built (non-blocking)
    threading.Thread(target=load_past_simulations, daemon=True).start()

    # --- Layout --------------------------------------------------------------

    return ft.Column(
        scroll=ft.ScrollMode.AUTO,
        spacing=20,
        controls=[
            ft.Text("Simulation Controls", size=22, weight=ft.FontWeight.BOLD,
                    color=TEAL),
            ft.Divider(color=BG_SURFACE),

            # Config form
            ft.Container(
                padding=20,
                bgcolor=BG_CARD,
                border_radius=10,
                content=ft.Column(spacing=14, controls=[
                    ft.Text("Run Parameters", size=15, color=TEXT_MAIN,
                            weight=ft.FontWeight.W_600),
                    config_name,
                    num_robots,
                    duration_hours,
                    ft.Divider(color=BG_SURFACE),
                    real_time_switch,
                    speed_factor,
                    ft.Row([start_btn, stop_btn], spacing=12),
                    status_text,
                ]),
            ),

            # Past simulations panel
            ft.Container(
                padding=20,
                bgcolor=BG_CARD,
                border_radius=10,
                content=ft.Column(spacing=8, controls=[
                    ft.Text("Past Simulation Runs", size=15, color=TEXT_MAIN,
                            weight=ft.FontWeight.W_600),
                    past_sims_list,
                ]),
            ),
        ],
    )


# ===========================================================================
# VIEW 2 — Dashboard / Stats
# ===========================================================================

def build_dashboard_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Summary cards showing live delivery counts + final results.

    REST polling:  GET /api/v1/stats  (every 5 s while running)
                   GET /api/v1/simulations/{id}/results  (once, after run)
    """

    def stat_card(label: str, ref: ft.Ref) -> ft.Container:
        return ft.Container(
            width=180, height=110,
            bgcolor=BG_CARD,
            border_radius=10,
            padding=16,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(ref=ref, value="—", size=32,
                            weight=ft.FontWeight.BOLD, color=TEAL),
                    ft.Text(label, size=12, color=TEXT_DIM),
                ],
            ),
        )

    total_ref     = ft.Ref[ft.Text]()
    completed_ref = ft.Ref[ft.Text]()
    pending_ref   = ft.Ref[ft.Text]()
    robots_a_ref  = ft.Ref[ft.Text]()
    robots_i_ref  = ft.Ref[ft.Text]()
    avg_time_ref  = ft.Ref[ft.Text]()

    last_updated = ft.Text("Last updated: —", size=11, color=TEXT_DIM, italic=True)

    def refresh_stats():
        """Poll /api/v1/stats and update the card values."""
        try:
            resp = httpx.get(f"{API_BASE}/api/v1/stats", timeout=5)
            s = resp.json()
            total_ref.current.value     = str(s.get("total_deliveries", "—"))
            completed_ref.current.value = str(s.get("completed_deliveries", "—"))
            pending_ref.current.value   = str(s.get("pending_deliveries", "—"))
            robots_a_ref.current.value  = str(s.get("robots_active", "—"))
            robots_i_ref.current.value  = str(s.get("robots_idle", "—"))
            avg_s = s.get("average_delivery_time", None)
            avg_time_ref.current.value  = (
                f"{int(avg_s//60)}m {int(avg_s%60)}s" if avg_s else "—"
            )
            import datetime
            last_updated.value = (
                f"Last updated: {datetime.datetime.now().strftime('%H:%M:%S')}"
            )
            page.update()
        except Exception:
            pass  # Silently skip if API is not yet reachable

    # Register an update hook so the WS delivery callback can also refresh cards
    state.on_delivery_update = refresh_stats

    # Poll every 5 s in a background thread
    def poll_loop():
        while True:
            refresh_stats()
            import time; time.sleep(5)

    threading.Thread(target=poll_loop, daemon=True).start()

    return ft.Column(
        scroll=ft.ScrollMode.AUTO,
        spacing=20,
        controls=[
            ft.Text("Dashboard", size=22, weight=ft.FontWeight.BOLD, color=TEAL),
            ft.Divider(color=BG_SURFACE),
            last_updated,

            # Delivery counters
            ft.Text("Deliveries", size=15, color=TEXT_MAIN,
                    weight=ft.FontWeight.W_600),
            ft.Row(spacing=12, wrap=True, controls=[
                stat_card("Total",     total_ref),
                stat_card("Completed", completed_ref),
                stat_card("Pending",   pending_ref),
            ]),

            # Robot counters
            ft.Text("Robots", size=15, color=TEXT_MAIN,
                    weight=ft.FontWeight.W_600),
            ft.Row(spacing=12, wrap=True, controls=[
                stat_card("Active", robots_a_ref),
                stat_card("Idle",   robots_i_ref),
            ]),

            # Performance
            ft.Text("Performance", size=15, color=TEXT_MAIN,
                    weight=ft.FontWeight.W_600),
            ft.Row(spacing=12, controls=[
                stat_card("Avg Delivery Time", avg_time_ref),
            ]),
        ],
    )


# ===========================================================================
# VIEW 3 — Robot Status Table
# ===========================================================================

def build_robot_table_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Live table of robot status, updated via the WebSocket robot feed.
    Each row: Robot ID | Status | Lat | Lon | Current Delivery
    """

    STATUS_COLOURS = {
        "traveling":    TEAL,
        "at_restaurant": ORANGE,
        "at_residence": GREEN,
        "idle":         TEXT_DIM,
    }

    header = ft.Row(
        controls=[
            ft.Text("Robot ID", width=90,  color=TEXT_DIM, size=12,
                    weight=ft.FontWeight.W_600),
            ft.Text("Status",   width=130, color=TEXT_DIM, size=12,
                    weight=ft.FontWeight.W_600),
            ft.Text("Latitude", width=110, color=TEXT_DIM, size=12,
                    weight=ft.FontWeight.W_600),
            ft.Text("Longitude",width=110, color=TEXT_DIM, size=12,
                    weight=ft.FontWeight.W_600),
            ft.Text("Delivery", width=90,  color=TEXT_DIM, size=12,
                    weight=ft.FontWeight.W_600),
        ],
    )

    rows_column = ft.Column(spacing=4)
    no_data_msg = ft.Text(
        "Waiting for simulation to start…",
        color=TEXT_DIM, italic=True, size=13,
    )

    def robot_row(r: dict) -> ft.Row:
        status = r.get("status", "unknown")
        colour = STATUS_COLOURS.get(status, TEXT_MAIN)
        return ft.Row(
            controls=[
                ft.Text(f"#{r['robot_id']}",          width=90,  color=TEXT_MAIN, size=13),
                ft.Text(status,                        width=130, color=colour,   size=13),
                ft.Text(f"{r.get('lat', '—'):.5f}",   width=110, color=TEXT_MAIN, size=13),
                ft.Text(f"{r.get('lon', '—'):.5f}",   width=110, color=TEXT_MAIN, size=13),
                ft.Text(str(r.get('delivery_id', '—')), width=90, color=TEXT_DIM, size=13),
            ],
        )

    def refresh_table():
        """Rebuild rows from state.robots — called by WS robot callback."""
        rows_column.controls.clear()
        if not state.robots:
            rows_column.controls.append(no_data_msg)
        else:
            for r in sorted(state.robots, key=lambda x: x["robot_id"]):
                rows_column.controls.append(robot_row(r))
        page.update()

    # Register callback so the WebSocket thread can trigger a redraw
    state.on_robot_update = refresh_table

    # Show placeholder on first render
    rows_column.controls.append(no_data_msg)

    return ft.Column(
        scroll=ft.ScrollMode.AUTO,
        spacing=16,
        controls=[
            ft.Text("Robot Status", size=22, weight=ft.FontWeight.BOLD,
                    color=TEAL),
            ft.Divider(color=BG_SURFACE),
            ft.Container(
                padding=ft.Padding(left=16, right=16, top=12, bottom=12),
                bgcolor=BG_CARD,
                border_radius=10,
                content=ft.Column(spacing=8, controls=[
                    header,
                    ft.Divider(color=BG_SURFACE, height=1),
                    rows_column,
                ]),
            ),
        ],
    )


# ===========================================================================
# VIEW 4 — Delivery Log
# ===========================================================================

def build_delivery_log_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Scrollable feed of deliveries, newest at top.
    Updated via the WebSocket delivery feed.
    """

    STATUS_ICONS = {
        "delivered": ("✓", GREEN),
        "in_transit": ("→", TEAL),
        "pending":   ("…", ORANGE),
    }

    log_column = ft.Column(
        spacing=6,
        scroll=ft.ScrollMode.AUTO,
        height=520,
    )

    summary_text = ft.Text(
        "Total: 0  |  Completed: 0  |  Pending: 0",
        color=TEXT_DIM, size=13,
    )

    def delivery_row(d: dict) -> ft.Container:
        status = d.get("status", "pending")
        icon, colour = STATUS_ICONS.get(status, ("?", TEXT_DIM))
        dur = d.get("duration_s")
        dur_str = f"{int(dur//60)}m {int(dur%60)}s" if dur else "—"
        dist_str = f"{d.get('distance_km', 0):.2f} km"

        return ft.Container(
            padding=ft.Padding(left=16, right=16, top=10, bottom=10),
            bgcolor=BG_SURFACE,
            border_radius=8,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Row(spacing=10, controls=[
                        ft.Text(icon, size=18, color=colour),
                        ft.Column(spacing=2, controls=[
                            ft.Text(
                                f"Delivery #{d.get('delivery_id','?')}  "
                                f"· Robot #{d.get('robot_id','?')}",
                                color=TEXT_MAIN, size=13,
                                weight=ft.FontWeight.W_500,
                            ),
                            ft.Text(
                                f"{status}  ·  {dur_str}  ·  {dist_str}",
                                color=TEXT_DIM, size=11,
                            ),
                        ]),
                    ]),
                ],
            ),
        )

    def refresh_log():
        """Rebuild the log from state.recent_deliveries."""
        # Update summary banner
        s = state.delivery_summary
        summary_text.value = (
            f"Total: {s['total']}  |  "
            f"Completed: {s['completed']}  |  "
            f"Pending: {s['pending']}"
        )
        # Rebuild rows (newest first)
        log_column.controls.clear()
        if not state.recent_deliveries:
            log_column.controls.append(
                ft.Text("No deliveries yet — waiting for simulation…",
                        color=TEXT_DIM, italic=True, size=13)
            )
        else:
            for d in reversed(state.recent_deliveries):
                log_column.controls.append(delivery_row(d))
        page.update()

    # Register callback so the WebSocket thread can trigger a redraw
    # (overrides the dashboard's registration — each view can share the hook
    #  by chaining; for simplicity we chain them here)
    _prev_hook = state.on_delivery_update

    def chained_hook():
        if _prev_hook:
            _prev_hook()
        refresh_log()

    state.on_delivery_update = chained_hook

    return ft.Column(
        scroll=ft.ScrollMode.AUTO,
        spacing=16,
        controls=[
            ft.Text("Delivery Log", size=22, weight=ft.FontWeight.BOLD,
                    color=TEAL),
            ft.Divider(color=BG_SURFACE),
            summary_text,
            ft.Container(
                padding=ft.Padding(left=12, right=12, top=8, bottom=8),
                bgcolor=BG_CARD,
                border_radius=10,
                content=log_column,
            ),
        ],
    )


# ===========================================================================
# VIEW 5 — Live Map
# ===========================================================================

def build_map_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Interactive map of Glendale showing live robot positions as markers,
    built with flet-map on top of OpenStreetMap tiles.

    How it works
    ------------
    flet-map wraps Flutter's flutter_map package. The Map control contains:
      - TileLayer  → fetches OSM raster tiles from the internet at render time;
                     the browser requests only the tiles visible at the current
                     zoom level (no tiles are stored locally).
      - MarkerLayer → a list of Marker objects, each pinned to a lat/lon.
                     We rebuild this list every time the robot WebSocket pushes
                     a position update.

    The marker_layer reference is kept so refresh_map() can swap in a new
    markers list and call page.update() to redraw.
    """

    # Glendale city centre — where the map opens by default
    GLENDALE_LAT = 34.1425
    GLENDALE_LON = -118.2551

    STATUS_COLORS = {
        "traveling":     TEAL,
        "at_restaurant": ORANGE,
        "at_residence":  GREEN,
        "idle":          TEXT_DIM,
    }

    def robot_marker(r: dict) -> fmap.Marker:
        """Build a flet-map Marker for one robot dict from the WebSocket."""
        lat    = r.get("lat", GLENDALE_LAT)
        lon    = r.get("lon", GLENDALE_LON)
        status = r.get("status", "idle")
        rid    = r.get("robot_id", "?")
        color  = STATUS_COLORS.get(status, TEXT_MAIN)

        return fmap.Marker(
            # Geographic position of this marker
            coordinates=fmap.MapLatitudeLongitude(lat, lon),
            # The visual widget rendered at that position.
            # A Stack lets us layer a label on top of the dot.
            content=ft.Stack(
                width=36, height=36,
                controls=[
                    ft.Container(
                        width=20, height=20,
                        bgcolor=color,
                        border_radius=10,
                        border=ft.Border.all(2, "#FFFFFF"),
                        margin=ft.Margin(left=8, top=8, right=0, bottom=0),
                        tooltip=f"Robot #{rid} — {status}",
                    ),
                    ft.Text(
                        f"#{rid}",
                        size=9,
                        color="#FFFFFF",
                        weight=ft.FontWeight.BOLD,
                        left=11, top=11,
                    ),
                ],
            ),
        )

    # Route color by leg type — matches the marker status colors
    ROUTE_COLORS = {
        "to_restaurant": ORANGE,
        "to_residence":  GREEN,
    }

    # Build layers with empty lists initially.
    # refresh_map() replaces contents as WebSocket updates arrive.
    polyline_layer = fmap.PolylineLayer(polylines=[])
    marker_layer   = fmap.MarkerLayer(markers=[])

    # The Map control: OSM tiles → route polylines → robot markers (order matters).
    map_control = fmap.Map(
        expand=True,
        initial_center=fmap.MapLatitudeLongitude(GLENDALE_LAT, GLENDALE_LON),
        initial_zoom=13,
        min_zoom=11,
        max_zoom=18,
        layers=[
            fmap.TileLayer(
                url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                additional_options={
                    "attribution": "© OpenStreetMap contributors",
                },
            ),
            polyline_layer,   # drawn below markers so dots sit on top of lines
            marker_layer,
        ],
    )

    robot_count  = ft.Text("Waiting for simulation…", color=TEXT_DIM, size=12)
    last_updated = ft.Text("", color=TEXT_DIM, size=11, italic=True)

    def refresh_map():
        """Rebuild markers and route polylines from state.robots and redraw."""
        valid = [r for r in state.robots
                 if r.get("lat") is not None and r.get("lon") is not None]

        # Markers — one per robot, color by status
        marker_layer.markers = [robot_marker(r) for r in valid]

        # Polylines — one per robot that has an active route
        lines = []
        for r in valid:
            coords    = r.get("route_coords")
            leg_type  = r.get("leg_type")
            if not coords or not leg_type:
                continue
            color = ROUTE_COLORS.get(leg_type, TEAL)
            lines.append(
                fmap.Polyline(
                    points=[
                        fmap.MapLatitudeLongitude(lat, lon)
                        for lat, lon in coords
                    ],
                    color=color,
                    stroke_width=3.0,
                )
            )
        polyline_layer.polylines = lines

        n = len(marker_layer.markers)
        robot_count.value  = f"{n} robot{'s' if n != 1 else ''} active"
        last_updated.value = f"Updated {datetime.datetime.now().strftime('%H:%M:%S')}"
        page.update()

    # Chain after the robot-table hook so both views stay in sync
    _prev = state.on_robot_update

    def _chained():
        if _prev:
            _prev()
        refresh_map()

    state.on_robot_update = _chained

    def legend_dot(color: str, label: str) -> ft.Row:
        return ft.Row(spacing=6, controls=[
            ft.Container(width=12, height=12, bgcolor=color, border_radius=6),
            ft.Text(label, color=TEXT_DIM, size=11),
        ])

    def legend_line(color: str, label: str) -> ft.Row:
        return ft.Row(spacing=6, controls=[
            ft.Container(width=20, height=3, bgcolor=color, border_radius=2),
            ft.Text(label, color=TEXT_DIM, size=11),
        ])

    return ft.Column(
        spacing=12,
        controls=[
            ft.Text("Live Map", size=22, weight=ft.FontWeight.BOLD, color=TEAL),
            ft.Divider(color=BG_SURFACE),
            ft.Row([robot_count, last_updated], spacing=16),

            # Map fills available height; Container gives it a fixed height
            # so the rest of the column layout is stable.
            ft.Container(
                height=520,
                border_radius=10,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                content=map_control,
            ),

            # Status legend
            ft.Container(
                padding=ft.Padding(left=12, right=12, top=8, bottom=8),
                bgcolor=BG_CARD,
                border_radius=8,
                content=ft.Column(spacing=8, controls=[
                    ft.Row(spacing=24, controls=[
                        legend_dot(TEAL,     "Traveling"),
                        legend_dot(ORANGE,   "At restaurant"),
                        legend_dot(GREEN,    "At residence"),
                        legend_dot(TEXT_DIM, "Idle"),
                    ]),
                    ft.Row(spacing=24, controls=[
                        legend_line(ORANGE, "Route → restaurant"),
                        legend_line(GREEN,  "Route → residence"),
                    ]),
                ]),
            ),
        ],
    )


# ===========================================================================
# WebSocket helpers — run in background daemon threads
# ===========================================================================

def _start_ws_threads(state: AppState, page: ft.Page):
    """Spawn two daemon threads: one for robots, one for deliveries."""

    def run_robot_ws():
        import asyncio as _asyncio

        async def _connect():
            uri = f"{WS_BASE}/ws/robots/{state.sim_id}"
            try:
                async with websockets.connect(uri) as ws:
                    async for raw in ws:
                        if not state.is_running:
                            break
                        msg = json.loads(raw)
                        if msg.get("type") == "robot_update":
                            state.robots = msg.get("robots", [])
                            if state.on_robot_update:
                                state.on_robot_update()
            except Exception:
                pass  # Connection dropped — simulation ended

        _asyncio.run(_connect())

    def run_delivery_ws():
        import asyncio as _asyncio

        async def _connect():
            uri = f"{WS_BASE}/ws/deliveries/{state.sim_id}"
            try:
                async with websockets.connect(uri) as ws:
                    async for raw in ws:
                        if not state.is_running:
                            break
                        msg = json.loads(raw)
                        if msg.get("type") == "delivery_update":
                            state.delivery_summary = msg.get("summary", {})
                            state.recent_deliveries = msg.get("recent", [])
                            if state.on_delivery_update:
                                state.on_delivery_update()
            except Exception:
                pass

        _asyncio.run(_connect())

    threading.Thread(target=run_robot_ws,    daemon=True).start()
    threading.Thread(target=run_delivery_ws, daemon=True).start()


# ===========================================================================
# App shell — sidebar navigation
# ===========================================================================

def main(page: ft.Page):
    page.title = "Serve Robotics Simulation"
    page.bgcolor = BG_DARK
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0

    state = AppState()

    # Build all views upfront so their polling threads start immediately.
    # map view is built last so its on_robot_update hook chains correctly after
    # the robot-table hook (which is registered by build_robot_table_view).
    views = {
        "controls":  build_controls_view(state, page),
        "dashboard": build_dashboard_view(state, page),
        "robots":    build_robot_table_view(state, page),
        "log":       build_delivery_log_view(state, page),
        "map":       build_map_view(state, page),
    }

    # Content area — swapped out when nav changes
    content_area = ft.Container(
        expand=True,
        padding=24,
        content=views["controls"],
    )

    def nav_changed(e):
        idx = e.control.selected_index
        key = ["controls", "dashboard", "robots", "log", "map"][idx]
        content_area.content = views[key]
        page.update()

    nav_rail = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        bgcolor=BG_CARD,
        indicator_color=TEAL,
        indicator_shape=ft.RoundedRectangleBorder(radius=8),
        destinations=[
            ft.NavigationRailDestination(
                icon=ft.Icons.SETTINGS_OUTLINED,
                selected_icon=ft.Icons.SETTINGS,
                label="Controls",
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.DASHBOARD_OUTLINED,
                selected_icon=ft.Icons.DASHBOARD,
                label="Dashboard",
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.PRECISION_MANUFACTURING_OUTLINED,
                selected_icon=ft.Icons.PRECISION_MANUFACTURING,
                label="Robots",
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.LIST_ALT_OUTLINED,
                selected_icon=ft.Icons.LIST_ALT,
                label="Log",
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.MAP_OUTLINED,
                selected_icon=ft.Icons.MAP,
                label="Map",
            ),
        ],
        on_change=nav_changed,
        # Logo / title at the top of the rail
        leading=ft.Container(
            padding=ft.Padding(top=16, bottom=16),
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=2,
                controls=[
                    ft.Icon(ft.Icons.DELIVERY_DINING, color=TEAL, size=32),
                    ft.Text("Serve", color=TEAL, size=13,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("Robotics", color=TEXT_DIM, size=10),
                ],
            ),
        ),
    )

    page.add(
        ft.Row(
            expand=True,
            spacing=0,
            controls=[
                nav_rail,
                ft.VerticalDivider(width=1, color=BG_SURFACE),
                content_area,
            ],
        )
    )


ft.run(
    main,
    view=ft.AppView.WEB_BROWSER,
    host="0.0.0.0",
    port=8501,
)
