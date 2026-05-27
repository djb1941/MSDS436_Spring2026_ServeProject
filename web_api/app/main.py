"""
FastAPI application for Serve Robotics Simulation.

REST endpoints:
  POST /api/v1/simulations/start          — launch a simulation in a background thread
  POST /api/v1/simulations/{id}/stop      — mark a simulation as stopped
  GET  /api/v1/simulations                — list all simulation runs
  GET  /api/v1/simulations/{id}/results   — final stats for a completed run
  GET  /health                            — liveness check

WebSocket endpoints:
  /ws/robots/{sim_id}      — robot positions, pushed every 1s
  /ws/deliveries/{sim_id}  — delivery counts + recent completions, pushed every 2s

The simulation engine lives in /sim_engine (mounted from ./app in docker-compose).
It is imported at runtime and run in a background thread so the API stays responsive.
"""

import asyncio
import json
import logging
import math
import os
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL")

# Must match DEFAULT_ROBOT_SPEED_KMH in robot_agent.py
ROBOT_SPEED_MS = 6.0 * 1000 / 3600   # 6 km/h → m/s


def remaining_route_distance(
    route_coords: list[list[float]], distance_traveled_m: float
) -> float:
    """
    Return the number of metres remaining in route_coords after
    distance_traveled_m have already been consumed from the start.

    Uses the same equirectangular segment-length approximation as
    interpolate_along_route so the two functions stay consistent.
    """
    remaining_travel = max(0.0, distance_traveled_m)

    for i in range(len(route_coords) - 1):
        lat1, lon1 = route_coords[i]
        lat2, lon2 = route_coords[i + 1]
        mid_lat_rad = math.radians((lat1 + lat2) / 2)
        dlat_m = (lat2 - lat1) * 111_320
        dlon_m = (lon2 - lon1) * 111_320 * math.cos(mid_lat_rad)
        seg_len = math.sqrt(dlat_m ** 2 + dlon_m ** 2)

        if remaining_travel <= seg_len:
            # Robot is currently on this segment — sum up everything after it
            tail = seg_len - remaining_travel
            for j in range(i + 1, len(route_coords) - 1):
                la1, lo1 = route_coords[j]
                la2, lo2 = route_coords[j + 1]
                mid = math.radians((la1 + la2) / 2)
                tail += math.sqrt(
                    ((la2 - la1) * 111_320) ** 2
                    + ((lo2 - lo1) * 111_320 * math.cos(mid)) ** 2
                )
            return tail
        remaining_travel -= seg_len

    return 0.0  # already past the end


def interpolate_along_route(
    route_coords: list[list[float]], distance_m: float
) -> tuple[float, float] | None:
    """
    Walk `route_coords` (list of [lat, lon]) until `distance_m` metres have
    been consumed, then return the interpolated (lat, lon) at that point.

    Uses an equirectangular approximation for segment length — accurate enough
    for city-scale distances (< 0.1 % error within a few kilometres).
    Returns the final waypoint if distance_m exceeds total route length.
    """
    if not route_coords:
        return None

    remaining = max(0.0, distance_m)

    for i in range(len(route_coords) - 1):
        lat1, lon1 = route_coords[i]
        lat2, lon2 = route_coords[i + 1]

        # Convert degree differences to approximate metres
        mid_lat_rad = math.radians((lat1 + lat2) / 2)
        dlat_m = (lat2 - lat1) * 111_320
        dlon_m = (lon2 - lon1) * 111_320 * math.cos(mid_lat_rad)
        seg_len = math.sqrt(dlat_m ** 2 + dlon_m ** 2)

        if seg_len == 0 or remaining <= seg_len:
            frac = (remaining / seg_len) if seg_len > 0 else 0.0
            return (
                lat1 + (lat2 - lat1) * frac,
                lon1 + (lon2 - lon1) * frac,
            )
        remaining -= seg_len

    # Past the end of the route — snap to destination
    return route_coords[-1][0], route_coords[-1][1]


# ------------------------------------------------------------------
# DB helper
# ------------------------------------------------------------------

def get_conn():
    """Open a new psycopg2 connection. Caller is responsible for closing it."""
    return psycopg2.connect(DB_URL)


# ------------------------------------------------------------------
# App
# ------------------------------------------------------------------

app = FastAPI(
    title="Serve Robotics Simulation API",
    description="REST + WebSocket API for the Serve Robotics SimPy simulation",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "serve-robotics-api"}


# ------------------------------------------------------------------
# Simulation lifecycle
# ------------------------------------------------------------------

