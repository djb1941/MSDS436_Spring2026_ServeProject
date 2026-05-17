# Serve Robotics Simulation - Backend Architecture Design
**Date:** May 2, 2026  
**Status:** Design Phase (Pre-Implementation)  
**Purpose:** Collaborative understanding of how SimPy, road networks, pluggable generators, and live queries work together

---

## Overview: The Full Data Flow

```
User Config (YAML)
    ↓
[Simulation Engine (SimPy)]
├─ Load delivery config
├─ Load road network (osmnx/NetworkX)
├─ Create robot agents
├─ Generate delivery events (via config)
│   └─ Route between locations (NetworkX shortest path)
├─ Simulate 1 day (23:59:59)
└─ Write to DB in real-time as events happen
    ↓
[PostgreSQL + PostGIS]
├─ robot_locations (streaming inserts)
├─ deliveries (order progress)
└─ robots (status updates)
    ↓
[FastAPI Web Server]
├─ Live queries (during simulation): SELECT * WHERE timestamp > last_query_time
├─ Historical queries (after simulation): SELECT * WHERE simulation_id = X
└─ Playback endpoints: GET /api/v1/simulation/{id}/timeline?t=900s
```

---

## Component 1: Pluggable Delivery Generator

### Architecture: Config → Events

**User creates:** `delivery_configs/rush_hour.yaml`
```yaml
simulation:
  name: "Rush Hour Scenario"
  duration_hours: 24
  start_time: "06:00"
  
delivery_generation:
  type: "poisson"  # Poisson process (random arrivals)
  rate: 0.5  # 0.5 deliveries per minute = ~720/day
  peak_hours:
    - hours: "11:00-13:00"  # Lunch rush
      multiplier: 3.0  # 3x normal rate
    - hours: "17:00-19:00"  # Dinner rush
      multiplier: 2.5
  
location_distribution:
  restaurants:
    # Filter OSM data for restaurants in Glendale
    type: "osm_tag:amenity=restaurant"
    clustering: "high"  # Concentrate in restaurant zones
    probability: 0.7
  residences:
    type: "osm_tag:building=residential"
    clustering: "moderate"  # More spread out
    probability: 0.3
  
constraints:
  robots_available: 5
  starting_location: "glendale_police_station"
  max_delivery_distance_km: 8.0
```

### Implementation Pattern

```python
# app/delivery_generator.py
import yaml
from dataclasses import dataclass
from typing import Callable
import random

@dataclass
class DeliveryRequest:
    timestamp: float  # SimPy time
    restaurant_id: int
    residence_id: int
    priority: str = "normal"

class DeliveryGenerator:
    """Loads YAML config and generates events for SimPy"""
    
    def __init__(self, config_path: str, restaurants: List[Location], residences: List[Location]):
        self.config = yaml.safe_load(open(config_path))
        self.restaurants = restaurants
        self.residences = residences
        self.event_log = []
    
    def generate_events(self) -> Callable:
        """Returns a SimPy process generator"""
        config = self.config['delivery_generation']
        base_rate = config['rate']
        
        def event_generator(env):
            time = 0
            while time < env.now:
                # Apply peak hour multiplier
                current_rate = self._get_rate_at_time(time, base_rate)
                
                # Poisson inter-arrival time
                inter_arrival = random.expovariate(current_rate)
                yield env.timeout(inter_arrival)
                
                # Generate new delivery request
                request = DeliveryRequest(
                    timestamp=env.now,
                    restaurant_id=random.choice(self.restaurants).id,
                    residence_id=random.choice(self.residences).id
                )
                
                # Log and yield to simulation
                self.event_log.append(request)
                yield env.process(self.process_delivery(env, request))
        
        return event_generator
    
    def _get_rate_at_time(self, time: float, base_rate: float) -> float:
        """Apply peak hour multipliers based on time"""
        # TODO: Parse peak_hours from config, return adjusted rate
        return base_rate
```

**Why this works:**
- User provides YAML, zero Python coding needed
- Simulation loads it at startup
- Different configs = different scenarios without code changes
- Easy to test: "What if deliveries doubled?"

---

## Component 2: Real Road Networks with osmnx

### Architecture: Download Once, Route Always

