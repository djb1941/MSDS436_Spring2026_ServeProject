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
    finally:
        conn.close()

    return {
        "total_deliveries":    int(stats["total_deliveries"] or 0),
        "completed_deliveries": int(stats["completed_deliveries"] or 0),
        "pending_deliveries":  int(stats["pending_deliveries"] or 0),
        "robots_active":       int(robot_stats["robots_active"] or 0),
        "robots_idle":         int(robot_stats["robots_idle"] or 0),
        "average_delivery_time": float(stats["avg_delivery_time"]) if stats["avg_delivery_time"] else None,
    }


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
                    cur.execute(
                        """
                        SELECT
                            rl.robot_id,
                            ST_Y(rl.location)  AS lat,
                            ST_X(rl.location)  AS lon,
                            rl.status,
                            ar.leg_type,
                            ar.route_coords
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
                        """,
                        (sim_id, sim_id),
                    )
                    robots = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

            payload = {
                "type": "robot_update",
                "simulation_id": sim_id,
                "robots": robots,
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
