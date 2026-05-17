"""
DuckDB analytics layer for completed simulation exports.

Pulls data from PostgreSQL/PostGIS into a local DuckDB file with the spatial
extension loaded, enabling fast columnar analytical queries and spatial joins
over completed simulation runs.
"""

import logging
import os
from typing import Optional

import duckdb
import psycopg2

logger = logging.getLogger(__name__)

DUCKDB_PATH = os.getenv("DUCKDB_PATH", "/app/data/analytics.duckdb")


def _pg_conn(db_url: str) -> psycopg2.extensions.connection:
    return psycopg2.connect(db_url)


def _init_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    """Install and load the spatial extension, then create analytics tables."""
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            id          INTEGER PRIMARY KEY,
            name        VARCHAR,
            address     VARCHAR,
            lat         DOUBLE,
            lon         DOUBLE,
            geom        GEOMETRY,
            created_at  TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS residences (
            id          INTEGER PRIMARY KEY,
            address     VARCHAR,
            lat         DOUBLE,
            lon         DOUBLE,
            geom        GEOMETRY,
            created_at  TIMESTAMP
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
            robot_id         INTEGER,
            restaurant_id    INTEGER,
            residence_id     INTEGER,
            start_time       TIMESTAMP,
            end_time         TIMESTAMP,
            status           VARCHAR,
            distance_meters  DOUBLE,
            duration_seconds INTEGER,
            created_at       TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS robot_locations (
            id              INTEGER PRIMARY KEY,
            robot_id        INTEGER,
            lat             DOUBLE,
            lon             DOUBLE,
            geom            GEOMETRY,
            timestamp       TIMESTAMP,
            speed_mps       DOUBLE,
            heading_degrees DOUBLE
        )
    """)


def _fetch_pg(pg: psycopg2.extensions.connection, query: str) -> list:
    with pg.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()


def export_simulation(db_url: str, duckdb_path: str = DUCKDB_PATH) -> None:
    """
    Export all simulation data from PostgreSQL into the local DuckDB file.

    Geometry columns are stored as both lat/lon doubles (for easy aggregation)
    and WKB GEOMETRY (for spatial joins via DuckDB spatial extension).
    Existing rows are replaced so re-running after additional simulation data
    is safe.
    """
    logger.info("Exporting simulation data to DuckDB at %s", duckdb_path)
    os.makedirs(os.path.dirname(duckdb_path), exist_ok=True)

    pg = _pg_conn(db_url)
    con = duckdb.connect(duckdb_path)

    try:
        _init_duckdb(con)

        # --- restaurants ---
        rows = _fetch_pg(pg, """
            SELECT id, name, address,
                   ST_Y(location) AS lat, ST_X(location) AS lon,
                   ST_AsBinary(location),
                   created_at
            FROM robotics.restaurants
        """)
        con.execute("DELETE FROM restaurants")
        if rows:
            con.executemany(
                "INSERT INTO restaurants VALUES (?,?,?,?,?,ST_GeomFromWKB(?),?)",
                rows,
            )
        logger.info("Exported %d restaurants", len(rows))

        # --- residences ---
        rows = _fetch_pg(pg, """
            SELECT id, address,
                   ST_Y(location) AS lat, ST_X(location) AS lon,
                   ST_AsBinary(location),
                   created_at
            FROM robotics.residences
        """)
        con.execute("DELETE FROM residences")
        if rows:
            con.executemany(
                "INSERT INTO residences VALUES (?,?,?,?,ST_GeomFromWKB(?),?)",
                rows,
            )
        logger.info("Exported %d residences", len(rows))

        # --- robots ---
        rows = _fetch_pg(pg, """
            SELECT id, robot_id,
                   ST_Y(home_location) AS lat, ST_X(home_location) AS lon,
                   ST_AsBinary(home_location),
                   status, battery_level, created_at, updated_at
            FROM robotics.robots
        """)
        con.execute("DELETE FROM robots")
        if rows:
            con.executemany(
                "INSERT INTO robots VALUES (?,?,?,?,ST_GeomFromWKB(?),?,?,?,?)",
                rows,
            )
        logger.info("Exported %d robots", len(rows))

        # --- deliveries ---
        rows = _fetch_pg(pg, """
            SELECT id, robot_id, restaurant_id, residence_id,
                   start_time, end_time, status,
                   distance_meters, duration_seconds, created_at
            FROM robotics.deliveries
        """)
        con.execute("DELETE FROM deliveries")
        if rows:
            con.executemany(
                "INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        logger.info("Exported %d deliveries", len(rows))

        # --- robot_locations ---
        rows = _fetch_pg(pg, """
            SELECT id, robot_id,
                   ST_Y(location) AS lat, ST_X(location) AS lon,
                   ST_AsBinary(location),
                   timestamp, speed_mps, heading_degrees
            FROM robotics.robot_locations
            ORDER BY robot_id, timestamp
        """)
        con.execute("DELETE FROM robot_locations")
        if rows:
            con.executemany(
                "INSERT INTO robot_locations VALUES (?,?,?,?,ST_GeomFromWKB(?),?,?,?)",
                rows,
            )
        logger.info("Exported %d robot location records", len(rows))

        con.commit()
        logger.info("DuckDB export complete")

    finally:
        pg.close()
        con.close()


def open_analytics(duckdb_path: str = DUCKDB_PATH) -> duckdb.DuckDBPyConnection:
    """Return an open read-only DuckDB connection to the analytics database."""
    con = duckdb.connect(duckdb_path, read_only=True)
    con.execute("LOAD spatial;")
    return con
