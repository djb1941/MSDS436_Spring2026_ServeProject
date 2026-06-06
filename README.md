# Serve Robotics Simulation

## Installation

### Requirements
- Git
- Docker (Docker CLI or Docker Desktop)

### Setup (Utilizing Unix Terminal)

1. Clone the repository to your local machine:
   ```bash
   git clone https://github.com/djb1941/MSDS436_Spring2026_ServeProject.git
   cd MSDS436_Spring2026_ServeProject
   ```

2. Start the application using Docker Compose:
   ```bash
   docker compose up --build
   ```

   > **Note:** On first run, the application will take some time to initialize as it downloads dependencies and loads supporting data.

3. Open your browser and navigate to:
   ```
   http://localhost:8501
   ```

## Running Simulations

Once the containers are running and the UI is loaded, you can start and manage simulations through the web interface. Exceptions being the locations of 'Taxi Stands' and the speed of the robots, those are contained in the app/delivery_configs/default.yaml file.
