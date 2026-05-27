"""
setup.py — One-time data initialisation for the Serve Robotics Simulation.

Run by start.sh before the simulation engine or Flet UI starts.
Safe to run repeatedly — every step checks whether the work is already done
and skips it if so (idempotent).

What this script does
---------------------
1. Road network
   - Checks for /app/data/serve_boundary_network.graphml on the Docker volume.
   - If absent: downloads the drive network within the custom CA-2 / CA-134 /
     LA River polygon from OpenStreetMap via osmnx and saves it as GraphML.
   - The simulation engine loads this file at runtime to do NetworkX routing.

2. Restaurants (robotics.restaurants)
   - Checks whether the table already has rows.
   - If empty: queries OSM for amenity=restaurant within the simulation boundary
     and inserts each one as a PostGIS Point geometry.

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

import networkx as nx
import osmnx as ox
import psycopg2
from psycopg2.extras import execute_values
from shapely.geometry import Polygon as ShapelyPolygon

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

# The cached GraphML filename encodes which boundary and network type were used.
# If either changes, rename this file so the old graph isn't reused.
# "_all" = network_type="all" (roads + service roads + paths + crossings)
NETWORK_PATH   = "/app/data/serve_boundary_network_all.graphml"

# ---------------------------------------------------------------------------
# Custom simulation boundary
# ---------------------------------------------------------------------------
# The simulation area is a rough triangle defined by three geographic features:
#
#   North edge — CA-134 (Ventura Freeway), running east-west
#   East edge  — CA-2 (Glendale Freeway), running south from the 134 interchange
#                through central Glendale toward Atwater Village
#   West edge  — Los Angeles River, running south from the 134 through the
#                Glendale Narrows toward Atwater Village
#
# The triangle opens downward: the two northern corners sit on CA-134, and the
# southern vertex is where CA-2 and the LA River converge near Atwater Village.
#
# Vertices (lon, lat) are approximate intersections / terminal points of these
# three features.  Each comment names the real-world landmark being anchored.
# Adjust coordinates here if you want to expand or tighten the boundary.
#
#  Node  Landmark
#  ----  -------------------------------------------------------
#  A     CA-134 × LA River (NW corner, Burbank/Glendale border)
#  B     CA-134 × CA-2 interchange (NE corner)
#  C–D   CA-2 (Glendale Freeway) southward through Glendale
#  E     CA-2 × LA River (south vertex, Atwater Village bridge area)
#  F–G   LA River northward through Glendale Narrows back to 134
#
BOUNDARY_COORDS = [
    (-118.27945, 34.15291),  # A — CA-134 × LA River (NW)
    (-118.26446, 34.15619),  # B - CA-134 & Pacific Ave
    (-118.25794, 34.15663),  # C - CA-134 & Central Ave
    (-118.24681, 34.15663),  # D - CA-134 & Geneva St
    (-118.24208, 34.15597),  # E - CA-134 & Glendale Ave
    (-118.22611, 34.14670),  # F — CA-134 × CA-2 interchange (NE)
    (-118.22918, 34.13676),  # G - CA-2 @ Verd Oaks Dr
    (-118.22946, 34.13215),  # H - CA-2 & Round Top Dr
    (-118.22821, 34.12512),  # I - CA-2 & York Blvd
    (-118.22978, 34.12128),  # J - CA-2 & Verdugo Rd
    (-118.23484, 34.11782),  # K - CA-2 & Avenue 36
    (-118.24418, 34.11326),  # L - CA-2 & San Fernando Rd
    (-118.25200, 34.10829),  # M - CA-2 × LA River (south vertex, Atwater Village)
    (-118.25633, 34.10847),  # N - LA River @ Silver Lake Blvd
    (-118.26127, 34.11020),  # O - LA River @ Garcia St
    (-118.26557, 34.11348),  # P - LA River & Glendale Blvd
    (-118.27044, 34.12154),  # Q - LA River & Los Feliz Blvd
    (-118.27679, 34.14096),  # R - LA River and Colorado St
    (-118.27945, 34.15291),  # A — close polygon back to NW corner
]

# Shapely polygon used by all three OSM fetch calls below
SIMULATION_BOUNDARY = ShapelyPolygon(BOUNDARY_COORDS)

# How many OSM results to cap at. The custom boundary covers most of Glendale
# plus a slice of Burbank — limits keep the DB and simulation manageable.
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
    """Download and cache the drive network inside SIMULATION_BOUNDARY as GraphML."""
    if os.path.exists(NETWORK_PATH):
        log.info("Road network already exists at %s — skipping download.", NETWORK_PATH)
        return

    log.info("Downloading road network from OpenStreetMap for custom boundary …")
    log.info("Boundary: CA-134 (north) × CA-2 (east) × LA River (west)")
    log.info("(This takes 30–90 seconds on first run and is cached to disk.)")

    # network_type="all" fetches every edge type: roads, service roads, paths,
    # at-grade railroad crossings, pedestrian ways, etc.  This is important
    # because Serve robots travel on sidewalks, not car lanes, and the BNSF/
    # Metrolink railroad running through the boundary creates a barrier that
    # the drive-only network cannot cross properly (streets that pass over or
    # through the railroad are often not in the car graph).
    G = ox.graph_from_polygon(SIMULATION_BOUNDARY, network_type="all")

    # Trim to the largest weakly connected component.  A weakly connected
    # component is a set of nodes where every node is reachable from every
    # other ignoring edge direction.  Without this step, small isolated
    # subgraphs (dead-end blocks, parking lots, etc.) remain in the graph.
    # ox.nearest_nodes() picks the geometrically closest node regardless of
    # connectivity, so a restaurant south of the tracks could snap to an
    # isolated node that routing can't reach — silently producing no route.
    # Keeping only the largest component ensures every node is reachable.
    largest_wcc = max(nx.weakly_connected_components(G), key=len)
    G = G.subgraph(largest_wcc).copy()
    log.info(
        "Trimmed to largest connected component: %d nodes retained.",
        G.number_of_nodes(),
    )

    # Add travel_time attribute to every edge.  We don't use these values for
    # routing (robots use their own fixed speed), but osmnx requires them for
    # the GraphML to be complete and they may be useful for future analysis.
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
    """Fetch restaurants within SIMULATION_BOUNDARY from OSM and insert into PostGIS."""
    if table_has_rows(conn, "robotics", "restaurants"):
        log.info("robotics.restaurants already populated — skipping.")
        return

    log.info("Fetching restaurants from OpenStreetMap within simulation boundary …")

    # features_from_polygon clips OSM results to the polygon — only restaurants
    # inside the CA-2 / CA-134 / LA River triangle are returned.
    gdf = ox.features_from_polygon(
        SIMULATION_BOUNDARY,
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
    """Fetch residential buildings within SIMULATION_BOUNDARY from OSM and insert into PostGIS."""
    if table_has_rows(conn, "robotics", "residences"):
        log.info("robotics.residences already populated — skipping.")
        return

    log.info("Fetching residential buildings from OpenStreetMap within simulation boundary …")

    gdf = ox.features_from_polygon(
        SIMULATION_BOUNDARY,
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

def apply_migrations(conn):
    """Idempotent schema migrations for columns added after initial deploy."""
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE robotics.simulations
            ADD COLUMN IF NOT EXISTS duration_hours FLOAT DEFAULT 24
        """)
        cur.execute("""
            ALTER TABLE robotics.simulations
            ADD COLUMN IF NOT EXISTS real_time BOOLEAN DEFAULT FALSE
        """)
        cur.execute("""
            ALTER TABLE robotics.simulations
            ADD COLUMN IF NOT EXISTS speed_factor FLOAT DEFAULT 1.0
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS robotics.active_routes (
                robot_id             INTEGER PRIMARY KEY REFERENCES robotics.robots(id),
                simulation_id        INT NOT NULL REFERENCES robotics.simulations(id),
                leg_type             VARCHAR(20) NOT NULL,
                route_coords         JSONB NOT NULL,
                leg_started_at_sim_s FLOAT,
                updated_at           TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            ALTER TABLE robotics.active_routes
            ADD COLUMN IF NOT EXISTS leg_started_at_sim_s FLOAT
        """)
        cur.execute("""
            ALTER TABLE robotics.simulations
            ADD COLUMN IF NOT EXISTS current_sim_time_s FLOAT DEFAULT 0
        """)
        # Allow robot_id to be NULL so deliveries can be written at generation
        # time (before a robot is assigned).  The robot claims the row later.
        cur.execute("""
            ALTER TABLE robotics.deliveries
            ALTER COLUMN robot_id DROP NOT NULL
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS robotics.rejected_deliveries (
                id SERIAL PRIMARY KEY,
                simulation_id   INT NOT NULL REFERENCES robotics.simulations(id),
                restaurant_id   INT NOT NULL REFERENCES robotics.restaurants(id),
                residence_id    INT NOT NULL REFERENCES robotics.residences(id),
                requested_at    TIMESTAMP NOT NULL,
                estimated_minutes FLOAT NOT NULL,
                reason          VARCHAR(50) NOT NULL
            )
        """)
    conn.commit()
    log.info("Schema migrations applied.")


def main():
    log.info("=" * 60)
    log.info("Serve Robotics — Data Setup")
    log.info("=" * 60)

    # Step 1: Road network (no DB connection needed)
    setup_road_network()

    # Steps 2–4: Location data + schema migrations
    log.info("Connecting to PostgreSQL …")
    conn = get_db_connection()
    try:
        apply_migrations(conn)
        setup_restaurants(conn)
        setup_residences(conn)
    finally:
        conn.close()

    log.info("=" * 60)
    log.info("Setup complete. Simulation engine may now start.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
