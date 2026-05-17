# Serve Robotics Simulation - Session 001 Summary

**Date:** May 2, 2026  
**Status:** Docker infrastructure running, PostgreSQL needs MobilityDB integration  
**Next Step:** Create Dockerfile.postgres for consistency and MobilityDB support

---

## What We Built This Session

### 1. Docker Infrastructure Setup ✅
- Created `docker-compose.yml` - orchestrates 3 services
- Created `Dockerfile.python` - Fedora 40 based Python app
- Created `Dockerfile.webapi` - Fedora 40 based FastAPI service
- Created `init-db.sql` - PostgreSQL schema initialization

### 2. Configuration Files ✅
- `requirements-python.txt` - Python dependencies (no versions pinned)
- `requirements-webapi.txt` - Web API dependencies (no versions pinned)
- `.env.example` - Environment variable template
- `.dockerignore` - Build optimization
- `DOCKER_SETUP.md` - Comprehensive Docker documentation
- `DEPLOYMENT_NOTES.txt` - Pre-deployment checklist

### 3. Application Starters ✅
- `app/main.py` - Python simulation entry point
- `web_api/app/main.py` - FastAPI endpoints scaffold

### 4. Directory Organization ✅
Moved all Docker-related files into `serve/` directory for clean structure:
```
MSDS436/
├── Term Project Option 2_Serve Robotics Simulation.pdf
└── serve/                 # All Docker & app code here
    ├── docker-compose.yml
    ├── Dockerfile.python
    ├── Dockerfile.webapi
    ├── requirements-*.txt
    ├── init-db.sql
    ├── DOCKER_SETUP.md
    ├── DEPLOYMENT_NOTES.txt
    ├── app/
    └── web_api/
```

---

## Key Architecture Decisions

### Why Fedora Instead of Debian?
- User's career focus is RHEL/Enterprise Linux
- Learning dnf instead of apt-get is more career-relevant
- Consistency across all custom images

### Why Separate Services?
1. **Separation of Concerns** - Each service has one job
2. **Independent Scaling** - Can run multiple instances of any service
3. **Technology Flexibility** - Each service uses best tool for the job
4. **Development Workflow** - Can test components independently

### Current Service Architecture
```
┌─────────────────────────────────────────────┐
│      Docker Network (robotics_network)      │
├─────────────────────────────────────────────┤
│                                             │
│  PostgreSQL + PostGIS (Port 5432)          │
│  ↑                    ↑                     │
│  └────────────────────┴────────────────────┐
│  │                                        │
│  ├─→ Python App (simulation engine)       │
│  │   └─ Writes to DB                      │
│  │                                        │
│  └─→ Web API (FastAPI, Port 8000)        │
│      └─ Reads from DB, serves HTTP       │
│                                           │
└───────────────────────────────────────────┘
```

### Package Management Philosophy
- **DNF** manages system packages (postgres, geos, proj, etc.)
- **pip** manages Python packages
- **No version pinning initially** - pip resolves compatibility
- **Will freeze with `pip freeze` before deployment** for reproducibility

---

## Current Docker Status ✅

### All Containers Running and Healthy
```
✅ serve-robotics-db (PostgreSQL)     - Port 5432, healthy
✅ serve-robotics-api (FastAPI)       - Port 8000, healthy
✅ serve-robotics-python (Simulation) - Background process
```

### Verified Working
- ✅ Web API responds to HTTP requests (`curl http://localhost:8000/health`)
- ✅ PostgreSQL initialized with schema (5 tables created)
- ✅ Python app connects to database successfully
- ✅ Services communicate on internal Docker network

### Database Schema (5 Tables)
- `robotics.restaurants` - Restaurant locations
- `robotics.residences` - Customer home locations
- `robotics.robots` - Robot inventory
- `robotics.deliveries` - Delivery orders
- `robotics.robot_locations` - Spatio-temporal tracking

---

## Current Issue: MobilityDB Integration

### What We Discovered
- PostgreSQL currently uses official `postgis/postgis:16-3.4` image (Debian-based)
- MobilityDB extension is commented out in `init-db.sql` (not available in official image)
- This breaks our architecture consistency (other two services are Fedora-based)

### The Inconsistency
```
❌ PostgreSQL: postgis/postgis:16-3.4 (Debian-based, pre-built)
✅ Python App: Custom Dockerfile.python (Fedora-based)
✅ Web API: Custom Dockerfile.webapi (Fedora-based)
```

### Next Task: Create Dockerfile.postgres
Need to create `Dockerfile.postgres` that:
1. Starts with `FROM fedora:40`
2. Installs PostgreSQL 16, PostGIS, and **MobilityDB**
3. Initializes database with our schema
4. Uses same pattern as other two Dockerfiles

