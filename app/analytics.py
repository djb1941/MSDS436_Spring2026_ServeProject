"""
DuckDB analytics layer for completed simulation exports.

Architecture
------------
- PostgreSQL (MobilityDB/PostGIS) is the live transactional store.
- DuckDB (file at DUCKDB_PATH) is the analytical store.
- export_simulation(sim_id, db_url) pulls one completed simulation from
  Postgres into DuckDB — safe to re-run, deletes and reinserts by sim_id.
- query_* functions return lists of dicts suitable for chart/table rendering.

Usage from the Flet frontend (runs inside the same container as the sim engine):
    import sys; sys.path.insert(0, '/app')
    import analytics as an
    an.export_simulation(sim_id, db_url)
    rows = an.query_summary_stats([sim_id])
"""

import logging
import os
from typing import Optional

import duckdb
import psycopg2

logger = logging.getLogger(__name__)

DUCKDB_PATH = os.getenv("DUCKDB_PATH", "/app/data/analytics.duckdb")


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_con(duckdb_path: str = DUCKDB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the spatial extension loaded."""
    con = duckdb.connect(duckdb_path, read_only=read_only)
    try:
        con.execute("LOAD spatial;")
    except Exception:
        con.execute("INSTALL spatial; LOAD spatial;")
    return con


def _pg_conn(db_url: str) -> psycopg2.extensions.connection:
    return psycopg2.connect(db_url)


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_EXPECTED_COLS: dict[str, set[str]] = {
    "simulations":        {"id", "config_name", "num_robots", "duration_hours",
                           "started_at", "completed_at", "total_deliveries",
                           "total_distance_km", "status"},
    "restaurants":        {"id", "name", "address", "lat", "lon", "geom", "created_at"},
    "residences":         {"id", "address", "lat", "lon", "geom", "created_at"},
    "robots":             {"id", "robot_id", "home_lat", "home_lon", "home_geom",
                           "status", "battery_level", "created_at", "updated_at"},
    "deliveries":         {"id", "simulation_id", "robot_id", "restaurant_id",
                           "residence_id", "requested_at", "picked_up_at", "delivered_at",
                           "distance_km", "duration_seconds", "created_at"},
    "rejected_deliveries":{"id", "simulation_id", "restaurant_id", "residence_id",
                           "requested_at", "estimated_minutes", "reason"},
    "robot_locations":    {"id", "simulation_id", "robot_id", "lat", "lon", "geom",
                           "timestamp", "status", "delivery_id", "speed_mps",
                           "heading_degrees"},
}


def _init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create analytics tables, dropping any whose schema is stale."""
    for table, required in _EXPECTED_COLS.items():
        try:
            existing = {
                row[0]
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = ?",
                    [table],
                ).fetchall()
            }
            if existing and not required.issubset(existing):
                logger.warning("Table '%s' has stale schema — dropping for rebuild", table)
                con.execute(f"DROP TABLE IF EXISTS {table}")
        except Exception:
            pass

    con.execute("""
        CREATE TABLE IF NOT EXISTS simulations (
            id               INTEGER PRIMARY KEY,
            config_name      VARCHAR,
            num_robots       INTEGER,
            duration_hours   DOUBLE,
            started_at       TIMESTAMP,
            completed_at     TIMESTAMP,
            total_deliveries INTEGER,
            total_distance_km DOUBLE,
            status           VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            id         INTEGER PRIMARY KEY,
            name       VARCHAR,
            address    VARCHAR,
            lat        DOUBLE,
            lon        DOUBLE,
            geom       GEOMETRY,
            created_at TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS residences (
            id         INTEGER PRIMARY KEY,
            address    VARCHAR,
            lat        DOUBLE,
            lon        DOUBLE,
            geom       GEOMETRY,
            created_at TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS robots (
            id             INTEGER PRIMARY KEY,
            robot_id       VARCHAR,
            home_lat       DOUBLE,
            home_lon       DOUBLE,
            home_geom      GEOMETRY,
            status         VARCHAR,
            battery_level  DOUBLE,
            created_at     TIMESTAMP,
            updated_at     TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id               INTEGER PRIMARY KEY,
            simulation_id    INTEGER,
            robot_id         INTEGER,
            restaurant_id    INTEGER,
            residence_id     INTEGER,
            requested_at     TIMESTAMP,
            picked_up_at     TIMESTAMP,
            delivered_at     TIMESTAMP,
            distance_km      DOUBLE,
            duration_seconds INTEGER,
            created_at       TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS rejected_deliveries (
            id                INTEGER PRIMARY KEY,
            simulation_id     INTEGER,
            restaurant_id     INTEGER,
            residence_id      INTEGER,
            requested_at      TIMESTAMP,
            estimated_minutes DOUBLE,
            reason            VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS robot_locations (
            id              INTEGER PRIMARY KEY,
            simulation_id   INTEGER,
            robot_id        INTEGER,
            lat             DOUBLE,
            lon             DOUBLE,
            geom            GEOMETRY,
            timestamp       TIMESTAMP,
            status          VARCHAR,
            delivery_id     INTEGER,
            speed_mps       DOUBLE,
            heading_degrees DOUBLE
        )
    """)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_simulation(sim_id: int, db_url: str, duckdb_path: str = DUCKDB_PATH) -> None:
    """
    Pull one completed simulation from PostgreSQL into DuckDB.

    Deletes any existing rows for this sim_id before reinserting, so it is
    safe to re-run after additional data has been written (e.g. if the sim
    was re-exported mid-run by mistake).

    Static tables (restaurants, residences, robots) are only populated on
    the first call; subsequent calls skip them to avoid duplicates.
    """
    logger.info("Exporting simulation %d to DuckDB at %s", sim_id, duckdb_path)
    os.makedirs(os.path.dirname(duckdb_path), exist_ok=True)

    pg  = _pg_conn(db_url)
    con = _get_con(duckdb_path, read_only=False)

    try:
        _init_schema(con)

        # --- simulations row ---
        with pg.cursor() as cur:
            cur.execute("""
                SELECT id, config_name, num_robots, duration_hours,
                       started_at, completed_at, total_deliveries,
                       total_distance_km, status
                FROM robotics.simulations WHERE id = %s
            """, (sim_id,))
            rows = cur.fetchall()
        con.execute("DELETE FROM simulations WHERE id = ?", (sim_id,))
        if rows:
            con.executemany("INSERT OR REPLACE INTO simulations VALUES (?,?,?,?,?,?,?,?,?)", rows)
        logger.info("Exported simulation row")

        # --- restaurants (static — skip if already populated) ---
        if con.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 0:
            with pg.cursor() as cur:
                cur.execute("""
                    SELECT id, name, address,
                           ST_Y(location) AS lat, ST_X(location) AS lon,
                           ST_AsBinary(location), created_at
                    FROM robotics.restaurants
                """)
                rows = cur.fetchall()
            if rows:
                con.executemany(
                    "INSERT OR REPLACE INTO restaurants VALUES (?,?,?,?,?,ST_GeomFromWKB(?),?)",
                    rows,
                )
            logger.info("Exported %d restaurants", len(rows))

        # --- residences (static) ---
        if con.execute("SELECT COUNT(*) FROM residences").fetchone()[0] == 0:
            with pg.cursor() as cur:
                cur.execute("""
                    SELECT id, address,
                           ST_Y(location) AS lat, ST_X(location) AS lon,
                           ST_AsBinary(location), created_at
                    FROM robotics.residences
                """)
                rows = cur.fetchall()
            if rows:
                con.executemany(
                    "INSERT OR REPLACE INTO residences VALUES (?,?,?,?,ST_GeomFromWKB(?),?)",
                    rows,
                )
            logger.info("Exported %d residences", len(rows))

        # --- robots (static) ---
        if con.execute("SELECT COUNT(*) FROM robots").fetchone()[0] == 0:
            with pg.cursor() as cur:
                cur.execute("""
                    SELECT id, robot_id,
                           ST_Y(home_location) AS lat, ST_X(home_location) AS lon,
                           ST_AsBinary(home_location),
                           status, battery_level, created_at, updated_at
                    FROM robotics.robots
                """)
                rows = cur.fetchall()
            if rows:
                con.executemany(
                    "INSERT OR REPLACE INTO robots VALUES (?,?,?,?,ST_GeomFromWKB(?),?,?,?,?)",
                    rows,
                )
            logger.info("Exported %d robots", len(rows))

        # --- deliveries ---
        with pg.cursor() as cur:
            cur.execute("""
                SELECT id, simulation_id, robot_id, restaurant_id, residence_id,
                       requested_at, picked_up_at, delivered_at,
                       distance_km, duration_seconds, created_at
                FROM robotics.deliveries
                WHERE simulation_id = %s
            """, (sim_id,))
            rows = cur.fetchall()
        con.execute("DELETE FROM deliveries WHERE simulation_id = ?", (sim_id,))
        if rows:
            con.executemany("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        logger.info("Exported %d deliveries", len(rows))

        # --- rejected_deliveries ---
        with pg.cursor() as cur:
            cur.execute("""
                SELECT id, simulation_id, restaurant_id, residence_id,
                       requested_at, estimated_minutes, reason
                FROM robotics.rejected_deliveries
                WHERE simulation_id = %s
            """, (sim_id,))
            rows = cur.fetchall()
        con.execute("DELETE FROM rejected_deliveries WHERE simulation_id = ?", (sim_id,))
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO rejected_deliveries VALUES (?,?,?,?,?,?,?)",
                rows,
            )
        logger.info("Exported %d rejected deliveries", len(rows))

        # --- robot_locations ---
        with pg.cursor() as cur:
            cur.execute("""
                SELECT id, simulation_id, robot_id,
                       ST_Y(location) AS lat, ST_X(location) AS lon,
                       ST_AsBinary(location),
                       timestamp, status, delivery_id, speed_mps, heading_degrees
                FROM robotics.robot_locations
                WHERE simulation_id = %s
                ORDER BY robot_id, timestamp
            """, (sim_id,))
            rows = cur.fetchall()
        con.execute("DELETE FROM robot_locations WHERE simulation_id = ?", (sim_id,))
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO robot_locations VALUES (?,?,?,?,?,ST_GeomFromWKB(?),?,?,?,?,?)",
                rows,
            )
        logger.info("Exported %d robot location records", len(rows))

        con.commit()
        logger.info("DuckDB export complete for simulation %d", sim_id)

    finally:
        pg.close()
        con.close()