@app.post("/api/v1/simulations/start")
async def start_simulation(config: dict = Body(default={})):
    """
    Queue a simulation run by inserting a 'pending' row.
    The python-app sim engine polls for pending rows and runs them.

    Body (all optional):
        { "config_name": "default", "num_robots": 5, "duration_hours": 24 }
    """
    config_name    = config.get("config_name", "default")
    num_robots     = int(config.get("num_robots", 5))
    duration_hours = float(config.get("duration_hours", 24))
    real_time      = bool(config.get("real_time", False))
    speed_factor   = float(config.get("speed_factor", 1.0))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO robotics.simulations
                    (config_name, num_robots, duration_hours, real_time, speed_factor, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
                RETURNING id
                """,
                (config_name, num_robots, duration_hours, real_time, speed_factor),
            )
            sim_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    return {"simulation_id": sim_id, "status": "queued", "real_time": real_time}


@app.post("/api/v1/simulations/{sim_id}/stop")
async def stop_simulation(sim_id: int):
    """
    Mark a simulation as stopped in the DB.
    The engine thread runs to natural completion — SimPy has no kill switch.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE robotics.simulations
                SET status = 'stopped', completed_at = NOW()
                WHERE id = %s AND status = 'running'
                """,
                (sim_id,),
            )
        conn.commit()
    finally:
        conn.close()

    return {"simulation_id": sim_id, "status": "stopped"}


@app.get("/api/v1/simulations")
async def list_simulations():
    """List all simulation runs, most recent first."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, config_name, num_robots, status,
                       started_at, completed_at,
                       total_deliveries, total_distance_km
                FROM robotics.simulations
                ORDER BY started_at DESC
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # datetime objects aren't JSON-serialisable — convert to ISO strings
    for r in rows:
        for key in ("started_at", "completed_at"):
            if isinstance(r[key], datetime):
                r[key] = r[key].isoformat()

    return {"simulations": rows}


@app.get("/api/v1/stats")
async def get_stats():
    """Aggregate stats across the most recent running or completed simulation."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Most recent simulation
            cur.execute(
                """
                SELECT id FROM robotics.simulations
                ORDER BY started_at DESC LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return {"total_deliveries": 0, "completed_deliveries": 0,
                        "pending_deliveries": 0, "robots_active": 0,
                        "robots_idle": 0, "average_delivery_time": None}
            sim_id = row["id"]

            cur.execute(
                """
                SELECT
                    COUNT(*)                                            AS total_deliveries,
                    COUNT(delivered_at)                                 AS completed_deliveries,
                    COUNT(*) FILTER (WHERE delivered_at IS NULL)        AS pending_deliveries,
                    AVG(duration_seconds) FILTER (WHERE delivered_at IS NOT NULL) AS avg_delivery_time
                FROM robotics.deliveries
                WHERE simulation_id = %s
                """,
                (sim_id,),
            )
            stats = dict(cur.fetchone())

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status != 'idle') AS robots_active,
                    COUNT(*) FILTER (WHERE status = 'idle')  AS robots_idle
                FROM (
                    SELECT DISTINCT ON (robot_id) robot_id, status
                    FROM robotics.robot_locations
                    WHERE simulation_id = %s
                    ORDER BY robot_id, timestamp DESC
                ) latest
                """,
                (sim_id,),
            )
            robot_stats = dict(cur.fetchone())

            cur.execute(
                "SELECT COUNT(*) AS rejected FROM robotics.rejected_deliveries WHERE simulation_id = %s",
                (sim_id,),
            )
            rejected_count = cur.fetchone()["rejected"]
    finally:
        conn.close()

    return {
        "total_deliveries":     int(stats["total_deliveries"] or 0),
        "completed_deliveries": int(stats["completed_deliveries"] or 0),
        "pending_deliveries":   int(stats["pending_deliveries"] or 0),
        "robots_active":        int(robot_stats["robots_active"] or 0),
        "robots_idle":          int(robot_stats["robots_idle"] or 0),
        "average_delivery_time": float(stats["avg_delivery_time"]) if stats["avg_delivery_time"] else None,
        "rejected_deliveries":  int(rejected_count or 0),
    }


@app.get("/api/v1/restaurants")
async def get_restaurants():
    """
    Return all restaurants in the simulation boundary with their PostGIS coordinates.
    This is static data — it doesn't change between simulation runs.
    The frontend fetches this once on load to render restaurant markers on the map.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    name,
                    address,
                    ST_Y(location) AS lat,
                    ST_X(location) AS lon
                FROM robotics.restaurants
                ORDER BY id
                """
            )
            restaurants = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return {"restaurants": restaurants}


@app.get("/api/v1/simulations/{sim_id}/results")
async def get_simulation_results(sim_id: int):
    """Final statistics for a completed simulation run."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Simulation summary row
            cur.execute(
                """
                SELECT id, config_name, num_robots, status,
                       started_at, completed_at,
                       total_deliveries, total_distance_km
                FROM robotics.simulations WHERE id = %s
                """,
                (sim_id,),
            )
            sim = cur.fetchone()
            if not sim:
                raise HTTPException(status_code=404, detail="Simulation not found")

            # Average delivery time and distance across all completed deliveries
            cur.execute(
                """
                SELECT
                    AVG(duration_seconds) AS avg_duration_s,
                    AVG(distance_km)      AS avg_distance_km
                FROM robotics.deliveries
                WHERE simulation_id = %s AND delivered_at IS NOT NULL
                """,
                (sim_id,),
            )
            stats = cur.fetchone()
    finally:
        conn.close()

    result = dict(sim)
    result["avg_delivery_time_s"] = round(stats["avg_duration_s"] or 0, 1)
    result["avg_distance_km"]     = round(stats["avg_distance_km"] or 0, 2)
    for key in ("started_at", "completed_at"):
        if isinstance(result[key], datetime):
            result[key] = result[key].isoformat()

    return result


