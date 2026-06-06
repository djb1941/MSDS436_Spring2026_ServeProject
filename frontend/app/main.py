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
import logging
import os
import threading

# Without an explicit config the root logger defaults to WARNING, so every
# logger.info(...) in this frontend was silently dropped — which hid useful
# diagnostics (e.g. taxi-stand loading). Configure INFO output to stdout.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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
        self.delivery_summary: dict = {"total": 0, "completed": 0, "pending": 0, "rejected": 0}
        self.delivery_events: list[dict] = []    # all recent deliveries, any state
        self.rejected_events: list[dict] = []    # recent rejected deliveries

        # Restaurant data: fetched once on load (static), busy state refreshed each WS push
        self.restaurants: list[dict] = []
        self.busy_restaurants: dict[int, int] = {}   # restaurant_id → pending_count

        # Active deliveries on leg 2 — one entry per robot heading to a residence
        self.active_residences: list[dict] = []

        # Button references set by build_sim_view so _start_ws_threads can
        # reset them on sim completion without requiring a closure over locals.
        self.start_btn = None
        self.stop_btn  = None

        # Callbacks registered by view builders so WS threads can trigger redraws
        self.on_robot_update    = None   # callable()
        self.on_delivery_update = None   # callable()


# ===========================================================================
# VIEW 1 — Simulation View and Controls
# ===========================================================================