def reset_store(duckdb_path: str = DUCKDB_PATH) -> None:
    """
    Drop every analytics table so the next export rebuilds them empty.

    Call this when the upstream PostgreSQL database has been reinitialised
    (fresh/wiped volume). Postgres primary keys are SERIAL sequences that
    restart from 1 on a new database incarnation, while this DuckDB file is
    on its own persistent volume and retains rows from the previous one.
    Without a reset, the restarted ids collide with those stale rows on the
    INTEGER PRIMARY KEY (the "Duplicate key" export failures).
    """
    if not os.path.exists(duckdb_path):
        return
    con = _get_con(duckdb_path, read_only=False)
    try:
        for table in _EXPECTED_COLS:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.commit()
        logger.info("Analytics store reset — dropped %d tables", len(_EXPECTED_COLS))
    finally:
        con.close()


def is_exported(sim_id: int, duckdb_path: str = DUCKDB_PATH) -> bool:
    """Return True if this simulation has already been exported to DuckDB."""
    if not os.path.exists(duckdb_path):
        return False
    try:
        con = _get_con(duckdb_path, read_only=False)
        try:
            _init_schema(con)
            count = con.execute(
                "SELECT COUNT(*) FROM simulations WHERE id = ?", (sim_id,)
            ).fetchone()[0]
            return count > 0
        finally:
            con.close()
    except Exception as exc:
        logger.warning("is_exported check failed for sim %d: %s", sim_id, exc)
        return False