# ------------------------------------------------------------------
# WebSocket: robot positions
# ------------------------------------------------------------------

@app.websocket("/ws/robots/{sim_id}")
async def ws_robot_positions(websocket: WebSocket, sim_id: int):
    """
    Push the latest position of every robot every second.

    Uses DISTINCT ON (robot_id) to get each robot's most recent row
    from robot_locations without a slow subquery.

    Message format:
      {
        "type": "robot_update",
        "simulation_id": 1,
        "robots": [
          {"robot_id": 0, "lat": 34.14, "lon": -118.25, "status": "traveling"},
          ...
        ]
      }
    """
    await websocket.accept()
    logger.info(f"WS connected: robot feed sim_id={sim_id}")

    try:
        while True:
            conn = get_conn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Latest recorded position + active route for every robot.
                    # LATERAL subquery finds the one in-progress delivery for
                    # each robot (picked up, not yet delivered) so we can join
                    # through to the residence for robots on leg 2.
                    cur.execute(
                        """
                        SELECT
                            rl.robot_id,
                            ST_Y(rl.location)        AS lat,
                            ST_X(rl.location)        AS lon,
                            rl.status,
                            ar.leg_type,
                            ar.route_coords,
                            ar.leg_started_at_sim_s,
                            res.id                   AS residence_id,
                            res.address              AS residence_address,
                            ST_Y(res.location)       AS residence_lat,
                            ST_X(res.location)       AS residence_lon
                        FROM (
                            SELECT DISTINCT ON (robot_id)
                                robot_id, location, status
                            FROM robotics.robot_locations
                            WHERE simulation_id = %s
                            ORDER BY robot_id, timestamp DESC
                        ) rl
                        LEFT JOIN robotics.active_routes ar
                            ON ar.robot_id = rl.robot_id
                           AND ar.simulation_id = %s
                        LEFT JOIN LATERAL (
                            SELECT residence_id
                            FROM robotics.deliveries
                            WHERE robot_id = rl.robot_id
                              AND simulation_id = %s
                              AND picked_up_at IS NOT NULL
                              AND delivered_at  IS NULL
                            ORDER BY picked_up_at DESC
                            LIMIT 1
                        ) active_del ON true
                        LEFT JOIN robotics.residences res
                            ON res.id = active_del.residence_id
                        """,
                        (sim_id, sim_id, sim_id),
                    )
                    robots = [dict(r) for r in cur.fetchall()]

                    # Current sim clock — written every ~1 sim-min by the engine
                    cur.execute(
                        "SELECT current_sim_time_s FROM robotics.simulations WHERE id = %s",
                        (sim_id,),
                    )
                    sim_row = cur.fetchone()
                    current_sim_time_s = (
                        sim_row["current_sim_time_s"] if sim_row else None
                    )

                    # Busy restaurants
                    cur.execute(
                        """
                        SELECT
                            restaurant_id,
                            COUNT(*) AS pending_count
                        FROM robotics.deliveries
                        WHERE simulation_id = %s
                          AND picked_up_at IS NULL
                          AND delivered_at IS NULL
                        GROUP BY restaurant_id
                        """,
                        (sim_id,),
                    )
                    busy_restaurants = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

            # Interpolate position for robots currently traveling.
            # The sim engine only writes a position row on status changes, so
            # "traveling" robots are frozen in the DB at their leg start point.
            # We compute where they actually are right now from:
            #   elapsed = current_sim_time_s − leg_started_at_sim_s
            #   distance = elapsed × ROBOT_SPEED_MS
            # then walk the stored route waypoints to that distance.
            if current_sim_time_s is not None:
                for robot in robots:
                    if (
                        robot["status"] == "traveling"
                        and robot.get("route_coords")
                        and robot.get("leg_started_at_sim_s") is not None
                    ):
                        elapsed_s  = current_sim_time_s - robot["leg_started_at_sim_s"]
                        distance_m = elapsed_s * ROBOT_SPEED_MS
                        pos = interpolate_along_route(robot["route_coords"], distance_m)
                        if pos:
                            robot["lat"], robot["lon"] = pos

            # Build one entry per robot that is currently on leg 2 (to_residence)
            # and has residence coordinates available.  ETA is computed from the
            # remaining route distance at the robot's current interpolated position.
            active_residences = []
            for robot in robots:
                if (
                    robot.get("leg_type") == "to_residence"
                    and robot.get("residence_lat") is not None
                ):
                    eta_sim_minutes = None
                    if (
                        current_sim_time_s is not None
                        and robot.get("route_coords")
                        and robot.get("leg_started_at_sim_s") is not None
                    ):
                        elapsed_s    = current_sim_time_s - robot["leg_started_at_sim_s"]
                        traveled_m   = max(0.0, elapsed_s * ROBOT_SPEED_MS)
                        remaining_m  = remaining_route_distance(
                            robot["route_coords"], traveled_m
                        )
                        eta_sim_minutes = round(remaining_m / ROBOT_SPEED_MS / 60, 1)

                    active_residences.append({
                        "robot_id":        robot["robot_id"],
                        "residence_id":    robot.get("residence_id"),
                        "residence_lat":   robot["residence_lat"],
                        "residence_lon":   robot["residence_lon"],
                        "address":         robot.get("residence_address") or "Address unknown",
                        "eta_sim_minutes": eta_sim_minutes,
                    })

            payload = {
                "type": "robot_update",
                "simulation_id": sim_id,
                "robots": robots,
                "busy_restaurants": busy_restaurants,
                "active_residences": active_residences,
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(1)

    except WebSocketDisconnect:
        logger.info(f"WS disconnected: robot feed sim_id={sim_id}")


# ------------------------------------------------------------------
# WebSocket: delivery status
# ------------------------------------------------------------------

@app.websocket("/ws/deliveries/{sim_id}")
async def ws_delivery_status(websocket: WebSocket, sim_id: int):
    """
    Push delivery counts + 10 most recent completions every 2 seconds.

    Message format:
      {
        "type": "delivery_update",
        "simulation_id": 1,
        "summary": {"total": 42, "completed": 35, "in_progress": 5, "pending": 2},
        "recent": [
          {"delivery_id": 12, "robot_id": 3, "duration_seconds": 480,
           "distance_km": 1.7, "delivered_at": "2026-05-23T11:42:00"},
          ...
        ]
      }
    """
    await websocket.accept()
    logger.info(f"WS connected: delivery feed sim_id={sim_id}")

    try:
        while True:
            conn = get_conn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Delivery counts
                    cur.execute(
                        """
                        SELECT
                            COUNT(*)                                             AS total,
                            COUNT(delivered_at)                                  AS completed,
                            COUNT(*) FILTER (WHERE picked_up_at IS NOT NULL
                                              AND delivered_at IS NULL)          AS in_progress,
                            COUNT(*) FILTER (WHERE picked_up_at IS NULL)         AS pending
                        FROM robotics.deliveries
                        WHERE simulation_id = %s
                        """,
                        (sim_id,),
                    )
                    summary = dict(cur.fetchone())

                    # 10 most recently completed deliveries
                    cur.execute(
                        """
                        SELECT
                            id            AS delivery_id,
                            robot_id,
                            duration_seconds,
                            distance_km,
                            delivered_at
                        FROM robotics.deliveries
                        WHERE simulation_id = %s AND delivered_at IS NOT NULL
                        ORDER BY delivered_at DESC
                        LIMIT 10
                        """,
                        (sim_id,),
                    )
                    recent = []
                    for r in cur.fetchall():
                        row = dict(r)
                        if isinstance(row["delivered_at"], datetime):
                            row["delivered_at"] = row["delivered_at"].isoformat()
                        recent.append(row)
            finally:
                conn.close()

            payload = {
                "type": "delivery_update",
                "simulation_id": sim_id,
                "summary": summary,
                "recent": recent,
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        logger.info(f"WS disconnected: delivery feed sim_id={sim_id}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