**Pre-processing (run once):**
```python
# app/network_setup.py
import osmnx as ox
import networkx as nx
import pickle

def setup_glendale_network():
    """Download Glendale, CA street network from OSM"""
    
    # Define Glendale bounding box
    glendale = ox.geocode_to_gdf("Glendale, California")
    
    # Download complete street network
    G = ox.graph_from_polygon(glendale.geometry[0], network_type='all')
    
    # Add edge weights (travel time in seconds)
    for u, v, data in G.edges(data=True):
        # Use length (m) and assumed speed (km/h)
        length_m = data.get('length', 0)
        speed_kmh = 15  # City street average
        speed_ms = speed_kmh / 3.6
        travel_time_s = length_m / speed_ms
        data['travel_time'] = travel_time_s
    
    # Cache to disk
    nx.write_gpickle(G, 'glendale_network.pkl')
    return G

# Load cached network
G = nx.read_gpickle('glendale_network.pkl')
```

**In SimPy simulation:**
```python
# app/simulation_engine.py
import networkx as nx

class RobotAgent:
    def __init__(self, env, robot_id, network, db_writer):
        self.env = env
        self.id = robot_id
        self.network = network
        self.db_writer = db_writer
        self.location = get_start_location()  # Police station
    
    def deliver(self, delivery_request):
        """SimPy process: handle one delivery"""
        
        # Route from current location to restaurant
        path = nx.shortest_path(
            self.network,
            source=snap_to_network(self.location, self.network),
            target=snap_to_network(delivery_request.restaurant, self.network),
            weight='travel_time'
        )
        
        # Calculate travel time
        travel_time = sum(
            self.network[path[i]][path[i+1]][0]['travel_time']
            for i in range(len(path)-1)
        )
        
        # Wait for travel
        yield self.env.timeout(travel_time)
        self.location = delivery_request.restaurant
        
        # Log to DB (real-time write)
        self.db_writer.log_movement(
            robot_id=self.id,
            latitude=self.location.lat,
            longitude=self.location.lon,
            timestamp=self.env.now,
            status='at_restaurant'
        )
        
        # Service time at restaurant
        yield self.env.timeout(60)  # 1 minute to pick up
        
        # Route to residence
        path = nx.shortest_path(self.network, ...)
        travel_time = ...
        yield self.env.timeout(travel_time)
        self.location = delivery_request.residence
        
        # Log delivery complete
        self.db_writer.log_movement(...)
        self.db_writer.mark_delivery_complete(delivery_request.id)
```

**Why this works:**
- Real routing, not Euclidean distance
- Cached network = fast lookups (no API calls during simulation)
- NetworkX integrates seamlessly with SimPy
- Can analyze network bottlenecks afterward

---

## Component 3: Real-Time Database Writes

### Architecture: Write During Simulation, Query After

**Database schema (additions for simulation):**
```sql
-- Track simulations
CREATE TABLE IF NOT EXISTS robotics.simulations (
    id SERIAL PRIMARY KEY,
    config_name VARCHAR(255),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    num_robots INT,
    num_deliveries INT,
    total_distance_km FLOAT
);

-- Real-time robot location stream
CREATE TABLE IF NOT EXISTS robotics.robot_locations (
    id SERIAL PRIMARY KEY,
    simulation_id INT REFERENCES robotics.simulations(id),
    robot_id INT,
    latitude FLOAT,
    longitude FLOAT,
    timestamp TIMESTAMP,
    status VARCHAR(50),  -- 'traveling', 'at_restaurant', 'at_residence', 'idle'
    delivery_id INT REFERENCES robotics.deliveries(id)
);
CREATE INDEX idx_sim_time ON robotics.robot_locations(simulation_id, timestamp);

-- Delivery progress
CREATE TABLE IF NOT EXISTS robotics.deliveries (
    id SERIAL PRIMARY KEY,
    simulation_id INT REFERENCES robotics.simulations(id),
    robot_id INT,
    restaurant_id INT,
    residence_id INT,
    requested_at TIMESTAMP,
    picked_up_at TIMESTAMP,
    delivered_at TIMESTAMP,
    distance_km FLOAT,
    duration_seconds INT
);
```

