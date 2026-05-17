"""
FastAPI application for Serve Robotics Simulation.
Provides REST endpoints (polling) for config/results and
WebSocket endpoints for real-time robot and delivery streaming.
"""

import asyncio
import json
import logging
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="Serve Robotics Simulation API",
    description="API for managing robot deliveries and simulation data",
    version="0.1.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "serve-robotics-api"}

@app.get("/api/v1/robots")
async def get_robots():
    """Get all robots."""
    # TODO: Query robots from PostgreSQL
    return {
        "robots": [],
        "count": 0
    }

@app.get("/api/v1/deliveries")
async def get_deliveries():
    """Get all deliveries."""
    # TODO: Query deliveries from PostgreSQL
    return {
        "deliveries": [],
        "count": 0
    }

@app.get("/api/v1/restaurants")
async def get_restaurants():
    """Get all restaurants."""
    # TODO: Query restaurants from PostgreSQL
    return {
        "restaurants": [],
        "count": 0
    }

@app.get("/api/v1/map")
async def get_map():
    """Get map visualization data."""
    # TODO: Generate map data with robot locations
    return {
        "map_data": None,
        "timestamp": None
    }

@app.get("/api/v1/stats")
async def get_statistics():
    """Get simulation statistics."""
    # TODO: Calculate statistics from simulation data
    return {
        "total_deliveries": 0,
        "average_delivery_time": 0,
        "average_distance": 0,
        "robots_idle": 0,
        "robots_active": 0
    }

@app.post("/api/v1/simulations/start")
async def start_simulation(config: dict):
    """
    Start a new simulation run.

    Expected body:
        {
            "config_name": "rush_hour",
            "num_robots": 5,
            "duration_hours": 24
        }

    Returns the simulation_id that the frontend uses to subscribe
    to the live WebSocket feeds.
    """
    # TODO: Trigger the SimPy simulation engine process
    sim_id = 1  # Placeholder until engine is wired up
    logger.info(f"Starting simulation with config={config}, sim_id={sim_id}")
    return {"simulation_id": sim_id, "status": "started"}

@app.post("/api/v1/simulations/{sim_id}/stop")
async def stop_simulation(sim_id: int):
    """Stop a running simulation."""
    # TODO: Send stop signal to simulation process
    logger.info(f"Stopping simulation {sim_id}")
    return {"simulation_id": sim_id, "status": "stopped"}

@app.get("/api/v1/simulations")
async def list_simulations():
    """List all simulation runs (for loading historical results)."""
    # TODO: Query robotics.simulations table
    return {"simulations": []}

@app.get("/api/v1/simulations/{sim_id}/results")
async def get_simulation_results(sim_id: int):
    """
    Return final statistics for a completed simulation.
    Polled once by the frontend after the simulation finishes.
    """
    # TODO: Query completed simulation summary from PostgreSQL
    return {
        "simulation_id": sim_id,
        "status": "completed",
        "total_deliveries": 0,
        "avg_delivery_time_s": 0,
        "avg_distance_km": 0.0,
        "total_distance_km": 0.0,
    }

# ---------------------------------------------------------------------------
# WebSocket: real-time robot position stream
# ---------------------------------------------------------------------------
# The Flet frontend opens this connection once per simulation run and receives
# a JSON object every ~1 second with the latest position of each robot.
#
# Message format:
#   {
#     "type": "robot_update",
#     "simulation_id": 1,
#     "sim_time_s": 900.0,
#     "robots": [
#       {"robot_id": 0, "lat": 34.14, "lon": -118.25, "status": "traveling"},
#       ...
#     ]
#   }

@app.websocket("/ws/robots/{sim_id}")
async def ws_robot_positions(websocket: WebSocket, sim_id: int):
    """
    WebSocket feed of robot positions for a running simulation.
    Pushes an update every second until the simulation ends or the
    client disconnects.
    """
    await websocket.accept()
    logger.info(f"WebSocket client connected: robot feed for sim_id={sim_id}")
    try:
        sim_time = 0.0
        while True:
            # TODO: Replace stub with a real DB query:
            #   SELECT robot_id, latitude, longitude, status
            #   FROM robotics.robot_locations
            #   WHERE simulation_id = %s AND timestamp > %s
            #   ORDER BY timestamp DESC LIMIT 1 per robot
            payload = {
                "type": "robot_update",
                "simulation_id": sim_id,
                "sim_time_s": sim_time,
                "robots": [],  # populated once DB writes are live
            }
            await websocket.send_text(json.dumps(payload))
            sim_time += 1.0
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: robot feed for sim_id={sim_id}")

# ---------------------------------------------------------------------------
# WebSocket: real-time delivery status stream
# ---------------------------------------------------------------------------
# Message format:
#   {
#     "type": "delivery_update",
#     "simulation_id": 1,
#     "summary": {"total": 12, "completed": 8, "pending": 4},
#     "recent": [
#       {"delivery_id": 5, "robot_id": 2, "status": "delivered",
#        "duration_s": 420, "distance_km": 1.3},
#       ...
#     ]
#   }

@app.websocket("/ws/deliveries/{sim_id}")
async def ws_delivery_status(websocket: WebSocket, sim_id: int):
    """
    WebSocket feed of delivery status for a running simulation.
    Pushes a summary + recent delivery list every 2 seconds.
    """
    await websocket.accept()
    logger.info(f"WebSocket client connected: delivery feed for sim_id={sim_id}")
    try:
        while True:
            # TODO: Replace stub with real DB queries:
            #   SELECT COUNT(*), COUNT(delivered_at), COUNT(CASE WHEN picked_up_at IS NULL ...)
            #   FROM robotics.deliveries WHERE simulation_id = %s
            payload = {
                "type": "delivery_update",
                "simulation_id": sim_id,
                "summary": {"total": 0, "completed": 0, "pending": 0},
                "recent": [],
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: delivery feed for sim_id={sim_id}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