# ---------------------------------------------------------------------------
# Query functions — all return list[dict], safe for closed-connection use
# ---------------------------------------------------------------------------

def query_simulations_list(duckdb_path: str = DUCKDB_PATH) -> list[dict]:
    """
    Return all simulations stored in DuckDB with delivery counts.
    Used to populate the analysis view selector.
    """
    if not os.path.exists(duckdb_path):
        return []
    con = _get_con(duckdb_path, read_only=False)
    try:
        _init_schema(con)
        rows = con.execute("""
            SELECT
                s.id,
                s.config_name,
                s.num_robots,
                s.duration_hours,
                s.started_at,
                s.completed_at,
                s.status,
                COUNT(DISTINCT d.id)  AS delivery_count,
                COUNT(DISTINCT r.id)  AS rejected_count
            FROM simulations s
            LEFT JOIN deliveries d          ON d.simulation_id = s.id
            LEFT JOIN rejected_deliveries r ON r.simulation_id = s.id
            GROUP BY s.id, s.config_name, s.num_robots, s.duration_hours,
                     s.started_at, s.completed_at, s.status
            ORDER BY s.id DESC
        """).fetchall()
        cols = ["id", "config_name", "num_robots", "duration_hours", "started_at",
                "completed_at", "status", "delivery_count", "rejected_count"]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        con.close()


