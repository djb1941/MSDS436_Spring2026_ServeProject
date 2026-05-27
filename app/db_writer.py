"""
SimulationDatabaseWriter — buffers robot location writes and persists
simulation lifecycle events (deliveries, simulation status) to PostgreSQL.

Write paths:
  - Robot locations  : buffered, flushed every 5s OR when buffer hits 100 rows
  - Delivery events  : immediate (low-frequency, discrete)
  - Simulation row   : written at startup, updated at shutdown
"""

import logging
import threading
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Flush the location buffer when either limit is hit first
BUFFER_MAX_ROWS = 100
BUFFER_MAX_SECONDS = 5


class SimulationDatabaseWriter:
    def __init__(self, db_url: str, sim_id: int, num_robots: int):
        """
        Connect to PostgreSQL using an already-created simulation row.

        The simulation row is created by the web-api when the user presses
        Start; this writer just uses that existing id and pre-inserts robots.
        """
        self.db_url = db_url
        self.conn = psycopg2.connect(db_url)
        self.conn.autocommit = False

        self._location_buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        self._last_flush_time = time.monotonic()

        self.simulation_id = sim_id
        logger.info(f"Simulation registered with id={self.simulation_id}")

        self.robot_db_ids: dict[int, int] = {}
        self._register_robots(num_robots)

        self._stop_event = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="db-flush"
        )
        self._flush_thread.start()

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------

    def _register_robots(self, num_robots: int):
        """
        Insert one row per robot into robotics.robots.

        Robots start at a fixed depot in Glendale (Glendale Police Dept).
        home_location is stored as a PostGIS Point (lon, lat order for ST_MakePoint).
        """
        DEPOT_LAT = 34.1478
        DEPOT_LON = -118.2551

        with self.conn.cursor() as cur:
            for i in range(num_robots):
                robot_label = f"SIM{self.simulation_id}-R{i+1}"
                cur.execute(
                    """
                    INSERT INTO robotics.robots
                        (robot_id, home_location, status)
                    VALUES (
                        %s,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                        'idle'
                    )
                    RETURNING id
                    """,
                    (robot_label, DEPOT_LON, DEPOT_LAT),
                )
                db_id = cur.fetchone()[0]
                self.robot_db_ids[i] = db_id  # robot index 0..N-1 → DB id
        self.conn.commit()
        logger.info(f"Registered {num_robots} robots: {self.robot_db_ids}")

    # ------------------------------------------------------------------
    # Location logging (buffered)
    # ------------------------------------------------------------------

    def log_movement(
        self,
        robot_index: int,
        lat: float,
        lon: float,
        sim_timestamp: float,
        status: str,
        delivery_id: int | None = None,
    ):
        """
        Queue a robot position for writing.

        robot_index   — 0-based index assigned by the SimPy engine
        sim_timestamp — SimPy env.now (seconds since simulation start)
        status        — 'traveling' | 'at_restaurant' | 'at_residence' | 'idle'
        """
        row = {
            "simulation_id": self.simulation_id,
            "robot_id": self.robot_db_ids[robot_index],
            "lon": lon,
            "lat": lat,
            "timestamp": datetime.fromtimestamp(sim_timestamp, tz=timezone.utc),
            "status": status,
            "delivery_id": delivery_id,
        }
        with self._buffer_lock:
            self._location_buffer.append(row)
            should_flush = len(self._location_buffer) >= BUFFER_MAX_ROWS

        if should_flush:
            self._flush()

    # ------------------------------------------------------------------
    # Delivery lifecycle (immediate writes)
    # ------------------------------------------------------------------

    def create_delivery(
        self,
        robot_index: int,
        restaurant_id: int,
        residence_id: int,
        sim_timestamp: float,
    ) -> int:
        """
        Insert a new delivery row and return its DB id.
        Called when the SimPy engine generates a delivery request.
        """
        requested_at = datetime.fromtimestamp(sim_timestamp, tz=timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO robotics.deliveries
                    (simulation_id, robot_id, restaurant_id, residence_id, requested_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.simulation_id,
                    self.robot_db_ids[robot_index],
                    restaurant_id,
                    residence_id,
                    requested_at,
                ),
            )
            delivery_id = cur.fetchone()[0]
        self.conn.commit()
        return delivery_id

    def claim_delivery(self, delivery_id: int, robot_index: int):
        """
        Set robot_id on a delivery that was written at generation time.
        Called when a robot takes the request from the dispatcher.
        """
        robot_id = self.robot_db_ids[robot_index]
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE robotics.deliveries SET robot_id = %s WHERE id = %s",
                (robot_id, delivery_id),
            )
        self.conn.commit()

    def mark_picked_up(self, delivery_id: int, sim_timestamp: float):
        """Record the moment a robot picks up an order at the restaurant."""
        picked_up_at = datetime.fromtimestamp(sim_timestamp, tz=timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE robotics.deliveries SET picked_up_at = %s WHERE id = %s",
                (picked_up_at, delivery_id),
            )
        self.conn.commit()

    def mark_delivered(
        self,
        delivery_id: int,
        sim_timestamp: float,
        distance_km: float,
        duration_seconds: int,
    ):
        """Record delivery completion with final distance and duration."""
        delivered_at = datetime.fromtimestamp(sim_timestamp, tz=timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE robotics.deliveries
                SET delivered_at = %s, distance_km = %s, duration_seconds = %s
                WHERE id = %s
                """,
                (delivered_at, distance_km, duration_seconds, delivery_id),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Route tracking (upserted per leg, cleared on idle)
    # ------------------------------------------------------------------

    def update_route(
        self,
        robot_index: int,
        leg_type: str,
        waypoints: list[tuple[float, float]],
        leg_started_at_sim_s: float,
    ):
        """
        Upsert the robot's current planned route into active_routes.

        robot_index          — 0-based sim index
        leg_type             — 'to_restaurant' or 'to_residence'
        waypoints            — list of (lat, lon) tuples for each node along the path
        leg_started_at_sim_s — sim time in seconds when this leg began; the
                               WebSocket uses this to compute how far along the
                               route the robot currently is (elapsed * speed).
        """
        import json
        robot_id = self.robot_db_ids[robot_index]
        coords = [[lat, lon] for lat, lon in waypoints]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO robotics.active_routes
                    (robot_id, simulation_id, leg_type, route_coords,
                     leg_started_at_sim_s, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (robot_id) DO UPDATE
                    SET simulation_id        = EXCLUDED.simulation_id,
                        leg_type             = EXCLUDED.leg_type,
                        route_coords         = EXCLUDED.route_coords,
                        leg_started_at_sim_s = EXCLUDED.leg_started_at_sim_s,
                        updated_at           = NOW()
                """,
                (robot_id, self.simulation_id, leg_type,
                 json.dumps(coords), leg_started_at_sim_s),
            )
        self.conn.commit()

    def update_sim_time(self, sim_time_s: float):
        """
        Write the simulation's current clock to the DB so the WebSocket can
        interpolate robot positions along their routes without the sim engine
        needing to emit a position row on every tick.
        Called by a lightweight SimPy process every ~1 sim-minute.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE robotics.simulations SET current_sim_time_s = %s WHERE id = %s",
                (sim_time_s, self.simulation_id),
            )
        self.conn.commit()

    def clear_route(self, robot_index: int):
        """Remove the active route row when a robot goes idle."""
        robot_id = self.robot_db_ids[robot_index]
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM robotics.active_routes WHERE robot_id = %s",
                (robot_id,),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self, total_deliveries: int, total_distance_km: float, stopped: bool = False):
        """
        Flush remaining buffered locations, mark the simulation completed (or
        stopped), and close the DB connection.
        """
        self._stop_event.set()
        self._flush_thread.join(timeout=10)

        self._flush()

        final_status = "stopped" if stopped else "completed"
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE robotics.simulations
                SET completed_at = NOW(),
                    status = %s,
                    total_deliveries = %s,
                    total_distance_km = %s
                WHERE id = %s
                """,
                (final_status, total_deliveries, total_distance_km, self.simulation_id),
            )
        self.conn.commit()
        self.conn.close()
        logger.info(
            f"Simulation {self.simulation_id} finalized: "
            f"{total_deliveries} deliveries, {total_distance_km:.2f} km"
        )

    # ------------------------------------------------------------------
    # Internal flush logic
    # ------------------------------------------------------------------

    def _flush(self):
        """Write buffered location rows to PostgreSQL using executemany."""
        with self._buffer_lock:
            if not self._location_buffer:
                return
            rows_to_write = self._location_buffer.copy()
            self._location_buffer.clear()
            self._last_flush_time = time.monotonic()

        data = [
            (
                r["simulation_id"],
                r["robot_id"],
                r["lon"],
                r["lat"],
                r["timestamp"],
                r["status"],
                r["delivery_id"],
            )
            for r in rows_to_write
        ]

        try:
            with self.conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO robotics.robot_locations
                        (simulation_id, robot_id, location, timestamp, status, delivery_id)
                    VALUES %s
                    """,
                    [
                        (
                            sim_id,
                            robot_id,
                            f"SRID=4326;POINT({lon} {lat})",
                            ts,
                            status,
                            delivery_id,
                        )
                        for sim_id, robot_id, lon, lat, ts, status, delivery_id in data
                    ],
                )
            self.conn.commit()
            logger.debug(f"Flushed {len(rows_to_write)} location rows to DB")
        except Exception as e:
            logger.error(f"Location flush failed: {e}")
            self.conn.rollback()

    def _flush_loop(self):
        """Background thread: flush every BUFFER_MAX_SECONDS wall-clock seconds."""
        while not self._stop_event.is_set():
            time.sleep(1)  # check every second
            elapsed = time.monotonic() - self._last_flush_time
            if elapsed >= BUFFER_MAX_SECONDS:
                self._flush()