def build_sim_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Two Rows: 
    (1) Map Display
    (2) Controls | Dashboard | Robot Log/Status

    (1) Interactive map of Glendale showing live robot positions as markers,
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

    (2a) Form for configuring and launching a simulation run.

    REST endpoints used:
        POST /api/v1/simulations/start   — launch
        POST /api/v1/simulations/{id}/stop — stop
        GET  /api/v1/simulations         — list past runs

    (2b) Summary cards showing live delivery counts + final results.

    REST polling:  GET /api/v1/stats  (every 5 s while running)
                   GET /api/v1/simulations/{id}/results  (once, after run)
    """
    # --- Map Setup --------------------------------------------------------

    # Approximate centroid of the simulation boundary (CA-134 / CA-2 / LA River)
    GLENDALE_LAT = 34.1320
    GLENDALE_LON = -118.2490

    STATUS_COLORS = {
        "traveling":      TEAL,
        "at_restaurant":  ORANGE,
        "at_residence":   GREEN,
        "idle":           TEXT_DIM,
        "repositioning":  "#3498DB",   # blue — traveling to standby post
    }

    # Restaurant marker colours
    # Gray  = no active order;  Orange = has at least one pending delivery request
    RESTAURANT_IDLE_COLOR = "#7F8C8D"
    RESTAURANT_BUSY_COLOR = ORANGE

    def show_restaurant_info(restaurant: dict):
        """Open an AlertDialog with name, address, and live delivery status."""
        rid = restaurant["id"]
        pending = state.busy_restaurants.get(rid, 0)

        if pending:
            status_icon  = "⚠"
            status_text  = f"{pending} pending request{'s' if pending > 1 else ''}"
            status_color = RESTAURANT_BUSY_COLOR
        else:
            status_icon  = "✓"
            status_text  = "No active requests"
            status_color = GREEN

        def close(e):
            dlg.open = False
            page.update()

        dlg = ft.AlertDialog(
            bgcolor=BG_CARD,
            title=ft.Row(spacing=8, controls=[
                ft.Icon(ft.Icons.RESTAURANT, color=TEAL, size=20),
                ft.Text(restaurant["name"], color=TEXT_MAIN, size=16,
                        weight=ft.FontWeight.W_600),
            ]),
            content=ft.Column(
                tight=True,
                spacing=10,
                controls=[
                    ft.Row(spacing=6, controls=[
                        ft.Icon(ft.Icons.LOCATION_ON, color=TEXT_DIM, size=14),
                        ft.Text(restaurant.get("address") or "Address unknown",
                                color=TEXT_DIM, size=13),
                    ]),
                    ft.Divider(color=BG_SURFACE),
                    ft.Row(spacing=6, controls=[
                        ft.Text(status_icon, size=15, color=status_color),
                        ft.Text(status_text, color=status_color, size=13,
                                weight=ft.FontWeight.W_500),
                    ]),
                ],
            ),
            actions=[
                ft.TextButton("Close", on_click=close,
                              style=ft.ButtonStyle(color=TEAL)),
            ],
        )
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    def restaurant_marker(r: dict) -> fmap.Marker:
        """Small square marker for one restaurant; color reflects busy state."""
        rid     = r["id"]
        is_busy = rid in state.busy_restaurants
        color   = RESTAURANT_BUSY_COLOR if is_busy else RESTAURANT_IDLE_COLOR

        return fmap.Marker(
            coordinates=fmap.MapLatitudeLongitude(r["lat"], r["lon"]),
            content=ft.Container(
                width=18, height=18,
                bgcolor=color,
                border_radius=3,
                border=ft.Border.all(1.5, "#FFFFFF"),
                tooltip=r["name"],
                content=ft.Icon(ft.Icons.RESTAURANT, size=10, color="#FFFFFF"),
                on_click=lambda e, res=r: show_restaurant_info(res),
            ),
        )

    def show_residence_info(residence: dict):
        """Open an AlertDialog with address and robot ETA for a delivery destination."""
        eta = residence.get("eta_sim_minutes")
        if eta is not None:
            eta_str = f"~{eta:.0f} sim min" if eta >= 1 else "< 1 sim min"
        else:
            eta_str = "—"

        def close(e):
            dlg.open = False
            page.update()

        dlg = ft.AlertDialog(
            bgcolor=BG_CARD,
            title=ft.Row(spacing=8, controls=[
                ft.Icon(ft.Icons.HOME, color=GREEN, size=20),
                ft.Text("Delivery Destination", color=TEXT_MAIN, size=16,
                        weight=ft.FontWeight.W_600),
            ]),
            content=ft.Column(
                tight=True,
                spacing=10,
                controls=[
                    ft.Row(spacing=6, controls=[
                        ft.Icon(ft.Icons.LOCATION_ON, color=TEXT_DIM, size=14),
                        ft.Text(residence.get("address", "Address unknown"),
                                color=TEXT_DIM, size=13),
                    ]),
                    ft.Divider(color=BG_SURFACE),
                    ft.Row(spacing=6, controls=[
                        ft.Icon(ft.Icons.DELIVERY_DINING, color=TEAL, size=14),
                        ft.Text(f"Robot #{residence['robot_id']}",
                                color=TEXT_MAIN, size=13),
                    ]),
                    ft.Row(spacing=6, controls=[
                        ft.Icon(ft.Icons.SCHEDULE, color=TEAL, size=14),
                        ft.Text(f"ETA: {eta_str}", color=TEXT_MAIN, size=13,
                                weight=ft.FontWeight.W_500),
                    ]),
                ],
            ),
            actions=[
                ft.TextButton("Close", on_click=close,
                              style=ft.ButtonStyle(color=TEAL)),
            ],
        )
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    def residence_marker(r: dict) -> fmap.Marker:
        """House marker for an active delivery destination; shows affiliated robot number."""
        robot_id = r["robot_id"]
        return fmap.Marker(
            coordinates=fmap.MapLatitudeLongitude(r["residence_lat"], r["residence_lon"]),
            content=ft.GestureDetector(
                on_tap=lambda e, res=r: show_residence_info(res),
                content=ft.Stack(
                    width=40, height=22,
                    controls=[
                        ft.Container(
                            width=20, height=20,
                            bgcolor=GREEN,
                            border_radius=4,
                            border=ft.Border.all(1.5, "#FFFFFF"),
                            tooltip=f"Delivery destination · Robot #{robot_id}",
                            content=ft.Icon(ft.Icons.HOME, size=11, color="#FFFFFF"),
                        ),
                        ft.Text(
                            f"#{robot_id}",
                            size=8,
                            color=TEXT_MAIN,
                            weight=ft.FontWeight.BOLD,
                            left=23,
                            top=5,
                        ),
                    ],
                ),
            ),
        )

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

    # --- Simulation boundary polygon ----------------------------------------
    # Mirrors BOUNDARY_COORDS in setup.py (lon, lat) → fmap (lat, lon).
    # Closing coordinate omitted — flet-map closes the polygon automatically.
    # Layer order: boundary fill first so routes and markers sit on top of it.
    BOUNDARY_POINTS = [
        fmap.MapLatitudeLongitude(34.15291, -118.27945),  # A — CA-134 × LA River (NW)
        fmap.MapLatitudeLongitude(34.15619, -118.26446),  # B - CA-134 & Pacific Ave
        fmap.MapLatitudeLongitude(34.15663, -118.25794),  # C - CA-134 & Central Ave
        fmap.MapLatitudeLongitude(34.15663, -118.24681),  # D - CA-134 & Geneva St
        fmap.MapLatitudeLongitude(34.15597, -118.24208),  # E - CA-134 & Glendale Ave
        fmap.MapLatitudeLongitude(34.14670, -118.22611),  # F — CA-134 × CA-2 interchange (NE)
        fmap.MapLatitudeLongitude(34.13676, -118.22918),  # G - CA-2 @ Verd Oaks Dr
        fmap.MapLatitudeLongitude(34.13215, -118.22946),  # H - CA-2 & Round Top Dr
        fmap.MapLatitudeLongitude(34.12512, -118.22821),  # I - CA-2 & York Blvd
        fmap.MapLatitudeLongitude(34.12128, -118.22978),  # J - CA-2 & Verdugo Rd
        fmap.MapLatitudeLongitude(34.11782, -118.23484),  # K - CA-2 & Avenue 36
        fmap.MapLatitudeLongitude(34.11326, -118.24418),  # L - CA-2 & San Fernando Rd
        fmap.MapLatitudeLongitude(34.10829, -118.25200),  # M - CA-2 × LA River (south vertex, Atwater Village)
        fmap.MapLatitudeLongitude(34.10847, -118.25633),  # N - LA River @ Silver Lake Blvd
        fmap.MapLatitudeLongitude(34.11020, -118.26127),  # O - LA River @ Garcia St
        fmap.MapLatitudeLongitude(34.11348, -118.26557),  # P - LA River & Glendale Blvd
        fmap.MapLatitudeLongitude(34.12154, -118.27044),  # Q - LA River & Los Feliz Blvd
        fmap.MapLatitudeLongitude(34.14096, -118.27679),  # R - LA River and Colorado St
        fmap.MapLatitudeLongitude(34.15291, -118.27945),  # A — close polygon back to NW corner
    ]

    boundary_polygon_layer = fmap.PolygonLayer(
        polygons=[
            fmap.PolygonMarker(
                coordinates=BOUNDARY_POINTS,
                color="#1A00C8AA",
                border_color=TEAL,
                border_stroke_width=2.5,
            )
        ]
    )

    # Build layers with empty lists initially.
    # refresh_map() replaces contents as WebSocket updates arrive.
    # restaurant_layer is also rebuilt by refresh_map() so colors update live.
    restaurant_layer   = fmap.MarkerLayer(markers=[])
    polyline_layer     = fmap.PolylineLayer(polylines=[])
    residence_layer    = fmap.MarkerLayer(markers=[])   # active delivery destinations
    post_stand_layer   = fmap.MarkerLayer(markers=[])   # taxi/post stands (post_location strategy)
    marker_layer       = fmap.MarkerLayer(markers=[])

    # The Map control: OSM tiles → boundary → restaurants → routes →
    #                  residence destinations → robots (topmost).
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
            boundary_polygon_layer,   # simulation boundary fill + outline
            restaurant_layer,         # restaurant locations (clickable)
            post_stand_layer,         # taxi / post stands (post_location strategy)
            polyline_layer,           # active robot routes
            residence_layer,          # active delivery destinations (above routes)
            marker_layer,             # robot position dots (topmost)
        ],
    )

    robot_count      = ft.Text("Waiting for simulation…", color=TEXT_DIM, size=12)
    map_refresh_text = ft.Text("", color=TEXT_DIM, size=11, italic=True)

    # Surfaced when the sim progress percentage stops advancing while a run is
    # still active — a likely sign the backend simulation has hung (e.g. the
    # post_restaurant zero-yield loop). Time-based so it is robust to how often
    # refresh_stats is called (5 s poll + every WS delivery push).
    stall_warning = ft.Text("", color=RED, size=12, weight=ft.FontWeight.W_600)
    STALL_SECONDS = 30          # no progress for this long → show warning
    _stall = {"pct": None, "since": 0.0}

    # Loaded taxi/post stands (post_location strategy). Held here and drawn in
    # refresh_map so they render via the exact same path as restaurants/robots
    # and persist across live WS-driven redraws.
    post_stands_state = {"stands": []}

    def refresh_map():
        """Rebuild markers and route polylines from state.robots and redraw."""
        valid = [r for r in state.robots
                 if r.get("lat") is not None and r.get("lon") is not None]

        # Restaurant markers — rebuilt each call so busy colors stay in sync
        restaurant_layer.markers = [
            restaurant_marker(r) for r in state.restaurants
        ]

        # Robot markers — one per robot, color by status
        marker_layer.markers = [robot_marker(r) for r in valid]

        # Residence markers — one per robot currently on leg 2 (to_residence)
        residence_layer.markers = [
            residence_marker(r) for r in state.active_residences
            if r.get("residence_lat") is not None
        ]

        # Taxi/post stand markers — drawn here so they survive every redraw
        post_stand_layer.markers = [
            post_stand_marker(p) for p in post_stands_state["stands"]
        ]

        # Polylines — one per robot that has an active route
        lines = []
        for r in valid:
            coords    = r.get("route_coords")
            leg_type  = r.get("leg_type")
            if not coords or not leg_type:
                continue
            color = ROUTE_COLORS.get(leg_type, TEAL)
            lines.append(
                fmap.PolylineMarker(
                    coordinates=[
                        fmap.MapLatitudeLongitude(lat, lon)
                        for lat, lon in coords
                    ],
                    color=color,
                    stroke_width=3.0,
                )
            )
        polyline_layer.polylines = lines

        n = len(marker_layer.markers)
        robot_count.value = f"{n} robot{'s' if n != 1 else ''} active"
        page.update()

    # --- Robot Table (embedded in the Robot Status tab below) ---------------
    # These vars must be defined *before* the map's chaining block so that
    # _prev captures refresh_table and both callbacks fire on each WS push.

    _TABLE_STATUS_COLOURS = {
        "traveling":      TEAL,
        "at_restaurant":  ORANGE,
        "at_residence":   GREEN,
        "idle":           TEXT_DIM,
        "repositioning":  "#3498DB",
    }

    # ------------------------------------------------------------------
    # Shared sort-header helper
    # ------------------------------------------------------------------

    def _sort_cell(label: str, col_key: str, width: int,
                   sort_state: dict, rebuild_fn) -> ft.Container:
        """
        Clickable header cell.  Shows ↑/↓ on the active sort column,
        highlights it in TEAL; inactive columns stay TEXT_DIM.
        """
        active = sort_state["col"] == col_key
        arrow  = (" ↑" if sort_state["asc"] else " ↓") if active else ""
        color  = TEAL if active else TEXT_DIM

        def on_click(e, key=col_key):
            if sort_state["col"] == key:
                sort_state["asc"] = not sort_state["asc"]
            else:
                sort_state["col"] = key
                sort_state["asc"] = True
            rebuild_fn()

        return ft.Container(
            width=width,
            on_click=on_click,
            content=ft.Text(
                label + arrow,
                color=color,
                size=12,
                weight=ft.FontWeight.W_600,
            ),
        )

    # ------------------------------------------------------------------
    # Robot table
    # ------------------------------------------------------------------

    _robot_sort = {"col": "robot_id", "asc": True}

    # Placeholder column — rebuilt by rebuild_robot_table()
    robot_table_col = ft.Column(spacing=4)
    _robot_no_data  = ft.Text(
        "Waiting for simulation to start…", color=TEXT_DIM, italic=True, size=13
    )
    robot_table_col.controls.append(_robot_no_data)

    def rebuild_robot_table():
        col, asc = _robot_sort["col"], _robot_sort["asc"]

        def _key(r):
            if col == "robot_id":   return r.get("robot_id", 0)
            if col == "status":     return r.get("status", "")
            if col == "lat":        return r.get("lat", 0.0)
            if col == "lon":        return r.get("lon", 0.0)
            if col == "delivery":   return r.get("delivery_id") or 0
            return 0

        header = ft.Row(controls=[
            _sort_cell("Robot ID",  "robot_id", 90,  _robot_sort, rebuild_robot_table),
            _sort_cell("Status",    "status",   130, _robot_sort, rebuild_robot_table),
            _sort_cell("Latitude",  "lat",      110, _robot_sort, rebuild_robot_table),
            _sort_cell("Longitude", "lon",      110, _robot_sort, rebuild_robot_table),
            _sort_cell("Delivery",  "delivery", 90,  _robot_sort, rebuild_robot_table),
        ])

        robot_table_col.controls.clear()
        robot_table_col.controls.append(header)
        robot_table_col.controls.append(ft.Divider(color=BG_SURFACE, height=1))

        if not state.robots:
            robot_table_col.controls.append(_robot_no_data)
        else:
            for r in sorted(state.robots, key=_key, reverse=not asc):
                status = r.get("status", "unknown")
                colour = _TABLE_STATUS_COLOURS.get(status, TEXT_MAIN)
                robot_table_col.controls.append(ft.Row(controls=[
                    ft.Text(f"#{r['robot_id']}",            width=90,  color=TEXT_MAIN, size=13),
                    ft.Text(status,                          width=130, color=colour,    size=13),
                    ft.Text(f"{r.get('lat', 0):.5f}",       width=110, color=TEXT_MAIN, size=13),
                    ft.Text(f"{r.get('lon', 0):.5f}",       width=110, color=TEXT_MAIN, size=13),
                    ft.Text(str(r.get("delivery_id", "—")), width=90,  color=TEXT_DIM,  size=13),
                ]))
        page.update()

    # Render initial header (no data yet)
    rebuild_robot_table()

    def refresh_table():
        rebuild_robot_table()

    state.on_robot_update = refresh_table

    # ------------------------------------------------------------------
    # Restaurant table
    # ------------------------------------------------------------------

    _rest_sort  = {"col": "id", "asc": True}

    rest_table_col  = ft.Column(spacing=4)
    _rest_no_data   = ft.Text(
        "Waiting for simulation to start…", color=TEXT_DIM, italic=True, size=13
    )
    rest_table_col.controls.append(_rest_no_data)

    def rebuild_restaurant_table():
        col, asc = _rest_sort["col"], _rest_sort["asc"]

        def _key(r):
            if col == "id":      return r.get("id", 0)
            if col == "name":    return (r.get("name") or "").lower()
            if col == "status":  return 0 if state.busy_restaurants.get(r["id"], 0) else 1
            if col == "pending": return state.busy_restaurants.get(r["id"], 0)
            if col == "address": return (r.get("address") or "").lower()
            return 0

        header = ft.Row(controls=[
            _sort_cell("ID",      "id",      50,  _rest_sort, rebuild_restaurant_table),
            _sort_cell("Name",    "name",    160, _rest_sort, rebuild_restaurant_table),
            _sort_cell("Status",  "status",  70,  _rest_sort, rebuild_restaurant_table),
            _sort_cell("Pending", "pending", 70,  _rest_sort, rebuild_restaurant_table),
            _sort_cell("Address", "address", 200, _rest_sort, rebuild_restaurant_table),
        ])

        rest_table_col.controls.clear()
        rest_table_col.controls.append(header)
        rest_table_col.controls.append(ft.Divider(color=BG_SURFACE, height=1))

        if not state.restaurants:
            rest_table_col.controls.append(_rest_no_data)
        else:
            for r in sorted(state.restaurants, key=_key, reverse=not asc):
                pending = state.busy_restaurants.get(r["id"], 0)
                status  = "busy" if pending else "idle"
                colour  = ORANGE if pending else TEXT_DIM
                rest_table_col.controls.append(ft.Row(controls=[
                    ft.Text(f"#{r['id']}",                   width=50,  color=TEXT_MAIN, size=13),
                    ft.Text(r.get("name", "—"),               width=160, color=TEXT_MAIN, size=13),
                    ft.Text(status,                            width=70,  color=colour,    size=13),
                    ft.Text(str(pending) if pending else "—",  width=70,  color=colour,    size=13),
                    ft.Text(r.get("address", "—"),             width=200, color=TEXT_DIM,  size=12),
                ]))
        page.update()

    rebuild_restaurant_table()

    def refresh_restaurant_table():
        rebuild_restaurant_table()

    # Chain after the robot-table hook so both views stay in sync
    _prev = state.on_robot_update

    def _chained():
        if _prev:
            _prev()
        refresh_map()
        refresh_restaurant_table()

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

    # --- Form fields --------------------------------------------------------

    config_name = ft.TextField(
        label="Config name",
        value="default",
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

    def on_idle_strategy_change(e):
        logger.info("on_idle_strategy_change fired: value=%r", e.control.value)
        if e.control.value == "post_location":
            threading.Thread(
                target=load_post_stands,
                args=(config_name.value.strip() or "default",),
                daemon=True,
            ).start()
        else:
            clear_post_stands()

    idle_strategy_dropdown = ft.Dropdown(
        label="Idle robot behavior",
        value="stay",
        color=TEXT_MAIN,
        bgcolor=BG_SURFACE,
        border_color=TEAL,
        focused_border_color=TEAL,
        width=300,
        options=[
            ft.dropdown.Option(key="stay",             text="Stay — hold position after delivery"),
            ft.dropdown.Option(key="post_restaurant",  text="Post at restaurant — reposition to nearest free restaurant"),
            ft.dropdown.Option(key="post_location",    text="Post at stand — reposition to nearest taxi stand"),
        ],
    )
    idle_strategy_dropdown.on_change = on_idle_strategy_change

    def on_config_name_change(e):
        # If we're showing taxi stands, a different config may define a
        # different set — reload them so the map reflects the chosen config.
        if idle_strategy_dropdown.value == "post_location":
            threading.Thread(
                target=load_post_stands,
                args=(config_name.value.strip() or "default",),
                daemon=True,
            ).start()

    config_name.on_change = on_config_name_change

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

    # Store button references in state so _start_ws_threads (module-level) can
    # reach them without needing a closure over build_sim_view's locals.
    state.start_btn = start_btn
    state.stop_btn  = stop_btn

    def start_simulation(e):
        payload = {
            "config_name":    config_name.value.strip() or "default",
            "num_robots":     int(num_robots.value or 5),
            "duration_hours": float(duration_hours.value or 24),
            "real_time":      real_time_switch.value,
            "speed_factor":   float(speed_factor.value or 1),
            "idle_strategy":  idle_strategy_dropdown.value or "stay",
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

            # Reliable fallback: ensure taxi stands are drawn whenever a
            # post_location run starts, even if the dropdown's on_change never
            # fired to load them.
            if payload["idle_strategy"] == "post_location":
                threading.Thread(
                    target=load_post_stands,
                    args=(payload["config_name"],),
                    daemon=True,
                ).start()

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

    POST_STAND_COLOR = "#9B59B6"   # purple — distinct from robot and restaurant colors

    def post_stand_marker(p: dict) -> fmap.Marker:
        """Taxi/post-stand marker — a plain ft.Icon as the Marker content,
        matching flet-map's documented example and keeping it consistent with
        the other markers."""
        return fmap.Marker(
            coordinates=fmap.MapLatitudeLongitude(p["lat"], p["lon"]),
            content=ft.Icon(
                ft.Icons.LOCAL_TAXI,
                color=POST_STAND_COLOR,
                size=22,
                tooltip=p.get("name", "Post stand"),
            ),
        )

    def load_post_stands(config_name: str = "default"):
        """Fetch post_locations from the API and render them as markers."""
        try:
            resp = httpx.get(
                f"{API_BASE}/api/v1/post_locations",
                params={"config": config_name},
                timeout=10,
            )
            stands = resp.json().get("post_locations", [])
            post_stands_state["stands"] = stands
            logger.info(
                "load_post_stands: fetched %d stand(s) for config '%s'",
                len(stands), config_name,
            )
        except Exception as ex:
            logger.warning(f"Could not load post_locations: {ex}")
            post_stands_state["stands"] = []
        refresh_map()

    def clear_post_stands():
        post_stands_state["stands"] = []
        refresh_map()

    def load_restaurants():
        """Fetch all restaurants from the API once and populate the map layer."""
        try:
            resp = httpx.get(f"{API_BASE}/api/v1/restaurants", timeout=10)
            state.restaurants = resp.json().get("restaurants", [])
            refresh_map()
        except Exception as ex:
            logger.warning(f"Could not load restaurants: {ex}")

    # Load past sims and restaurant pins once when the view is built (non-blocking)
    threading.Thread(target=load_past_simulations, daemon=True).start()
    threading.Thread(target=load_restaurants,      daemon=True).start()
    
    def stat_card(label: str, ref: ft.Ref, value_color: str = TEAL) -> ft.Container:
        return ft.Container(
            width=180, height=110,
            bgcolor=BG_SURFACE,
            border_radius=10,
            padding=16,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(ref=ref, value="—", size=32,
                            weight=ft.FontWeight.BOLD, color=value_color),
                    ft.Text(label, size=12, color=TEXT_DIM,
                            text_align=ft.TextAlign.CENTER),
                ],
            ),
        )

    total_ref     = ft.Ref[ft.Text]()
    completed_ref = ft.Ref[ft.Text]()
    pending_ref   = ft.Ref[ft.Text]()
    rejected_ref  = ft.Ref[ft.Text]()
    robots_a_ref  = ft.Ref[ft.Text]()
    robots_i_ref  = ft.Ref[ft.Text]()
    avg_time_ref  = ft.Ref[ft.Text]()

    def _fmt_remaining(seconds: float) -> str:
        """Format wall-clock seconds as a compact duration string."""
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"{h}h {m}m"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    def refresh_stats():
        """Poll /api/v1/stats and update the card values."""
        try:
            # Scope the poll to the run we started, so a previously completed
            # simulation (which can momentarily be the "most recent" one while
            # this run's row still has a NULL started_at) can't be mistaken for
            # ours and flip is_running off, killing the live feed.
            params = {"sim_id": state.sim_id} if state.sim_id is not None else {}
            resp = httpx.get(f"{API_BASE}/api/v1/stats", params=params, timeout=5)
            s = resp.json()
            total_ref.current.value     = str(s.get("total_deliveries", "—"))
            completed_ref.current.value = str(s.get("completed_deliveries", "—"))
            pending_ref.current.value   = str(s.get("pending_deliveries", "—"))
            rejected_ref.current.value  = str(s.get("rejected_deliveries", "—"))
            robots_a_ref.current.value  = str(s.get("robots_active", "—"))
            robots_i_ref.current.value  = str(s.get("robots_idle", "—"))
            avg_s = s.get("average_delivery_time", None)
            avg_time_ref.current.value  = (
                f"{int(avg_s//60)}m {int(avg_s%60)}s" if avg_s else "—"
            )

            # --- Progress / ETA display ---
            pct        = s.get("sim_progress_pct", 0.0)
            wall_rem   = s.get("wall_time_remaining_s")
            sim_status = s.get("sim_status", "")

            # Current local time with timezone abbreviation
            now_str = datetime.datetime.now().astimezone().strftime("%I:%M:%S %p %Z")

            # Poll-based button reset — fallback for when the WS misses the final push.
            # Only act when these stats are for the run we're actually tracking;
            # otherwise a stale "most recent" sim could prematurely end ours.
            stats_sim_id = s.get("simulation_id")
            is_our_sim = stats_sim_id is None or stats_sim_id == state.sim_id
            if (is_our_sim and sim_status in ("completed", "failed", "stopped")
                    and state.is_running):
                state.is_running         = False
                start_btn.disabled       = False
                stop_btn.disabled        = True

            if sim_status == "completed":
                map_refresh_text.value = f"Last Dashboard Update: {now_str}  ·  Simulation complete (100%)"
                map_refresh_text.color = GREEN
            elif sim_status == "stopped":
                map_refresh_text.value = f"Last Dashboard Update: {now_str}  ·  Simulation stopped at {pct:.1f}%"
                map_refresh_text.color = ORANGE
            elif pct >= 99.9:
                map_refresh_text.value = f"Last Dashboard Update: {now_str}  ·  Simulation ETA: finishing…"
                map_refresh_text.color = TEAL
            elif pct > 1.0 and wall_rem is not None:
                map_refresh_text.value = (
                    f"Last Dashboard Update: {now_str}  ·  Simulation ETA: {_fmt_remaining(wall_rem)} remaining  ({pct:.1f}% complete)"
                )
                map_refresh_text.color = TEAL
            elif pct > 0:
                map_refresh_text.value = f"Last Dashboard Update: {now_str}  ·  Simulation ETA: calculating…"
                map_refresh_text.color = TEXT_DIM
            else:
                map_refresh_text.value = f"Last Dashboard Update: {now_str}  ·  Simulation not yet started"
                map_refresh_text.color = TEXT_DIM

            # --- Stall detection ---
            # If a run is active and not finishing, but sim_progress_pct hasn't
            # moved for STALL_SECONDS, the backend is probably hung. Time-based
            # so it doesn't matter whether this came from a poll or a WS push.
            import time as _time
            now_mono = _time.monotonic()
            running = state.is_running and sim_status not in (
                "completed", "failed", "stopped"
            )
            if running and pct < 99.9:
                if _stall["pct"] is None or abs(pct - _stall["pct"]) > 1e-6:
                    # Progress moved (or first sample) — reset the clock
                    _stall["pct"]   = pct
                    _stall["since"] = now_mono
                    stall_warning.value = ""
                elif now_mono - _stall["since"] >= STALL_SECONDS:
                    stalled_for = int(now_mono - _stall["since"])
                    stall_warning.value = (
                        f"⚠  Simulation may be stalled — no progress for "
                        f"{stalled_for}s (stuck at {pct:.1f}%). "
                        f"Check the engine logs / try Stop."
                    )
            else:
                # Not running, finishing, or finished — clear any warning
                _stall["pct"] = None
                stall_warning.value = ""

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

    # --- Layout --------------------------------------------------------------

    return ft.Column(
        scroll=ft.ScrollMode.AUTO,
        spacing=20,
        expand=True,
        controls=[
            ft.Text("Live Map", size=22, weight=ft.FontWeight.BOLD, color=TEAL),
            ft.Divider(color=BG_SURFACE),
            ft.Row([robot_count, map_refresh_text], spacing=16),
            stall_warning,

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
                        legend_dot(TEAL,      "Traveling"),
                        legend_dot(ORANGE,    "At restaurant"),
                        legend_dot(GREEN,     "At residence"),
                        legend_dot(TEXT_DIM,  "Idle"),
                        legend_dot("#3498DB", "Repositioning"),
                    ]),
                    ft.Row(spacing=24, controls=[
                        legend_line(ORANGE, "Route → restaurant"),
                        legend_line(GREEN,  "Route → residence"),
                        legend_line(TEAL,   "Simulation boundary"),
                    ]),
                    ft.Row(spacing=24, controls=[
                        ft.Row(spacing=6, controls=[
                            ft.Container(width=12, height=12,
                                         bgcolor="#7F8C8D", border_radius=2),
                            ft.Text("Restaurant", color=TEXT_DIM, size=11),
                        ]),
                        ft.Row(spacing=6, controls=[
                            ft.Container(width=12, height=12,
                                         bgcolor=ORANGE, border_radius=2),
                            ft.Text("Restaurant (order pending)", color=TEXT_DIM, size=11),
                        ]),
                        ft.Row(spacing=6, controls=[
                            ft.Icon(ft.Icons.LOCAL_TAXI, color=POST_STAND_COLOR, size=14),
                            ft.Text("Post / taxi stand", color=TEXT_DIM, size=11),
                        ]),
                    ]),
                ]),
            ),
            ft.Row(
                controls=[
                    ft.Text("Simulation Controls", size=22, weight=ft.FontWeight.BOLD,
                            color=TEAL),
                    ft.Divider(color=BG_SURFACE),

                    # Config form
                    ft.Container(
                        padding=20,
                        bgcolor=BG_CARD,
                        border_radius=10,
                        content=ft.Column(
                            spacing=14,
                            controls=[
                                ft.Text("Run Parameters", size=15, color=TEXT_MAIN,
                                        weight=ft.FontWeight.W_600),
                                config_name,
                                num_robots,
                                duration_hours,
                                idle_strategy_dropdown,
                                ft.Divider(color=BG_SURFACE),
                                real_time_switch,
                                speed_factor,
                                ft.Row([start_btn, stop_btn], spacing=12),
                                status_text,
                            ],
                        ),
                    ),
                    ft.Container(
                        width=600,
                        height=600,
                        content=ft.Tabs(
                            length=3,
                            expand=True,
                            content=ft.Column(
                                expand=True,
                                controls=[
                                    ft.TabBar(
                                        tabs=[
                                            ft.Tab(label="Log"),
                                            ft.Tab(label="Robot Status"),
                                            ft.Tab(label="Restaurants"),
                                        ],
                                    ),
                                    ft.TabBarView(
                                        expand=True,
                                        controls=[
                                            # Log tab
                                            build_delivery_log_view(state, page),
                                            # Robot Status tab
                                            ft.Column(
                                                scroll=ft.ScrollMode.AUTO,
                                                spacing=16,
                                                controls=[
                                                    ft.Text("Robot Status", size=22,
                                                            weight=ft.FontWeight.BOLD, color=TEAL),
                                                    ft.Divider(color=BG_SURFACE),
                                                    ft.Container(
                                                        padding=ft.Padding(left=16, right=16, top=12, bottom=12),
                                                        bgcolor=BG_CARD,
                                                        border_radius=10,
                                                        content=robot_table_col,
                                                    ),
                                                ],
                                            ),
                                            # Restaurants tab
                                            ft.Column(
                                                scroll=ft.ScrollMode.AUTO,
                                                spacing=16,
                                                controls=[
                                                    ft.Text("Restaurants", size=22,
                                                            weight=ft.FontWeight.BOLD, color=TEAL),
                                                    ft.Divider(color=BG_SURFACE),
                                                    ft.Container(
                                                        padding=ft.Padding(left=16, right=16, top=12, bottom=12),
                                                        bgcolor=BG_CARD,
                                                        border_radius=10,
                                                        content=rest_table_col,
                                                    ),
                                                ],
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        ),
                    ),
                    ft.Container(
                        padding=ft.Padding(left=16, right=16, top=12, bottom=16),
                        bgcolor=BG_CARD,
                        border_radius=10,
                        content=ft.Column(
                            spacing=12,
                            controls=[
                                ft.Text("Dashboard", size=18,
                                        weight=ft.FontWeight.BOLD, color=TEAL),
                                ft.Divider(color=BG_SURFACE, height=1),

                                # Three columns: Deliveries | Robots | Performance
                                ft.Row(
                                    spacing=16,
                                    vertical_alignment=ft.CrossAxisAlignment.START,
                                    controls=[
                                        # --- Deliveries column ---
                                        ft.Column(spacing=8, controls=[
                                            ft.Text("Deliveries", size=12,
                                                    color=TEXT_DIM,
                                                    weight=ft.FontWeight.W_600),
                                            stat_card("Total",     total_ref),
                                            stat_card("Completed", completed_ref),
                                            stat_card("Pending",   pending_ref),
                                            stat_card("Rejected",  rejected_ref,
                                                      value_color=RED),
                                        ]),

                                        # --- Robots column ---
                                        ft.Column(spacing=8, controls=[
                                            ft.Text("Robots", size=12,
                                                    color=TEXT_DIM,
                                                    weight=ft.FontWeight.W_600),
                                            stat_card("Active", robots_a_ref),
                                            stat_card("Idle",   robots_i_ref),
                                        ]),

                                        # --- Performance column ---
                                        ft.Column(spacing=8, controls=[
                                            ft.Text("Performance", size=12,
                                                    color=TEXT_DIM,
                                                    weight=ft.FontWeight.W_600),
                                            stat_card("Avg Delivery Time",
                                                      avg_time_ref),
                                        ]),
                                    ],
                                ),
                            ],
                        ),
                    ),
                ],
            ),
        ],
    )

# ===========================================================================
# VIEW 2 — Analysis Board (DuckDB-backed)
# ===========================================================================

def build_analysis_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Flexible analysis board backed by DuckDB.

    Left panel: checkboxes to select simulations, radio buttons for X axis,
                checkboxes for Y metrics. Update Chart button triggers export
                + query + render in a background thread.

    Right area:  single chart that changes type based on X axis selection:
                   - Sim Hour  → ft.LineChart (one series per sim × metric)
                   - Simulation → ft.BarChart (grouped bars per metric)
                   - By Robot   → ft.BarChart (grouped bars per sim)

    Bottom:      sortable ft.DataTable of summary statistics.
    """
    import sys
    sys.path.insert(0, '/app')
    import analytics as an

    DB_URL      = os.getenv("DATABASE_URL",
                            "postgresql://robotics_user:robotics_password"
                            "@postgres:5432/robotics_db")
    DUCKDB_PATH = os.getenv("DUCKDB_PATH", "/app/data/analytics.duckdb")
    SIM_COLORS  = [TEAL, ORANGE, "#3498DB", "#9B59B6", "#1ABC9C", "#E67E22"]

    # ---- Y metric definitions keyed by X axis ------------------------------
    # Each entry: (internal_key, display_label)
    Y_DEFS = {
        "sim_hour": [
            ("cumulative_deliveries",  "Cumulative Deliveries"),
            ("deliveries_per_hour",    "Deliveries / Hour"),
            ("delivery_requests_per_hour", "Delivery Requests / Hour"),
            ("avg_delivery_time",      "Avg Delivery Time (min)"),
        ],
        "simulation": [
            ("completed",    "Completed Deliveries"),
            ("rejected",     "Rejected Deliveries"),
            ("avg_time",     "Avg Time (min)"),
            ("avg_distance", "Avg Distance (km)"),
        ],
        "robot": [
            ("utilization", "Robot Utilization %"),
        ],
    }

    # Maps Y key → summary dict column name (for "simulation" X axis)
    SIM_COL = {
        "completed":    "completed",
        "rejected":     "rejected",
        "avg_time":     "avg_delivery_min",
        "avg_distance": "avg_distance_km",
    }

    # ---- Mutable closure state ---------------------------------------------
    _sims     : list = []
    _selected : set  = set()           # sim IDs checked in the panel
    _x_axis   : list = ["sim_hour"]    # one of: sim_hour, simulation, robot
    _y_active : set  = {"cumulative_deliveries"}
    _summary  : list = []              # query_summary_stats result
    _sort_col : list = ["sim_id"]
    _sort_asc : list = [True]

    # ---- UI controls created once ------------------------------------------
    status_lbl     = ft.Text("Loading…", color=TEXT_DIM, size=12, italic=True)
    sim_checks_col = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, height=170)
    y_checks_col   = ft.Column(spacing=4)

    # chart_title is updated by each renderer; chart_slot holds the chart widget.
    # Kept separate so there is no padding/height contention between title + chart.
    chart_title = ft.Text(
        "Select simulations and metrics, then click Update Chart.",
        color=TEXT_DIM, italic=True, size=13,
    )
    # NOTE: height ONLY — no expand=True. A Container with both `expand` and a
    # fixed `height` becomes a flex child; placed inside the expand=True chart
    # column (itself nested in a scroll=AUTO, i.e. unbounded-height, parent)
    # Flutter cannot resolve the vertical constraint and the chart subtree
    # silently fails to lay out — so the PNG renders but never appears. Giving
    # it a plain fixed height matches the working map container in the sim view.
    chart_slot = ft.Container(
        height=380,
        bgcolor=BG_CARD,
        border_radius=10,
        padding=ft.Padding(left=12, right=12, top=8, bottom=8),
    )
    legend_row  = ft.Row(spacing=12, wrap=True)
    table_slot  = ft.Container()
    table_section = ft.Column(
        visible=False, spacing=12,
        controls=[
            ft.Text("Summary Statistics", size=18,
                    weight=ft.FontWeight.BOLD, color=TEAL),
            ft.Divider(color=BG_SURFACE),
            ft.Text("Click a column header to sort.",
                    color=TEXT_DIM, size=11, italic=True),
            legend_row,
            table_slot,
        ],
    )

    # ---- Small helpers -----------------------------------------------------
    def _panel_label(text: str) -> ft.Text:
        return ft.Text(text, size=10, weight=ft.FontWeight.W_600,
                       color=TEXT_DIM)

    def _make_chart_image(fig) -> ft.Image:
        """Convert a matplotlib Figure to an ft.Image (base64 PNG)."""
        import io, base64
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        buf.seek(0)
        encoded = base64.b64encode(buf.read()).decode()
        import matplotlib.pyplot as plt
        plt.close(fig)
        return ft.Image(src=f"data:image/png;base64,{encoded}", fit="contain", expand=True)

    def _mpl_style(fig, ax):
        """Apply dark theme consistent with the app palette."""
        fig.patch.set_facecolor(BG_CARD)
        ax.set_facecolor(BG_CARD)
        for spine in ax.spines.values():
            spine.set_color(BG_SURFACE)
        ax.tick_params(colors=TEXT_DIM, labelsize=8)
        ax.xaxis.label.set_color(TEXT_DIM)
        ax.yaxis.label.set_color(TEXT_DIM)
        ax.grid(color=BG_SURFACE, linewidth=0.5, alpha=0.6)

    # ---- Simulation list loader --------------------------------------------
    def load_sim_list():
        try:
            resp = httpx.get(f"{API_BASE}/api/v1/simulations", timeout=5)
            _sims.clear()
            _sims.extend(resp.json().get("simulations", []))

            finished  = [s for s in _sims
                         if s.get("status") in ("completed", "stopped", "failed")]
            completed = [s for s in finished if s.get("status") == "completed"]

            sim_checks_col.controls.clear()
            if not finished:
                sim_checks_col.controls.append(
                    ft.Text("No finished simulations yet.",
                            color=TEXT_DIM, size=12, italic=True)
                )
            else:
                for s in finished:
                    sid     = s["id"]
                    aborted = s.get("status") in ("stopped", "failed")
                    if aborted:
                        sim_checks_col.controls.append(ft.Checkbox(
                            label=(f"#{sid}  {s.get('config_name','?')}"
                                   f"  ·  ⚠ aborted"),
                            value=False, disabled=True,
                            label_style=ft.TextStyle(color=TEXT_DIM, size=12),
                        ))
                    else:
                        def _make_toggle(sim_id):
                            def toggle(e):
                                if e.control.value:
                                    _selected.add(sim_id)
                                else:
                                    _selected.discard(sim_id)
                            return toggle
                        sim_checks_col.controls.append(ft.Checkbox(
                            label=(f"#{sid}  {s.get('config_name','?')}"
                                   f"  ·  {s.get('num_robots','?')} robots"
                                   f"  ·  {s.get('duration_hours','?')}h"),
                            value=False,
                            active_color=TEAL,
                            label_style=ft.TextStyle(color=TEXT_MAIN, size=12),
                            on_change=_make_toggle(sid),
                        ))

            nc, na = len(completed), len(finished) - len(completed)
            status_lbl.value = (
                f"{nc} complete"
                + (f"  ·  {na} aborted" if na else "")
                + "."
            )
            status_lbl.color = TEXT_DIM
            page.update()
        except Exception as exc:
            status_lbl.value = f"Could not load: {exc}"
            status_lbl.color = RED
            page.update()

    # ---- Y checkbox rebuilder (called when X axis changes) -----------------
    def _rebuild_y_options():
        valid = {k for k, _ in Y_DEFS[_x_axis[0]]}
        _y_active.intersection_update(valid)
        if not _y_active:
            _y_active.add(Y_DEFS[_x_axis[0]][0][0])

        y_checks_col.controls.clear()
        for key, label in Y_DEFS[_x_axis[0]]:
            def _make_y_toggle(k):
                def toggle(e):
                    if e.control.value:
                        _y_active.add(k)
                    else:
                        _y_active.discard(k)
                return toggle
            y_checks_col.controls.append(ft.Checkbox(
                label=label,
                value=key in _y_active,
                active_color=TEAL,
                label_style=ft.TextStyle(color=TEXT_MAIN, size=12),
                on_change=_make_y_toggle(key),
            ))
        page.update()

    def _on_x_change(e):
        _x_axis[0] = e.control.value
        _rebuild_y_options()

    x_radio_group = ft.RadioGroup(
        value="sim_hour",
        on_change=_on_x_change,
        content=ft.Column(spacing=4, controls=[
            ft.Radio(
                value="sim_hour",
                label="Sim Hour",
                label_style=ft.TextStyle(color=TEXT_MAIN, size=12),
            ),
            ft.Radio(
                value="simulation",
                label="Compare Simulations",
                label_style=ft.TextStyle(color=TEXT_MAIN, size=12),
            ),
            ft.Radio(
                value="robot",
                label="By Robot",
                label_style=ft.TextStyle(color=TEXT_MAIN, size=12),
            ),
        ]),
    )

    # ---- Chart renderers ---------------------------------------------------
    # Each renderer sets chart_title.value and chart_slot.content.

    def _render_time_chart(sim_ids):
        """X = sim hour. One line per (sim × metric) combo — rendered via matplotlib."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from collections import defaultdict

        active = [k for k, _ in Y_DEFS["sim_hour"] if k in _y_active]
        if not active:
            chart_title.value = "Select at least one metric."
            chart_title.color = TEXT_DIM
            chart_slot.content = None
            return

        hourly    = an.query_deliveries_over_time(sim_ids, DUCKDB_PATH)
        timed     = an.query_avg_delivery_time_over_time(sim_ids, DUCKDB_PATH)
        requested = an.query_delivery_requests_over_time(sim_ids, DUCKDB_PATH)

        by_hour = defaultdict(list)
        for r in hourly:
            by_hour[r["sim_id"]].append((r["sim_hour"], r["count"]))
        by_time = defaultdict(list)
        for r in timed:
            by_time[r["sim_id"]].append((r["sim_hour"], r["avg_minutes"]))
        by_requests = defaultdict(list)
        for r in requested:
            by_requests[r["sim_id"]].append((r["sim_hour"], r["count"]))

        lmap = {k: lbl for k, lbl in Y_DEFS["sim_hour"]}
        fig, ax = plt.subplots(figsize=(8, 3.6))
        _mpl_style(fig, ax)
        has_data = False

        for si, sid in enumerate(sim_ids):
            for mi, metric in enumerate(active):
                color = SIM_COLORS[(si * len(active) + mi) % len(SIM_COLORS)]
                label = f"#{sid} · {lmap[metric]}"
                if metric == "cumulative_deliveries":
                    pts = sorted(by_hour.get(sid, []))
                    xs, ys, cum = [], [], 0
                    for h, c in pts:
                        cum += c; xs.append(h); ys.append(cum)
                elif metric == "deliveries_per_hour":
                    pts = sorted(by_hour.get(sid, []))
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                elif metric == "delivery_requests_per_hour":
                    pts = sorted(by_requests.get(sid, []))
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                elif metric == "avg_delivery_time":
                    pts = sorted(by_time.get(sid, []))
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                else:
                    continue
                if xs:
                    ax.plot(xs, ys, color=color, linewidth=2.0, label=label)
                    has_data = True

        if not has_data:
            chart_title.value = "No data found for the selected simulations."
            chart_title.color = TEXT_DIM
            chart_slot.content = None
            plt.close(fig)
            return

        y_title = " / ".join(lmap[m] for m in active)
        ax.set_xlabel("Sim Hour")
        ax.set_ylabel(y_title)
        ax.set_ylim(bottom=0)
        ax.legend(facecolor=BG_SURFACE, edgecolor=BG_SURFACE,
                  labelcolor=TEXT_MAIN, fontsize=8)
        chart_title.value = f"{y_title}  ×  Sim Hour"
        chart_title.color = TEXT_MAIN
        chart_slot.content = _make_chart_image(fig)

    def _render_sim_comparison_chart(sim_ids):
        """X = simulation ID. Grouped bars per metric — rendered via matplotlib."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        active = [k for k, _ in Y_DEFS["simulation"] if k in _y_active]
        if not active:
            chart_title.value = "Select at least one metric."
            chart_title.color = TEXT_DIM
            chart_slot.content = None
            return
        if not _summary:
            chart_title.value = "No summary data yet — click Update Chart."
            chart_title.color = TEXT_DIM
            chart_slot.content = None
            return

        lmap  = {k: lbl for k, lbl in Y_DEFS["simulation"]}
        n_sim = len(_summary)
        n_met = len(active)
        width = 0.7 / n_met
        xs    = np.arange(n_sim)

        fig, ax = plt.subplots(figsize=(max(6, n_sim * 1.4), 3.6))
        _mpl_style(fig, ax)

        for mi, metric in enumerate(active):
            vals  = [float(_summary[i].get(SIM_COL.get(metric, metric)) or 0)
                     for i in range(n_sim)]
            color = SIM_COLORS[mi % len(SIM_COLORS)]
            offset = (mi - n_met / 2 + 0.5) * width
            ax.bar(xs + offset, vals, width=width * 0.9,
                   color=color, label=lmap[metric])

        ax.set_xticks(xs)
        ax.set_xticklabels([f"#{r['sim_id']}" for r in _summary],
                           color=TEXT_DIM, fontsize=8)
        ax.set_ylim(bottom=0)
        ax.legend(facecolor=BG_SURFACE, edgecolor=BG_SURFACE,
                  labelcolor=TEXT_MAIN, fontsize=8)

        y_title = " / ".join(lmap[m] for m in active)
        chart_title.value = f"{y_title}  ×  Simulation"
        chart_title.color = TEXT_MAIN
        chart_slot.content = _make_chart_image(fig)

    def _render_robot_chart(sim_ids):
        """X = robot ID. Grouped bars per sim — rendered via matplotlib."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from collections import defaultdict

        util_data = an.query_robot_utilization(sim_ids, DUCKDB_PATH)
        if not util_data:
            chart_title.value = "No robot location data available."
            chart_title.color = TEXT_DIM
            chart_slot.content = None
            return

        by_util    = defaultdict(dict)
        for r in util_data:
            by_util[r["sim_id"]][r["robot_id"]] = float(r["utilization"] or 0)
        all_robots = sorted({r["robot_id"] for r in util_data})

        n_rob = len(all_robots)
        n_sim = len(sim_ids)
        width = 0.7 / n_sim
        xs    = np.arange(n_rob)

        fig, ax = plt.subplots(figsize=(max(6, n_rob * 1.2), 3.6))
        _mpl_style(fig, ax)

        for i, sid in enumerate(sim_ids):
            vals   = [by_util.get(sid, {}).get(rid, 0) * 100 for rid in all_robots]
            color  = SIM_COLORS[i % len(SIM_COLORS)]
            offset = (i - n_sim / 2 + 0.5) * width
            ax.bar(xs + offset, vals, width=width * 0.9,
                   color=color, label=f"Sim #{sid}")

        ax.set_xticks(xs)
        ax.set_xticklabels([f"#{rid}" for rid in all_robots],
                           color=TEXT_DIM, fontsize=8)
        ax.set_ylim(0, 100)
        ax.set_ylabel("Utilization %")
        ax.legend(facecolor=BG_SURFACE, edgecolor=BG_SURFACE,
                  labelcolor=TEXT_MAIN, fontsize=8)
        chart_title.value = "Robot Utilization %  ×  Robot"
        chart_title.color = TEXT_MAIN
        chart_slot.content = _make_chart_image(fig)

    # ---- Legend builder ----------------------------------------------------
    def _build_legend(sim_ids):
        legend_row.controls.clear()
        x = _x_axis[0]

        if x == "sim_hour":
            # One chip per (sim × metric) combo — matches series color assignment
            active = [k for k, _ in Y_DEFS["sim_hour"] if k in _y_active]
            lmap   = {k: lbl for k, lbl in Y_DEFS["sim_hour"]}
            for si, sid in enumerate(sim_ids):
                for mi, metric in enumerate(active):
                    color = SIM_COLORS[(si * len(active) + mi) % len(SIM_COLORS)]
                    label = f"#{sid} · {lmap[metric]}"
                    legend_row.controls.append(ft.Row(spacing=5, controls=[
                        ft.Container(width=14, height=4, bgcolor=color,
                                     border_radius=2),
                        ft.Text(label, color=TEXT_DIM, size=11),
                    ]))

        elif x == "simulation":
            # One chip per metric (bars within each sim group)
            active = [k for k, _ in Y_DEFS["simulation"] if k in _y_active]
            lmap   = {k: lbl for k, lbl in Y_DEFS["simulation"]}
            for mi, metric in enumerate(active):
                color = SIM_COLORS[mi % len(SIM_COLORS)]
                legend_row.controls.append(ft.Row(spacing=5, controls=[
                    ft.Container(width=14, height=14, bgcolor=color,
                                 border_radius=3),
                    ft.Text(lmap[metric], color=TEXT_DIM, size=11),
                ]))

        elif x == "robot":
            # One chip per sim (bars within each robot group)
            for i, sid in enumerate(sim_ids):
                color = SIM_COLORS[i % len(SIM_COLORS)]
                legend_row.controls.append(ft.Row(spacing=5, controls=[
                    ft.Container(width=14, height=14, bgcolor=color,
                                 border_radius=3),
                    ft.Text(f"Sim #{sid}", color=TEXT_DIM, size=11),
                ]))

    # ---- Summary table builder ---------------------------------------------
    _COL_KEYS = [
        "sim_id", "config_name", "num_robots", "duration_hours",
        "total_deliveries", "completed", "rejected",
        "avg_delivery_min", "median_delivery_min",
        "avg_distance_km", "total_distance_km",
    ]

    def _build_table():
        def fmt(v, dec=1):
            return "—" if v is None else f"{v:.{dec}f}"

        key  = _sort_col[0]
        rows = sorted(
            _summary,
            key=lambda r: (r.get(key) is None, r.get(key) or 0),
            reverse=not _sort_asc[0],
        )

        def on_sort(e):
            _sort_col[0] = _COL_KEYS[e.column_index]
            _sort_asc[0] = e.ascending
            _build_table()
            page.update()

        table_slot.content = ft.DataTable(
            bgcolor=BG_CARD,
            border_radius=8,
            border=ft.Border.all(1, BG_SURFACE),
            heading_row_color=BG_SURFACE,
            data_row_color={"hovered": BG_SURFACE},
            sort_column_index=_COL_KEYS.index(_sort_col[0]),
            sort_ascending=_sort_asc[0],
            columns=[
                ft.DataColumn(ft.Text("Sim",   color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Config", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600)),
                ft.DataColumn(ft.Text("Robots", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Duration (h)", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Total",  color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Completed", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Rejected", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Avg Time (m)", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Median (m)", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Avg Dist (km)", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
                ft.DataColumn(ft.Text("Total Dist (km)", color=TEAL, size=12,
                               weight=ft.FontWeight.W_600), numeric=True),
            ],
            rows=[
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(f"#{r['sim_id']}", color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(r.get("config_name") or "—", color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(str(r.get("num_robots") or "—"), color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(fmt(r.get("duration_hours"), 0), color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(str(r.get("total_deliveries") or 0), color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(str(r.get("completed") or 0), color=GREEN, size=12,
                                       weight=ft.FontWeight.W_500)),
                    ft.DataCell(ft.Text(str(r.get("rejected") or 0), color=RED, size=12,
                                       weight=ft.FontWeight.W_500)),
                    ft.DataCell(ft.Text(fmt(r.get("avg_delivery_min")), color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(fmt(r.get("median_delivery_min")), color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(fmt(r.get("avg_distance_km"), 2), color=TEXT_MAIN, size=12)),
                    ft.DataCell(ft.Text(fmt(r.get("total_distance_km"), 1), color=TEXT_MAIN, size=12)),
                ])
                for r in rows
            ],
        )
        table_slot.content.on_sort = on_sort

    # ---- Main runner (background thread) -----------------------------------
    def run_analysis():
        sim_ids = sorted(_selected)
        if not sim_ids:
            status_lbl.value = "Select at least one simulation."
            status_lbl.color = ORANGE
            page.update()
            return

        status_lbl.color = TEAL
        try:
            for sid in sim_ids:
                if not an.is_exported(sid, DUCKDB_PATH):
                    status_lbl.value = f"Exporting sim #{sid} to DuckDB…"
                    page.update()
                    an.export_simulation(sid, DB_URL, DUCKDB_PATH)

            status_lbl.value = "Querying…"
            page.update()

            _summary.clear()
            _summary.extend(an.query_summary_stats(sim_ids, DUCKDB_PATH))

            x = _x_axis[0]
            if x == "sim_hour":
                _render_time_chart(sim_ids)
            elif x == "simulation":
                _render_sim_comparison_chart(sim_ids)
            elif x == "robot":
                _render_robot_chart(sim_ids)

            _build_legend(sim_ids)
            _build_table()
            table_section.visible = True

            n = len(sim_ids)
            status_lbl.value = (
                f"{n} sim{'s' if n != 1 else ''} loaded."
            )
            status_lbl.color = GREEN

        except Exception as exc:
            import traceback
            logger.error("Analysis error: %s", traceback.format_exc())
            status_lbl.value = f"Error: {exc}"
            status_lbl.color = RED

        page.update()

    # ---- Button handlers ---------------------------------------------------
    def on_refresh(e):
        threading.Thread(target=load_sim_list, daemon=True).start()

    def on_update_chart(e):
        threading.Thread(target=run_analysis, daemon=True).start()

    # Initialise Y checkboxes and kick off the first sim-list load
    _rebuild_y_options()
    threading.Thread(target=load_sim_list, daemon=True).start()

    # ---- Selection panel (left sidebar) ------------------------------------
    selection_panel = ft.Container(
        width=280,
        bgcolor=BG_CARD,
        border_radius=10,
        padding=16,
        content=ft.Column(
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                _panel_label("SIMULATIONS"),
                ft.Container(
                    bgcolor=BG_SURFACE, border_radius=8,
                    padding=ft.Padding(left=8, right=8, top=6, bottom=6),
                    content=sim_checks_col,
                ),
                status_lbl,
                ft.Divider(color=BG_SURFACE, height=1),
                _panel_label("X AXIS"),
                x_radio_group,
                ft.Divider(color=BG_SURFACE, height=1),
                _panel_label("METRICS  (Y AXIS)"),
                y_checks_col,
                ft.Divider(color=BG_SURFACE, height=1),
                ft.Row(spacing=8, controls=[
                    ft.ElevatedButton(
                        "↺",
                        bgcolor=BG_SURFACE, color=TEXT_MAIN,
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8)
                        ),
                        on_click=on_refresh,
                        width=44,
                    ),
                    ft.ElevatedButton(
                        "Update Chart",
                        bgcolor=TEAL, color="#000000",
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8)
                        ),
                        on_click=on_update_chart,
                        expand=True,
                    ),
                ]),
            ],
        ),
    )

    # ---- Top-level layout --------------------------------------------------
    return ft.Column(
        scroll=ft.ScrollMode.AUTO,
        spacing=20,
        expand=True,
        controls=[
            ft.Text("Analysis", size=22, weight=ft.FontWeight.BOLD, color=TEAL),
            ft.Divider(color=BG_SURFACE),

            ft.Row(
                spacing=16,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    selection_panel,
                    ft.Column(
                        expand=True,
                        spacing=8,
                        controls=[chart_title, chart_slot, legend_row],
                    ),
                ],
            ),

            table_section,
        ],
    )

# ===========================================================================
# VIEW 3 — Delivery Log
# ===========================================================================

def build_delivery_log_view(state: AppState, page: ft.Page) -> ft.Column:
    """
    Narrative event feed — shows the full story of each delivery in
    chronological order: request → dispatcher decision → pickup → delivery.
    Rejected requests are interleaved in the same timeline.
    """

    def _sim_time_str(sim_min: float) -> str:
        """Format sim minutes as 't=2h 15m'."""
        h = int(sim_min // 60)
        m = int(sim_min % 60)
        return f"t={h}h {m:02d}m"

    def delivery_card(e: dict) -> ft.Container:
        """Render one delivery as a two-line narrative card."""
        sim_str   = _sim_time_str(e.get("sim_time_min", 0))
        restaurant = e.get("restaurant_name", "Unknown restaurant")
        address    = e.get("residence_address", "Unknown address")
        robot_id   = e.get("robot_id")
        state_key  = e.get("state", "awaiting_robot")
        dur        = e.get("duration_seconds")
        dist       = e.get("distance_km")

        # --- Dispatcher / status line ---
        if state_key == "awaiting_robot":
            status_icon  = "⏳"
            status_color = ORANGE
            status_line  = "Dispatcher queued — awaiting available robot"
        elif state_key == "en_route_restaurant":
            status_icon  = "🤖"
            status_color = TEAL
            status_line  = f"Dispatcher assigned Robot #{robot_id} — en route to restaurant"
        elif state_key == "en_route_residence":
            status_icon  = "📦"
            status_color = TEAL
            status_line  = f"Robot #{robot_id} picked up — en route to residence"
        else:  # delivered
            dur_str     = f"{int(dur//60)}m {int(dur%60)}s" if dur else "—"
            dist_str    = f"{dist:.2f} km" if dist else "—"
            status_icon  = "✓"
            status_color = GREEN
            status_line  = f"Robot #{robot_id} delivered in {dur_str} · {dist_str}"

        return ft.Container(
            padding=ft.Padding(left=14, right=14, top=10, bottom=10),
            bgcolor=BG_SURFACE,
            border_radius=8,
            content=ft.Column(
                spacing=4,
                controls=[
                    # Request line
                    ft.Row(spacing=8, controls=[
                        ft.Text("🔔", size=13),
                        ft.Text(
                            f"{sim_str}  ·  Resident at {address} requested "
                            f"delivery from {restaurant}",
                            color=TEXT_MAIN, size=12,
                            weight=ft.FontWeight.W_500,
                        ),
                    ]),
                    # Dispatcher / status line (indented)
                    ft.Row(spacing=8, controls=[
                        ft.Container(width=20),  # indent
                        ft.Text(f"↳  {status_icon}  {status_line}",
                                color=status_color, size=11),
                    ]),
                ],
            ),
        )

    def rejected_card(e: dict) -> ft.Container:
        """Render one rejected request as a narrative card."""
        sim_str    = _sim_time_str(e.get("sim_time_min", 0))
        restaurant = e.get("restaurant_name", "Unknown restaurant")
        address    = e.get("residence_address", "Unknown address")
        est        = e.get("estimated_minutes", 0)

        return ft.Container(
            padding=ft.Padding(left=14, right=14, top=10, bottom=10),
            bgcolor=BG_SURFACE,
            border_radius=8,
            content=ft.Column(
                spacing=4,
                controls=[
                    ft.Row(spacing=8, controls=[
                        ft.Text("✗", size=13, color=RED),
                        ft.Text(
                            f"{sim_str}  ·  Resident at {address} requested "
                            f"delivery from {restaurant}",
                            color=TEXT_MAIN, size=12,
                            weight=ft.FontWeight.W_500,
                        ),
                    ]),
                    ft.Row(spacing=8, controls=[
                        ft.Container(width=20),
                        ft.Text(
                            f"↳  Dispatcher rejected — ETA {est:.1f} min exceeded limit",
                            color=RED, size=11,
                        ),
                    ]),
                ],
            ),
        )

    log_column = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=520)

    summary_text = ft.Text(
        "Total: 0  |  Completed: 0  |  In Progress: 0  |  Pending: 0  |  Rejected: 0",
        color=TEXT_DIM, size=12,
    )

    def refresh_log():
        s = state.delivery_summary
        summary_text.value = (
            f"Total: {s.get('total',0)}  |  "
            f"Completed: {s.get('completed',0)}  |  "
            f"In Progress: {s.get('in_progress',0)}  |  "
            f"Pending: {s.get('pending',0)}  |  "
            f"Rejected: {s.get('rejected',0)}"
        )

        # Merge delivery events and rejected into one timeline, newest first
        combined = []
        for e in state.delivery_events:
            combined.append(("delivery", e.get("sim_time_min", 0), e))
        for e in state.rejected_events:
            combined.append(("rejected", e.get("sim_time_min", 0), e))
        combined.sort(key=lambda x: x[1], reverse=True)

        log_column.controls.clear()
        if not combined:
            log_column.controls.append(
                ft.Text("No events yet — waiting for simulation…",
                        color=TEXT_DIM, italic=True, size=13)
            )
        else:
            for kind, _, e in combined:
                if kind == "delivery":
                    log_column.controls.append(delivery_card(e))
                else:
                    log_column.controls.append(rejected_card(e))
        page.update()

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
# WebSocket helpers — run in background daemon threads
# ===========================================================================

def _start_ws_threads(state: AppState, page: ft.Page):
    """Spawn two daemon threads: one for robots, one for deliveries."""

    # Bind each thread to the run it was started for. If a newer run begins,
    # state.sim_id changes and these threads exit instead of overwriting the
    # new run's data with stale pushes from the old socket.
    my_sim_id = state.sim_id

    def run_robot_ws():
        import asyncio as _asyncio

        async def _connect():
            uri = f"{WS_BASE}/ws/robots/{my_sim_id}"
            try:
                async with websockets.connect(uri) as ws:
                    async for raw in ws:
                        if not state.is_running or state.sim_id != my_sim_id:
                            break
                        msg = json.loads(raw)
                        if msg.get("simulation_id") not in (None, my_sim_id):
                            continue
                        if msg.get("type") == "robot_update":
                            state.robots = msg.get("robots", [])
                            # Build a fast lookup dict: restaurant_id → pending count
                            state.busy_restaurants = {
                                r["restaurant_id"]: r["pending_count"]
                                for r in msg.get("busy_restaurants", [])
                            }
                            state.active_residences = msg.get("active_residences", [])
                            if state.on_robot_update:
                                state.on_robot_update()
            except Exception:
                pass  # Connection dropped — simulation ended

        _asyncio.run(_connect())

    def run_delivery_ws():
        import asyncio as _asyncio

        async def _connect():
            uri = f"{WS_BASE}/ws/deliveries/{my_sim_id}"
            try:
                async with websockets.connect(uri) as ws:
                    async for raw in ws:
                        if not state.is_running or state.sim_id != my_sim_id:
                            break
                        msg = json.loads(raw)
                        if msg.get("simulation_id") not in (None, my_sim_id):
                            continue
                        if msg.get("type") == "delivery_update":
                            state.delivery_summary = msg.get("summary", {})
                            state.delivery_events  = msg.get("events", [])
                            state.rejected_events  = msg.get("rejected", [])
                            # Event-driven completion: reset buttons the moment
                            # the WS push carries a terminal sim status, rather
                            # than waiting for the next poll cycle.
                            ws_sim_status = msg.get("sim_status", "")
                            if ws_sim_status in ("completed", "failed", "stopped") and state.is_running:
                                state.is_running = False
                                if state.start_btn:
                                    state.start_btn.disabled = False
                                    state.stop_btn.disabled  = True
                                page.update()
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

    # -----------------------------------------------------------------------
    # Startup loading overlay
    # Shown while setup.py is still running (downloading road network, loading
    # restaurants/residences into the DB). Polls /api/v1/restaurants every 3 s
    # and dismisses once data is present. This lets the user open the browser
    # immediately after `docker compose up --build` instead of seeing a blank
    # connection-refused page for several minutes.
    # -----------------------------------------------------------------------
    _overlay_status = ft.Text(
        "Connecting to services…",
        color=TEXT_MAIN, size=15, weight=ft.FontWeight.W_500,
        text_align=ft.TextAlign.CENTER,
    )
    _overlay_detail = ft.Text(
        "",
        color=TEXT_DIM, size=12, italic=True,
        text_align=ft.TextAlign.CENTER,
    )
    _overlay_bar = ft.ProgressBar(
        color=TEAL, bgcolor=BG_SURFACE, width=320,
    )

    _loading_overlay = ft.Container(
        expand=True,
        bgcolor=BG_DARK,
        content=ft.Column(
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=20,
            controls=[
                ft.Icon(ft.Icons.DELIVERY_DINING, color=TEAL, size=64),
                ft.Text(
                    "Serve Robotics Simulation",
                    size=26, color=TEAL, weight=ft.FontWeight.BOLD,
                    text_align=ft.TextAlign.CENTER,
                ),
                _overlay_bar,
                _overlay_status,
                _overlay_detail,
                ft.Container(height=40),   # bottom padding
            ],
        ),
        visible=True,
    )
    page.overlay.append(_loading_overlay)
    page.update()

    def _check_readiness():
        """Poll until setup.py has finished seeding the DB, then dismiss."""
        import time as _time
        _phase = [0]   # 0 = connecting, 1 = setup in progress, 2 = ready

        while True:
            try:
                resp  = httpx.get(f"{API_BASE}/api/v1/restaurants", timeout=5)
                rests = resp.json().get("restaurants", [])
                if rests:
                    # Setup complete — dismiss overlay
                    _loading_overlay.visible = False
                    page.update()
                    return
                else:
                    # API is up but DB is empty — setup.py still running
                    if _phase[0] != 1:
                        _phase[0] = 1
                        _overlay_status.value = "Setting up simulation environment…"
                        _overlay_detail.value = (
                            "Downloading road network and loading location data.\n"
                            "This takes a few minutes on first run."
                        )
                        page.update()
            except Exception:
                # API not yet reachable — container still booting
                if _phase[0] != 0:
                    _phase[0] = 0
                    _overlay_status.value = "Waiting for services to start…"
                    _overlay_detail.value = ""
                    page.update()

            _time.sleep(3)

    threading.Thread(target=_check_readiness, daemon=True).start()

    # Build all views upfront so their polling threads start immediately.
    views = {
        "sim":  build_sim_view(state, page),
        "analysis": build_analysis_view(state, page),
    }

    # Content area — swapped out when nav changes
    content_area = ft.Container(
        expand=True,
        padding=24,
        content=views["sim"],
    )

    def nav_changed(e):
        idx = e.control.selected_index
        key = ["sim", "analysis"][idx]
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
                icon=ft.Icons.PLAY_ARROW_OUTLINED,
                selected_icon=ft.Icons.PLAY_ARROW,
                label="Simulation",
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.BAR_CHART_OUTLINED,
                selected_icon=ft.Icons.BAR_CHART,
                label="Analysis",
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