**Real-time writer:**
```python
# app/db_writer.py
import psycopg2
from datetime import datetime
from queue import Queue
import threading

class SimulationDatabaseWriter:
    """Writes simulation data to PostgreSQL in real-time"""
    
    def __init__(self, db_conn_string, simulation_id):
        self.conn = psycopg2.connect(db_conn_string)
        self.cursor = self.conn.cursor()
        self.simulation_id = simulation_id
        
        # Batch writes for performance
        self.buffer = []
        self.buffer_size = 100
        self.write_thread = threading.Thread(target=self._flush_periodically, daemon=True)
        self.write_thread.start()
    
    def log_movement(self, robot_id, lat, lon, timestamp, status, delivery_id=None):
        """Queue a robot movement for writing"""
        self.buffer.append({
            'robot_id': robot_id,
            'lat': lat,
            'lon': lon,
            'timestamp': timestamp,
            'status': status,
            'delivery_id': delivery_id
        })
        
        # Flush if buffer full
        if len(self.buffer) >= self.buffer_size:
            self._flush()
    
    def _flush(self):
        """Write buffered data to DB"""
        if not self.buffer:
            return
        
        sql = """
        INSERT INTO robotics.robot_locations 
        (simulation_id, robot_id, latitude, longitude, timestamp, status, delivery_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        
        data = [
            (self.simulation_id, b['robot_id'], b['lat'], b['lon'], 
             b['timestamp'], b['status'], b['delivery_id'])
            for b in self.buffer
        ]
        
        self.cursor.executemany(sql, data)
        self.conn.commit()
        self.buffer.clear()
    
    def _flush_periodically(self):
        """Background thread: flush every 5 seconds"""
        import time
        while True:
            time.sleep(5)
            self._flush()
    
    def mark_delivery_complete(self, delivery_id, distance_km, duration_s):
        """Mark delivery as completed"""
        sql = """
        UPDATE robotics.deliveries 
        SET delivered_at = NOW(), distance_km = %s, duration_seconds = %s
        WHERE id = %s
        """
        self.cursor.execute(sql, (distance_km, duration_s, delivery_id))
        self.conn.commit()
```

**Why this works:**
- Buffered writes = less DB overhead
- Background thread = non-blocking
- Simulation writes during run, API reads same data
- Historical queries work on same table after simulation ends

---

## Component 4: Live + Historical API

### Architecture: Same Data Source, Different Views

```python
# web_api/app/main.py
from fastapi import FastAPI, Query
from datetime import datetime

app = FastAPI()

# ==================== LIVE QUERIES ====================
# Runs while simulation is active

@app.get("/api/v1/simulation/{sim_id}/live/robots")
async def get_live_robot_locations(
    sim_id: int,
    since_timestamp: float = Query(0, description="Only return movements after this simulation time")
):
    """
    Live query: Return robot locations since last query.
    Used by frontend to update map in real-time during simulation.
    """
    sql = """
    SELECT robot_id, latitude, longitude, timestamp, status 
    FROM robotics.robot_locations
    WHERE simulation_id = %s AND timestamp > %s
    ORDER BY timestamp ASC
    """
    
    result = db.query(sql, (sim_id, since_timestamp))
    return {
        "simulation_id": sim_id,
        "timestamp": datetime.now(),
        "movements": result
    }

@app.get("/api/v1/simulation/{sim_id}/live/deliveries")
async def get_live_deliveries(sim_id: int):
    """Live delivery progress: How many delivered, pending, failed?"""
    sql = """
    SELECT 
        COUNT(*) as total,
        COUNT(CASE WHEN delivered_at IS NOT NULL THEN 1 END) as completed,
        COUNT(CASE WHEN picked_up_at IS NULL THEN 1 END) as pending
    FROM robotics.deliveries
    WHERE simulation_id = %s
    """
    result = db.query(sql, (sim_id,))[0]
    return result

# ==================== HISTORICAL QUERIES ====================
# Run after simulation completes

@app.get("/api/v1/simulation/{sim_id}/history/trajectory")
async def get_robot_trajectory(
    sim_id: int,
    robot_id: int
):
    """
    Historical: Get complete trajectory for one robot.
    Used for playback or analysis after simulation.
    """
    sql = """
    SELECT latitude, longitude, timestamp, status, delivery_id
    FROM robotics.robot_locations
    WHERE simulation_id = %s AND robot_id = %s
    ORDER BY timestamp ASC
    """
    result = db.query(sql, (sim_id, robot_id))
    return {
        "robot_id": robot_id,
        "path": result,
        "total_distance_km": calculate_distance(result)
    }

@app.get("/api/v1/simulation/{sim_id}/history/deliveries")
async def get_deliveries_report(
    sim_id: int,
    robot_id: int = None
):
    """
    Historical: Get delivery statistics.
    Filters by robot_id if provided.
    """
    sql = """
    SELECT 
        delivery_id,
        robot_id,
        restaurant_id,
        residence_id,
        requested_at,
        delivered_at,
        EXTRACT(EPOCH FROM (delivered_at - requested_at)) as service_time_seconds,
        distance_km
    FROM robotics.deliveries
    WHERE simulation_id = %s
    """
    params = [sim_id]
    
    if robot_id:
        sql += " AND robot_id = %s"
        params.append(robot_id)
    
    sql += " ORDER BY delivered_at ASC"
    
    result = db.query(sql, tuple(params))
    return {
        "simulation_id": sim_id,
        "robot_id": robot_id,
        "deliveries": result,
        "avg_service_time": statistics.mean([d['service_time_seconds'] for d in result]),
        "total_distance_km": sum([d['distance_km'] for d in result])
    }

# ==================== PLAYBACK ====================
# Replay simulation state at any point in time

@app.get("/api/v1/simulation/{sim_id}/playback")
async def playback_at_time(
    sim_id: int,
    sim_time_seconds: float
):
    """
    Get simulation state at a specific moment.
    sim_time_seconds = 0 is start, 86400 is end of 1-day simulation.
    """
    sql = """
    SELECT robot_id, latitude, longitude, status, delivery_id
    FROM robotics.robot_locations
    WHERE simulation_id = %s AND timestamp <= %s
    -- Get latest position for each robot before this time
    AND (robot_id, timestamp) IN (
        SELECT robot_id, MAX(timestamp)
        FROM robotics.robot_locations
        WHERE simulation_id = %s AND timestamp <= %s
        GROUP BY robot_id
    )
    """
    result = db.query(sql, (sim_id, sim_time_seconds, sim_id, sim_time_seconds))
    return {
        "simulation_id": sim_id,
        "playback_time": sim_time_seconds,
        "robot_positions": result
    }
```

