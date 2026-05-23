-- Enable PostGIS extension
CREATE EXTENSION IF NOT EXISTS postgis;

-- Enable MobilityDB extension (if available in the image)
-- CREATE EXTENSION IF NOT EXISTS mobilitydb;

-- Create schema for the simulation
CREATE SCHEMA IF NOT EXISTS robotics;

-- Restaurants table
CREATE TABLE robotics.restaurants (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    address VARCHAR(255) NOT NULL,
    location GEOMETRY(Point, 4326) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Customer residences table
CREATE TABLE robotics.residences (
    id SERIAL PRIMARY KEY,
    address VARCHAR(255) NOT NULL,
    location GEOMETRY(Point, 4326) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Robots table
CREATE TABLE robotics.robots (
    id SERIAL PRIMARY KEY,
    robot_id VARCHAR(50) NOT NULL UNIQUE,
    home_location GEOMETRY(Point, 4326) NOT NULL,
    status VARCHAR(50) DEFAULT 'idle',
    battery_level FLOAT DEFAULT 100.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Simulations table (one row per simulation run)
CREATE TABLE robotics.simulations (
    id SERIAL PRIMARY KEY,
    config_name VARCHAR(255),
    num_robots INT NOT NULL DEFAULT 5,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    total_deliveries INT,
    total_distance_km FLOAT,
    status VARCHAR(50) DEFAULT 'running'  -- 'running', 'completed', 'failed'
);

-- Deliveries table
CREATE TABLE robotics.deliveries (
    id SERIAL PRIMARY KEY,
    simulation_id INT NOT NULL REFERENCES robotics.simulations(id),
    robot_id INTEGER NOT NULL REFERENCES robotics.robots(id),
    restaurant_id INTEGER NOT NULL REFERENCES robotics.restaurants(id),
    residence_id INTEGER NOT NULL REFERENCES robotics.residences(id),
    requested_at TIMESTAMP NOT NULL,
    picked_up_at TIMESTAMP,
    delivered_at TIMESTAMP,
    distance_km FLOAT,
    duration_seconds INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Robot locations history (spatio-temporal data)
CREATE TABLE robotics.robot_locations (
    id SERIAL PRIMARY KEY,
    simulation_id INT NOT NULL REFERENCES robotics.simulations(id),
    robot_id INTEGER NOT NULL REFERENCES robotics.robots(id),
    location GEOMETRY(Point, 4326) NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    status VARCHAR(50) DEFAULT 'idle',  -- 'traveling', 'at_restaurant', 'at_residence', 'idle'
    delivery_id INT REFERENCES robotics.deliveries(id),
    speed_mps FLOAT,
    heading_degrees FLOAT
);

-- Create indexes for performance
CREATE INDEX idx_robot_locations_robot_id ON robotics.robot_locations(robot_id);
CREATE INDEX idx_robot_locations_timestamp ON robotics.robot_locations(timestamp);
CREATE INDEX idx_robot_locations_sim_time ON robotics.robot_locations(simulation_id, timestamp);
CREATE INDEX idx_robot_locations_geom ON robotics.robot_locations USING GIST(location);
CREATE INDEX idx_deliveries_simulation_id ON robotics.deliveries(simulation_id);
CREATE INDEX idx_restaurants_location ON robotics.restaurants USING GIST(location);
CREATE INDEX idx_residences_location ON robotics.residences USING GIST(location);

-- Grant permissions to the application user
GRANT ALL PRIVILEGES ON SCHEMA robotics TO robotics_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA robotics TO robotics_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA robotics TO robotics_user;