def query_summary_stats(sim_ids: list[int], duckdb_path: str = DUCKDB_PATH) -> list[dict]:
    """
    Per-simulation summary statistics for the analysis table.

    Returns one dict per sim with: total_deliveries, completed, rejected,
    avg/median/min/max delivery time (minutes), avg/total distance (km).
    """
    if not sim_ids:
        return []
    placeholders = ",".join("?" * len(sim_ids))
    con = _get_con(duckdb_path, read_only=False)
    try:
        _init_schema(con)
        rows = con.execute(f"""
            SELECT
                s.id,
                s.config_name,
                s.num_robots,
                s.duration_hours,
                COUNT(d.id)                                                   AS total_deliveries,
                SUM(CASE WHEN d.delivered_at IS NOT NULL THEN 1 ELSE 0 END)   AS completed,
                (SELECT COUNT(*) FROM rejected_deliveries r
                 WHERE r.simulation_id = s.id)                                AS rejected,
                AVG(d.duration_seconds) / 60.0                                AS avg_delivery_min,
                MEDIAN(d.duration_seconds) / 60.0                             AS median_delivery_min,
                MIN(d.duration_seconds) / 60.0                                AS min_delivery_min,
                MAX(d.duration_seconds) / 60.0                                AS max_delivery_min,
                AVG(d.distance_km)                                            AS avg_distance_km,
                SUM(d.distance_km)                                            AS total_distance_km
            FROM simulations s
            LEFT JOIN deliveries d ON d.simulation_id = s.id
            WHERE s.id IN ({placeholders})
            GROUP BY s.id, s.config_name, s.num_robots, s.duration_hours
            ORDER BY s.id
        """, sim_ids).fetchall()

        cols = [
            "sim_id", "config_name", "num_robots", "duration_hours",
            "total_deliveries", "completed", "rejected",
            "avg_delivery_min", "median_delivery_min",
            "min_delivery_min", "max_delivery_min",
            "avg_distance_km", "total_distance_km",
        ]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        con.close()


def query_deliveries_over_time(
    sim_ids: list[int], duckdb_path: str = DUCKDB_PATH
) -> list[dict]:
    """
    Hourly completed delivery counts per simulation (relative to sim start).

    Returns list of {sim_id, sim_hour, count} — one row per (sim, hour) pair
    where at least one delivery was completed that hour. Caller is responsible
    for filling gaps and computing cumulative sums.
    """
    if not sim_ids:
        return []
    placeholders = ",".join("?" * len(sim_ids))
    con = _get_con(duckdb_path, read_only=False)
    try:
        _init_schema(con)
        rows = con.execute(f"""
            SELECT
                d.simulation_id,
                FLOOR(epoch(d.delivered_at) / 3600.0)::INTEGER AS sim_hour,
                COUNT(*)   AS count
            FROM deliveries d
            WHERE d.simulation_id IN ({placeholders})
              AND d.delivered_at IS NOT NULL
            GROUP BY d.simulation_id, sim_hour
            ORDER BY d.simulation_id, sim_hour
        """, sim_ids).fetchall()
        return [{"sim_id": r[0], "sim_hour": int(r[1]), "count": int(r[2])} for r in rows]
    finally:
        con.close()


def query_avg_delivery_time_over_time(
    sim_ids: list[int], duckdb_path: str = DUCKDB_PATH
) -> list[dict]:
    """
    Hourly average delivery time (minutes) per simulation.

    Returns list of {sim_id, sim_hour, avg_minutes}.
    """
    if not sim_ids:
        return []
    placeholders = ",".join("?" * len(sim_ids))
    con = _get_con(duckdb_path, read_only=False)
    try:
        _init_schema(con)
        rows = con.execute(f"""
            SELECT
                d.simulation_id,
                FLOOR(epoch(d.delivered_at) / 3600.0)::INTEGER AS sim_hour,
                AVG(d.duration_seconds) / 60.0 AS avg_minutes
            FROM deliveries d
            WHERE d.simulation_id IN ({placeholders})
              AND d.delivered_at     IS NOT NULL
              AND d.duration_seconds IS NOT NULL
            GROUP BY d.simulation_id, sim_hour
            ORDER BY d.simulation_id, sim_hour
        """, sim_ids).fetchall()
        return [
            {"sim_id": r[0], "sim_hour": int(r[1]), "avg_minutes": float(r[2] or 0)}
            for r in rows
        ]
    finally:
        con.close()