**Challenge:** MobilityDB might not be in Fedora repositories
- **Option A:** Try dnf install (fastest, might fail)
- **Option B:** Compile from source (slower, guaranteed to work)
- **Option C:** Use official postgres image, abandon Fedora for this service (practical compromise)

---

## Project Context

### Master's Level Data Science Class
- Focus: Understanding architecture collaboratively
- Project: Serve Robotics Simulation
- Goal: Demonstrate full-stack containerized application

### Project Requirements (from PDF)
1. **Data Gathering** - Python/Go programs for restaurant & residence data
2. **Database Schema** - PostgreSQL with PostGIS and MobilityDB
3. **Simulation** - Discrete event simulation tracking robot movements
4. **SQL Queries** - Store spatio-temporal data

### Glendale, California Focus
- All robot operations in Glendale, CA
- Robots start from Glendale police station
- Point-to-point delivery simulation

---

## Important Learnings This Session

### Docker Concepts Clarified
1. **rpm vs dnf** - rpm is low-level (individual files), dnf is high-level (repository + dependencies)
2. **Base images** - The `FROM` line determines the OS and package manager
3. **Output redirection** - `2>&1` combines error and standard output
4. **`docker-compose ps`** - Shows status of all services
5. **Health checks** - Docker waits for services to be "healthy" before starting dependents

### Python Requirements Management
- Started with pinned versions (e.g., `numpy==1.26.3`)
- Switched to no versions (e.g., `numpy`) to let pip resolve
- Will use `pip freeze` before deployment to capture exact versions
- Teaches: Let package manager do its job during development, pin only before production

### Architectural Consistency
- All services should follow same pattern if possible
- Using mixed approaches (pre-built + custom) creates inconsistency
- Dockerfile.postgres needed to stay consistent with philosophy

---

## Commands Reference

### Start Everything
```bash
cd serve/
docker-compose build
docker-compose up -d
```

### Check Status
```bash
docker-compose ps
docker-compose logs -f
```

### Test API
```bash
curl http://localhost:8000/health
```

### Check Database
```bash
docker-compose exec postgres psql -U robotics_user -d serve_robotics \
  -c "\dt robotics.*"
```

### View Python Logs
```bash
docker-compose logs python-app
```

### Stop Everything
```bash
docker-compose down
```

### Stop and Remove Data
```bash
docker-compose down -v
```

---

## Files Created/Modified This Session

### New Files
- `docker-compose.yml` - Main orchestration
- `Dockerfile.python` - Python app container
- `Dockerfile.webapi` - Web API container
- `init-db.sql` - Database schema
- `requirements-python.txt` - Python dependencies
- `requirements-webapi.txt` - Web API dependencies
- `.env.example` - Environment template
- `.dockerignore` - Build optimization
- `DOCKER_SETUP.md` - Docker documentation
- `DEPLOYMENT_NOTES.txt` - Pre-deployment checklist
- `app/main.py` - Python app starter
- `web_api/app/main.py` - API starter
- `SESSION_001_SUMMARY.md` - This file

### Modified Files
- `docker-compose.yml` - Removed obsolete `version:` attribute

### Deleted Files
- `serve/` (old empty directory)
- `multi-container-app/` (unrelated tutorial)
- `welcome-to-docker/` (unrelated tutorial)

---

## Next Session: PostgreSQL with MobilityDB

### Decision Needed
Which approach for Dockerfile.postgres?
1. **Option A:** Try `dnf install mobilitydb` (might not be available)
2. **Option B:** Compile MobilityDB from source (slower build, guaranteed work)
3. **Option C:** Keep postgis image, skip MobilityDB (practical but inconsistent)

### What We'll Do
1. Choose approach for MobilityDB
2. Create Dockerfile.postgres using Dockerfile.python as template
3. Update docker-compose.yml to use new image
4. Rebuild and verify MobilityDB loads
5. Test database still works with new image

### Then: Implementation
After Docker is fully working:
1. Data gathering module (Python)
2. Discrete event simulation (Python)
3. Web API endpoints
4. Visualization

---

## Session Notes

### What Went Well
- Docker setup was straightforward
- All three services started successfully
- API and database communication verified immediately
- Good teaching moments about architecture consistency

### What We Learned
- Consistency in approach matters (all services should follow same pattern)
- Package manager philosophy: trust during dev, pin before production
- Understanding why decisions were made helps architecture

### Blockers
- MobilityDB not in standard postgis image
- Need to decide compilation/approach strategy

---

**Session Duration:** ~2 hours  
**Status:** Ready for next session on PostgreSQL Dockerfile  
**Confidence Level:** High - Docker infrastructure working, clear next steps
