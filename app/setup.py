"""
setup.py — One-time data initialisation for the Serve Robotics Simulation.

Run by start.sh before the simulation engine or Flet UI starts.
Safe to run repeatedly — every step checks whether the work is already done
and skips it if so (idempotent).

What this script does
---------------------
1. Road network
   - Checks for /app/data/glendale_network.graphml on the Docker volume.
   - If absent: downloads the Glendale, CA drive network from OpenStreetMap
     via osmnx and saves it as GraphML.
   - The simulation engine loads this file at runtime to do NetworkX routing.

2. Restaurants (robotics.restaurants)
   - Checks whether the table already has rows.
   - If empty: queries OSM for amenity=restaurant within Glendale and inserts
     each one as a PostGIS Point geometry.

3. Residences (robotics.residences)
   - Same pattern, using building=residential from OSM.
   - OSM often returns building footprints (Polygons) rather than Points, so
     we convert each geometry to its centroid before inserting.

Database connection
-------------------
Reads DATABASE_URL from the environment (set in docker-compose.yml).
Connects directly to PostgreSQL — this is intentional. setup.py is a
backend infrastructure script, not a user-facing operation, so it bypasses
the web-api layer just as a database migration runner would in production.
"""

import logging
import os
import sys

import osmnx as ox
import psycopg2
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [setup] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL   = os.getenv("DATABASE_URL")
NETWORK_PATH   = "/app/data/glendale_network.graphml"
PLACE_NAME     = "Glendale, California, USA"

# OSM network type: "drive" gives us roads robots can travel on.
# "all" would include footpaths and bike lanes — more realistic for sidewalk
# robots but much slower to download and route through.
NETWORK_TYPE   = "drive"

# How many OSM results to cap at. Glendale has ~400 restaurants and ~3,000+
# residential buildings — these limits keep the DB and simulation manageable.
MAX_RESTAURANTS = 500
MAX_RESIDENCES  = 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    """Open a psycopg2 connection using DATABASE_URL from the environment."""
    if not DATABASE_URL:
        log.error("DATABASE_URL environment variable is not set.")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL)


def table_has_rows(conn, schema: str, table: str) -> bool:
    """Return True if the table contains at least one row."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {schema}.{table} LIMIT 1);")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Step 1 — Road network
# ---------------------------------------------------------------------------

def setup_road_network():
    """Download and cache the Glendale drive network as GraphML."""
    if os.path.exists(NETWORK_PATH):
        log.info("Road network already exists at %s — skipping download.", NETWORK_PATH)
        return

    log.info("Downloading Glendale road network from OpenStreetMap …")
    log.info("(This takes 30–90 seconds on first run and is cached to disk.)")

    # osmnx downloads the street graph and adds travel-time edge weights.
    G = ox.graph_from_place(PLACE_NAME, network_type=NETWORK_TYPE)

    # Add travel_time attribute to every edge so NetworkX can route by time.
    # osmnx imputes speed from OSM maxspeed tags where available,
    # falls back to sensible defaults by road type otherwise.
    G = ox.add_edge_speeds(G)
    G = ox.add_edge_travel_times(G)

    log.info(
        "Network downloaded: %d nodes, %d edges.",
        G.number_of_nodes(),
        G.number_of_edges(),
    )

    os.makedirs(os.path.dirname(NETWORK_PATH), exist_ok=True)
    ox.save_graphml(G, filepath=NETWORK_PATH)
    log.info("Road network saved to %s.", NETWORK_PATH)


# ---------------------------------------------------------------------------
# Step 2 — Restaurants
# ---------------------------------------------------------------------------

def setup_restaurants(conn):
    """Fetch Glendale restaurants from OSM and insert into PostGIS."""
    if table_has_rows(conn, "robotics", "restaurants"):
        log.info("robotics.restaurants already populated — skipping.")
        return

    log.info("Fetching restaurants from OpenStreetMap …")

    # ox.features_from_place returns a GeoDataFrame of OSM features.
    # Each row is one restaurant with geometry and OSM tags as columns.
    gdf = ox.features_from_place(
        PLACE_NAME,
        tags={"amenity": "restaurant"},
    )

    # We only want Point geometries (some OSM entries are outlines of
    # the building instead of a single point). Convert everything to
    # a centroid so all rows are proper Points.
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf["geometry"] = gdf["geometry"].centroid

    # Cap results
    gdf = gdf.head(MAX_RESTAURANTS)

    log.info("Found %d restaurants. Inserting into PostGIS …", len(gdf))

    rows = []
    for _, row in gdf.iterrows():
        name    = str(row.get("name", "Unknown Restaurant"))[:255]
        address = _build_address(row)
        # ST_GeomFromText expects WKT with SRID
        wkt = f"SRID=4326;POINT({row.geometry.x} {row.geometry.y})"
        rows.append((name, address, wkt))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO robotics.restaurants (name, address, location)
            VALUES %s
            """,
            rows,
            template="(%s, %s, ST_GeomFromEWKT(%s))",
        )
    conn.commit()
    log.info("Inserted %d restaurants.", len(rows))


# ---------------------------------------------------------------------------
# Step 3 — Residences
# ---------------------------------------------------------------------------

def setup_residences(conn):
    """Fetch Glendale residential buildings from OSM and insert into PostGIS."""
    if table_has_rows(conn, "robotics", "residences"):
        log.info("robotics.residences already populated — skipping.")
        return

    log.info("Fetching residential buildings from OpenStreetMap …")

    gdf = ox.features_from_place(
        PLACE_NAME,
        tags={"building": "residential"},
    )

    gdf = gdf[gdf.geometry.notna()].copy()

    # Building footprints come back as Polygons or MultiPolygons.
    # Convert to centroid Points so they fit the GEOMETRY(Point, 4326) column.
    gdf["geometry"] = gdf["geometry"].centroid

    gdf = gdf.head(MAX_RESIDENCES)

    log.info("Found %d residences. Inserting into PostGIS …", len(gdf))

    rows = []
    for _, row in gdf.iterrows():
        address = _build_address(row)
        wkt = f"SRID=4326;POINT({row.geometry.x} {row.geometry.y})"
        rows.append((address, wkt))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO robotics.residences (address, location)
            VALUES %s
            """,
            rows,
            template="(%s, ST_GeomFromEWKT(%s))",
        )
    conn.commit()
    log.info("Inserted %d residences.", len(rows))


# ---------------------------------------------------------------------------
# Address helper
# ---------------------------------------------------------------------------

def _build_address(row) -> str:
    """
    Build a best-effort address string from OSM tags.
    OSM address fields are inconsistently populated, so we fall back
    gracefully through the available tags.
    """
    parts = []
    housenumber = row.get("addr:housenumber", "")
    street      = row.get("addr:street", "")
    city        = row.get("addr:city", "Glendale")

    if housenumber and street:
        parts.append(f"{housenumber} {street}")
    elif street:
        parts.append(street)

    if city:
        parts.append(str(city))

    return ", ".join(parts)[:255] if parts else "Glendale, CA"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Serve Robotics — Data Setup")
    log.info("=" * 60)

    # Step 1: Road network (no DB connection needed)
    setup_road_network()

    # Steps 2 & 3: Location data into PostGIS
    log.info("Connecting to PostgreSQL …")
    conn = get_db_connection()
    try:
        setup_restaurants(conn)
        setup_residences(conn)
    finally:
        conn.close()

    log.info("=" * 60)
    log.info("Setup complete. Simulation engine may now start.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
