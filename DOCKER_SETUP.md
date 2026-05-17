# Docker Setup for Serve Robotics Simulation

This project uses Docker to containerize the complete Serve Robotics Simulation application with PostgreSQL, Python data gathering/simulation, and a FastAPI web service.

## Architecture

```
┌─────────────────────────────────────────┐
│        Docker Network (robotics_network) │
├─────────────────────────────────────────┤
│                                         │
│  ┌──────────────────┐                   │
│  │  PostgreSQL 16   │                   │
│  │  + PostGIS 3.4   │                   │
│  │  + MobilityDB    │                   │
│  │  (Port 5432)     │                   │
│  └──────────────────┘                   │
│          ▲                               │
│          │ (TCP)                         │
│    ┌─────┴────────┐                     │
│    │              │                     │
│  ┌─┴──────────┐ ┌─┴────────────────┐   │
│  │Python App  │ │   FastAPI Web    │   │
│  │- Data Gather         API         │   │
│  │- Simulation │ - Visualization    │   │
│  │- Storage   │ - REST Endpoints    │   │
│  └────────────┘ └────────────────────┘   │
│                 (Port 8000)               │
│                                          │
└─────────────────────────────────────────┘
```

## Prerequisites

- Docker Desktop or Docker Engine
- Docker Compose (v1.29+)

## Quick Start

### 1. Clone/Setup the Project

```bash
cd /path/to/MSDS436
```

### 2. Create Environment File

```bash
cp .env.example .env
# Edit .env as needed
```

### 3. Build and Start Containers

```bash
# Build all images
docker-compose build

# Start all services
docker-compose up -d

# View logs
docker-compose logs -f
```

### 4. Verify Services

```bash
# Check container status
docker-compose ps

# Test database connection
docker-compose exec postgres psql -U robotics_user -d serve_robotics -c "SELECT version();"

# Test API health
curl http://localhost:8000/health
```

### 5. Run Simulation

```bash
# Execute Python app
docker-compose exec python-app python /app/main.py

# Or keep it running in background
docker-compose logs -f python-app
```

## Directory Structure

```
MSDS436/
├── docker-compose.yml          # Main orchestration file
├── Dockerfile.python           # Python app Dockerfile
├── Dockerfile.webapi           # Web API Dockerfile
├── init-db.sql                # Database initialization script
├── requirements-python.txt     # Python app dependencies
├── requirements-webapi.txt     # Web API dependencies
├── .env.example               # Environment variables template
├── .dockerignore              # Docker build ignore file
├── app/                       # Python application directory
│   ├── main.py               # Entry point
│   ├── data_gathering/       # Data collection modules
│   ├── simulation/           # Discrete event simulation
│   └── data/                 # Output data directory
└── web_api/                  # FastAPI web service
    └── app/
        ├── main.py           # API entry point
        ├── routes/           # API endpoint definitions
        ├── models/           # SQLAlchemy models
        └── templates/        # HTML templates for visualization
```

## Key Services

### PostgreSQL (serve-robotics-db)

- **Image**: postgis/postgis:16-3.4
- **Port**: 5432 (localhost)
- **Database**: serve_robotics
- **User**: robotics_user
- **Password**: robotics_password
- **Extensions**: PostGIS, MobilityDB-ready

**Schema**:
- `robotics.restaurants` - Restaurant locations
- `robotics.residences` - Customer residence locations
- `robotics.robots` - Robot inventory
- `robotics.deliveries` - Delivery orders
- `robotics.robot_locations` - Spatio-temporal tracking data

### Python App (serve-robotics-python)

- **Base Image**: python:3.11-slim
- **Purpose**: Data gathering & discrete event simulation
- **Dependencies**: psycopg2, SQLAlchemy, GeoPandas, Shapely
- **Entry**: `/app/main.py`

**Responsibilities**:
- Gather restaurant and residence data from APIs (Google Maps, OpenStreetMap)
- Run discrete event simulation
- Store results in PostgreSQL

### Web API (serve-robotics-api)

- **Base Image**: python:3.11-slim
- **Framework**: FastAPI
- **Port**: 8000 (localhost)
- **Entry**: uvicorn with auto-reload

**API Endpoints**:
- `GET /health` - Health check
- `GET /api/v1/robots` - Get all robots
- `GET /api/v1/deliveries` - Get all deliveries
- `GET /api/v1/restaurants` - Get restaurant data
- `GET /api/v1/map` - Map visualization data
- `GET /api/v1/stats` - Simulation statistics

## Common Commands

```bash
# View all services
docker-compose ps

# Start services
docker-compose up -d

# Stop services
docker-compose down

# View logs
docker-compose logs [service_name]  # Follow: add -f
docker-compose logs -f              # All services

# Execute commands in containers
docker-compose exec postgres psql -U robotics_user -d serve_robotics
docker-compose exec python-app python /app/main.py
docker-compose exec web-api bash

# Rebuild a specific service
docker-compose build [service_name]

# Remove volumes (WARNING: deletes data)
docker-compose down -v

# View service details
docker inspect serve-robotics-db
```

## Database Access

### From Host Machine
```bash
psql -h localhost -U robotics_user -d serve_robotics
```

### From Python Container
```bash
import psycopg2
conn = psycopg2.connect(
    host="postgres",
    user="robotics_user",
    password="robotics_password",
    database="serve_robotics"
)
```

### From SQLAlchemy
```python
from sqlalchemy import create_engine
engine = create_engine('postgresql://robotics_user:robotics_password@postgres/serve_robotics')
```

## Development Workflow

### 1. Make Code Changes

Edit files in `app/` or `web_api/` directories directly. The docker-compose.yml mounts these as volumes.

### 2. Rebuild Images (if dependencies change)

```bash
docker-compose build [service_name]
docker-compose up -d
```

### 3. View Logs

```bash
docker-compose logs -f [service_name]
```

### 4. Debug Issues

```bash
# Enter container shell
docker-compose exec [service_name] bash

# Check connectivity
docker-compose exec python-app ping postgres
docker-compose exec python-app curl http://web-api:8000/health
```

## Troubleshooting

### PostgreSQL Won't Connect

```bash
# Check if container is running
docker-compose ps postgres

# Check logs
docker-compose logs postgres

# Verify network
docker network inspect robotics_network
```

### Python App Can't Connect to DB

```bash
# Test connectivity inside container
docker-compose exec python-app psql -h postgres -U robotics_user -d serve_robotics -c "SELECT 1;"
```

### Web API Port Already in Use

```bash
# Change port in docker-compose.yml
# Or kill process using port 8000
lsof -i :8000
kill -9 <PID>
```

### Rebuild from Scratch

```bash
docker-compose down -v
docker-compose build --no-cache
docker-compose up -d
```

## Next Steps

1. **Implement Data Gathering** (`app/data_gathering/`)
   - Connect to Google Maps API
   - Fetch restaurants and residences in Glendale, CA
   - Store in PostgreSQL

2. **Build Simulation Engine** (`app/simulation/`)
   - Discrete event simulation framework
   - Robot movement calculations
   - Battery/energy model

3. **Develop API Endpoints** (`web_api/app/routes/`)
   - Query simulation results
   - Real-time robot tracking
   - Delivery statistics

4. **Create Visualization** (`web_api/templates/`)
   - Map display using Folium/Leaflet
   - Real-time robot positions
   - Heatmaps of delivery demand

## Resources

- [Docker Documentation](https://docs.docker.com/)
- [Docker Compose Reference](https://docs.docker.com/compose/compose-file/)
- [PostGIS Documentation](https://postgis.net/documentation/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [SQLAlchemy ORM](https://docs.sqlalchemy.org/)