def query_robot_utilization(
    sim_ids: list[int], duckdb_path: str = DUCKDB_PATH
) -> list[dict]:
    """
    Fraction of location samples where each robot was not idle, per simulation.

    Returns list of {sim_id, robot_id, utilization} where utilization is 0–1.
    Robot IDs are integer foreign keys; the frontend can join to the robots
    table for display names if needed.
    """
    if not sim_ids:
        return []
    placeholders = ",".join("?" * len(sim_ids))
    con = _get_con(duckdb_path, read_only=False)
    try:
        _init_schema(con)
        rows = con.execute(f"""
            SELECT
                rl.simulation_id,
                rl.robot_id,
                SUM(CASE WHEN rl.status != 'idle' THEN 1 ELSE 0 END) * 1.0
                    / NULLIF(COUNT(*), 0) AS utilization
            FROM robot_locations rl
            WHERE rl.simulation_id IN ({placeholders})
            GROUP BY rl.simulation_id, rl.robot_id
            ORDER BY rl.simulation_id, rl.robot_id
        """, sim_ids).fetchall()
        return [
            {"sim_id": r[0], "robot_id": r[1], "utilization": float(r[2] or 0)}
            for r in rows
        ]
    finally:
        con.close()


def query_delivery_requests_over_time(
    sim_ids: list[int], duckdb_path: str = DUCKDB_PATH
) -> list[dict]:
    """
    Hourly delivery request counts per simulation (completed + rejected combined).

    Returns list of {sim_id, sim_hour, count} — one row per (sim, hour) pair.
    requested_at is stored as datetime.fromtimestamp(simpy_now), so epoch()
    gives the raw SimPy seconds directly.
    """
    if not sim_ids:
        return []
    placeholders = ",".join("?" * len(sim_ids))
    con = _get_con(duckdb_path, read_only=False)
    try:
        _init_schema(con)
        rows = con.execute(f"""
            SELECT
                simulation_id,
                FLOOR(epoch(requested_at) / 3600.0)::INTEGER AS sim_hour,
                COUNT(*) AS count
            FROM (
                SELECT simulation_id, requested_at FROM deliveries
                WHERE simulation_id IN ({placeholders})
                  AND requested_at IS NOT NULL
                UNION ALL
                SELECT simulation_id, requested_at FROM rejected_deliveries
                WHERE simulation_id IN ({placeholders})
                  AND requested_at IS NOT NULL
            ) combined
            GROUP BY simulation_id, sim_hour
            ORDER BY simulation_id, sim_hour
        """, sim_ids + sim_ids).fetchall()
        return [{"sim_id": r[0], "sim_hour": int(r[1]), "count": int(r[2])} for r in rows]
    finally:
        con.close()


def query_delivery_distance_buckets(
    sim_ids: list[int], duckdb_path: str = DUCKDB_PATH
) -> list[dict]:
    """
    Delivery count bucketed by distance for each simulation.

    Buckets: <0.5 km, 0.5–1 km, 1–2 km, >2 km.
    Returns list of {sim_id, bucket, count}.
    """
    if not sim_ids:
        return []
    placeholders = ",".join("?" * len(sim_ids))
    con = _get_con(duckdb_path, read_only=False)
    try:
        _init_schema(con)
        rows = con.execute(f"""
            SELECT
                simulation_id,
                CASE
                    WHEN distance_km < 0.5  THEN '< 0.5 km'
                    WHEN distance_km < 1.0  THEN '0.5–1 km'
                    WHEN distance_km < 2.0  THEN '1–2 km'
                    ELSE                         '> 2 km'
                END AS bucket,
                COUNT(*) AS count
            FROM deliveries
            WHERE simulation_id IN ({placeholders})
              AND distance_km IS NOT NULL
            GROUP BY simulation_id, bucket
            ORDER BY simulation_id, MIN(distance_km)
        """, sim_ids).fetchall()
        return [{"sim_id": r[0], "bucket": r[1], "count": int(r[2])} for r in rows]
    finally:
        con.close()