**Why this works:**
- Same data, different queries
- Live: `WHERE timestamp > last_query` (incremental)
- Historical: `WHERE simulation_id = X` (complete)
- Playback: `WHERE timestamp <= T` (snapshot)
- API doesn't care if simulation is running or finished

---

## End-to-End Data Flow

### Session startup:

```
1. User provides: delivery_configs/rush_hour.yaml
2. System loads:
   - Config → delivery_generator.py
   - Glendale network → glendale_network.pkl
   - Restaurant/residence data → PostgreSQL
3. Simulation starts:
   - Creates 5 robot agents
   - Spawns delivery_generator process
   - Spawns db_writer thread
4. For each delivery event:
   - Generator creates DeliveryRequest
   - Robot calculates route (NetworkX)
   - Robot moves through network (SimPy time)
   - Writes position to DB (buffered)
5. Frontend polls API:
   - GET /api/v1/simulation/{id}/live/robots?since_timestamp=900
   - Gets new movements since 15 minutes ago
   - Updates map in real-time
6. After 24 hours simulated time:
   - Simulation stops
   - Final flush to DB
   - Historical queries now available
```

---

## Key Design Decisions

| Decision | Why | Tradeoff |
|----------|-----|---------|
| **Config-driven delivery** | User customization without code changes | Limited to YAML schema |
| **Real road networks** | Realistic routing, educational value | Slower than Euclidean, needs caching |
| **Real-time DB writes** | Live queries work during simulation | DB overhead (mitigated by buffering) |
| **Buffered writes** | Reduce DB I/O | Slight lag in "live" data (~5s) |
| **SimPy for simulation** | Purpose-built for discrete events | Must think in simulation time |
| **Option A (live writes)** | Shows system working end-to-end | More complex than post-run flush |

---

## Next Implementation Steps

1. **Build delivery generator** - YAML parser + DeliveryGenerator class
2. **Download + cache Glendale network** - osmnx pre-processing
3. **Implement robot agent** - SimPy + NetworkX routing
4. **Real-time DB writer** - Buffered writes with background flush
5. **API endpoints** - Live + historical queries
6. **Frontend playback** - Animate robots on map over time

---

## Questions for Refinement

1. **Delivery config**: Does the YAML structure above feel right, or would you want different parameters?
2. **Travel times**: Should we use real OSM speeds (tagged in data), or fixed assumptions?
3. **Robot constraints**: Do robots run out of battery? Need to return to station? Pick limits?
4. **Multiple simulations**: Can you run two simulations simultaneously (same DB)?
5. **Scaling**: Start with 5 robots, 1 day—how far do you want to scale eventually?

