"""
Simulation engine — polls the DB for pending simulation jobs and runs them.

After setup.py completes, this process loops indefinitely:
  1. SELECT the oldest 'pending' simulation row (FOR UPDATE SKIP LOCKED)
  2. Mark it 'running'
  3. Run the SimPy simulation to completion
  4. Mark it 'completed' (or 'failed')
  5. Sleep briefly and repeat

The web-api creates 'pending' rows when the UI presses Start.
This process never exits under normal operation, keeping the container alive.
"""

import logging
import os
import time

import osmnx as ox
import psycopg2
import simpy
import simpy.rt
import yaml
from dotenv import load_dotenv

from db_writer import SimulationDatabaseWriter
from dispatcher import Dispatcher
from delivery_generator import DeliveryGenerator
from robot_agent import RobotAgent

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)

GRAPHML_PATH = "/app/data/serve_boundary_network_all.graphml"
CONFIGS_DIR  = "/app/delivery_configs"
DEFAULT_CONFIG = os.path.join(CONFIGS_DIR, "default.yaml")
POLL_INTERVAL  = 3  # seconds between DB polls when idle


# ------------------------------------------------------------------
# DB polling
# ------------------------------------------------------------------

def claim_pending_simulation(db_url: str) -> dict | None:
    """
    Atomically claim the oldest pending simulation.
    Returns a dict with sim params, or None if nothing is queued.
    Uses FOR UPDATE SKIP LOCKED so multiple workers never double-claim.
    """
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE robotics.simulations
                SET status = 'running', started_at = NOW()
                WHERE id = (
                    SELECT id FROM robotics.simulations
                    WHERE status = 'pending'
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, config_name, num_robots, duration_hours, real_time, speed_factor
                """
            )
            row = cur.fetchone()
        conn.commit()
        if row:
            return {
                "sim_id":         row[0],
                "config_name":    row[1] or "default",
                "num_robots":     row[2],
                "duration_hours": row[3],
                "real_time":      row[4],
                "speed_factor":   row[5],
            }
        return None
    finally:
        conn.close()


def mark_failed(db_url: str, sim_id: int):
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE robotics.simulations SET status='failed', completed_at=NOW() WHERE id=%s",
                (sim_id,),
            )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------
# Core simulation runner
# ------------------------------------------------------------------

def run_simulation(
    db_url: str,
    sim_id: int,
    config_name: str,
    num_robots: int,
    duration_hours: float,
    real_time: bool = False,
    speed_factor: float = 1.0,
):
    """Execute one simulation run using an already-claimed sim_id."""

    # Resolve config file — fall back to default if name doesn't match a file
    config_path = os.path.join(CONFIGS_DIR, f"{config_name}.yaml")
    if not os.path.exists(config_path):
        logger.warning("Config '%s' not found — using default.yaml", config_name)
        config_path = DEFAULT_CONFIG

    with open(config_path) as f:
        config = yaml.safe_load(f)

    duration_min = duration_hours * 60

    # Pull robot speed from config; fall back to the RobotAgent default if absent
    robot_speed_kmh = (
        config.get("constraints", {}).get("robot_speed_kmh", 6.0)
    )

    logger.info(
        "Simulation %d: config='%s', %d robots, %.0fh, robot speed=%.1f km/h",
        sim_id, config_name, num_robots, duration_hours, robot_speed_kmh,
    )

    # Load road network (cached on disk after first run)
    logger.info("Loading road network from %s ...", GRAPHML_PATH)
    G = ox.load_graphml(GRAPHML_PATH)
    logger.info("Network ready: %d nodes, %d edges", len(G.nodes), len(G.edges))

    # DB writer uses the existing sim row (created by web-api)
    db_writer = SimulationDatabaseWriter(db_url, sim_id, num_robots)

    # Real-time mode: SimPy sleeps between events to stay in sync with wall time.
    # factor = wall-clock seconds per simulated minute = 1 / speed_factor.
    # strict=False lets it catch up silently if a route calculation runs long.
    if real_time:
        env = simpy.rt.RealtimeEnvironment(
            factor=1.0 / speed_factor, strict=False
        )
        logger.info(
            "Real-time mode: %.1f sim-min/sec (factor=%.4f)",
            speed_factor, 1.0 / speed_factor,
        )
    else:
        env = simpy.Environment()
        logger.info("Fast mode: running as fast as possible")

    dispatcher = Dispatcher()

    def sim_clock(env, db_writer, until):
        """
        Lightweight SimPy process: write current sim time to the DB every
        sim-minute so the WebSocket can interpolate robot positions without
        the sim engine emitting a position row on every tick.

        At speed_factor=1 (real-time), 1 sim-minute = 1 wall second, so the
        WS position is at most 1 second stale — roughly 1.7 m at 6 km/h.
        """
        while env.now < until:
            yield env.timeout(1.0)   # 1 sim-minute
            db_writer.update_sim_time(env.now * 60)

    env.process(sim_clock(env, db_writer, duration_min))

    robots = [
        RobotAgent(env=env, robot_index=i, G=G,
                   dispatcher=dispatcher, db_writer=db_writer,
                   speed_kmh=robot_speed_kmh)
        for i in range(num_robots)
    ]
    for robot in robots:
        env.process(robot.run())

    generator = DeliveryGenerator(
        env=env, db_url=db_url, config_path=config_path, dispatcher=dispatcher,
        sim_id=sim_id,
    )
    env.process(generator.run())

    logger.info("Simulation %d running until t=%.0f min ...", sim_id, duration_min)

    # Run in chunks so we can detect a user-triggered stop between steps.
    # For fast-mode each chunk completes instantly; for real-time a 10-min chunk
    # takes 10/speed_factor wall-clock seconds, so stop latency is at most that.
    CHUNK = 10.0  # simulated minutes per iteration
    t = CHUNK
    stopped_early = False
    while env.now < duration_min:
        env.run(until=min(t, duration_min))
        t += CHUNK
        # Check DB — user may have pressed Stop
        conn_check = psycopg2.connect(db_url)
        try:
            with conn_check.cursor() as cur:
                cur.execute(
                    "SELECT status FROM robotics.simulations WHERE id = %s",
                    (sim_id,),
                )
                status = cur.fetchone()[0]
        finally:
            conn_check.close()
        if status == "stopped":
            logger.info("Simulation %d stopped by user at t=%.1f min", sim_id, env.now)
            stopped_early = True
            break

    if not stopped_early:
        logger.info("Simulation %d complete at t=%.1f min", sim_id, env.now)

    total_deliveries  = sum(r.total_deliveries  for r in robots)
    total_distance_km = sum(r.total_distance_km for r in robots)

    db_writer.close(
        total_deliveries=total_deliveries,
        total_distance_km=total_distance_km,
        stopped=stopped_early,
    )
    logger.info(
        "Simulation %d finalized — %d deliveries, %.1f km",
        sim_id, total_deliveries, total_distance_km,
    )


# ------------------------------------------------------------------
# Entry point — polling loop
# ------------------------------------------------------------------

def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    logger.info("Simulation engine ready — polling for pending jobs every %ds", POLL_INTERVAL)

    while True:
        job = claim_pending_simulation(db_url)
        if job:
            logger.info("Claimed simulation %d", job["sim_id"])
            try:
                run_simulation(db_url, **job)
            except Exception as exc:
                logger.error(
                    "Simulation %d failed: %s", job["sim_id"], exc, exc_info=True
                )
                mark_failed(db_url, job["sim_id"])
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
