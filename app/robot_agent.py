"""
RobotAgent — a SimPy process representing one delivery robot.

Each robot loops forever:
  1. Signal the dispatcher that it is idle
  2. Block on its personal inbox until the dispatcher assigns a delivery
  3. Write the delivery row to DB (robot index is now known)
  4. Route to the restaurant via NetworkX shortest path, yield travel time
  5. Log arrival, mark picked_up in DB, wait 1 min for pickup
  6. Route to the residence, yield travel time
  7. Log arrival, mark delivered in DB, repeat

The dispatcher (not a shared queue) decides which robot gets which delivery
based on proximity, so a robot always knows its assignment before moving.
"""

import logging

import networkx as nx
import osmnx as ox
import simpy
from shapely.geometry import LineString, Point
from shapely.ops import substring

from delivery_generator import DeliveryRequest

logger = logging.getLogger(__name__)

DEPOT_LAT = 34.1478
DEPOT_LON = -118.2551
PICKUP_MINUTES = 1.0        # time at restaurant to collect the order

# Default robot cruising speed.  Serve Robotics robots travel sidewalks at
# roughly 3–6 mph; we default to 6 km/h (~3.7 mph).  The OSM drive network
# stores car speeds (25–45 mph in Glendale), so we ignore the graph's
# `travel_time` edge attribute entirely and compute our own travel times
# from edge lengths and this speed.
DEFAULT_ROBOT_SPEED_KMH = 6.0


class RobotAgent:
    def __init__(
        self,
        env: simpy.Environment,
        robot_index: int,
        G: nx.MultiDiGraph,
        dispatcher,             # Dispatcher
        db_writer,              # SimulationDatabaseWriter
        speed_kmh: float = DEFAULT_ROBOT_SPEED_KMH,
    ):
        self.env = env
        self.index = robot_index
        self.G = G
        self.dispatcher = dispatcher
        self.db_writer = db_writer
        # Convert km/h → m/s once at construction time for fast use in routing
        self._speed_ms = speed_kmh * 1000 / 3600

        # Personal inbox — dispatcher puts exactly one assignment here at a time
        self.inbox = simpy.Store(env, capacity=1)

        # Current position — starts at depot
        self.current_lat = DEPOT_LAT
        self.current_lon = DEPOT_LON
        self.current_node = ox.nearest_nodes(G, DEPOT_LON, DEPOT_LAT)

        # Running totals for shutdown summary
        self.total_distance_km = 0.0
        self.total_deliveries = 0

    # ------------------------------------------------------------------
    # Main SimPy process
    # ------------------------------------------------------------------

    def run(self):
        """Infinite delivery loop — runs for the lifetime of the simulation."""
        while True:
            # Tell the dispatcher this robot is available and clear its route line
            self.db_writer.log_movement(
                robot_index=self.index,
                lat=self.current_lat,
                lon=self.current_lon,
                sim_timestamp=self.env.now * 60,
                status="idle",
            )
            self.db_writer.clear_route(self.index)
            self.dispatcher.robot_available(self)

            # Wait for the dispatcher to assign a delivery
            request: DeliveryRequest = yield self.inbox.get()

            # The delivery row was written at generation time with robot_id=NULL.
            # Claim it now that the robot index is known.
            delivery_id = request.delivery_id
            self.db_writer.claim_delivery(delivery_id, self.index)

            logger.info(
                f"Robot {self.index} claimed delivery #{delivery_id} "
                f"at t={self.env.now:.1f}min"
            )

            # --- Leg 1: current location → restaurant ---
            restaurant_node = ox.nearest_nodes(
                self.G, request.restaurant.lon, request.restaurant.lat
            )
            leg1_time, leg1_dist, leg1_path = self._route_info(self.current_node, restaurant_node)
            if leg1_path:
                origin_stub = self._edge_stub(
                    self.current_lat, self.current_lon,
                    leg1_path[0],
                )
                dest_stub = self._edge_stub(
                    request.restaurant.lat, request.restaurant.lon,
                    leg1_path[-1],
                )
                # origin_stub[-1] == _path_waypoints[0]  (both are leg1_path[0] coords)
                # reversed(dest_stub)[0] == _path_waypoints[-1]  (both are leg1_path[-1])
                # Drop the shared node at each join to avoid duplicate points.
                waypoints = (
                    origin_stub[:-1]
                    + self._path_waypoints(leg1_path)
                    + list(reversed(dest_stub))[1:]
                )
                self.db_writer.update_route(
                    self.index, "to_restaurant",
                    waypoints,
                    self.env.now * 60,
                )

            self.db_writer.log_movement(
                robot_index=self.index,
                lat=self.current_lat,
                lon=self.current_lon,
                sim_timestamp=self.env.now * 60,
                status="traveling",
                delivery_id=delivery_id,
            )
            yield self.env.timeout(leg1_time / 60)  # seconds → minutes

            self.current_lat  = request.restaurant.lat
            self.current_lon  = request.restaurant.lon
            self.current_node = restaurant_node

            self.db_writer.log_movement(
                robot_index=self.index,
                lat=self.current_lat,
                lon=self.current_lon,
                sim_timestamp=self.env.now * 60,
                status="at_restaurant",
                delivery_id=delivery_id,
            )
            self.db_writer.mark_picked_up(delivery_id, self.env.now * 60)
            yield self.env.timeout(PICKUP_MINUTES)

            # --- Leg 2: restaurant → residence ---
            residence_node = ox.nearest_nodes(
                self.G, request.residence.lon, request.residence.lat
            )
            leg2_time, leg2_dist, leg2_path = self._route_info(self.current_node, residence_node)
            if leg2_path:
                origin_stub = self._edge_stub(
                    self.current_lat, self.current_lon,
                    leg2_path[0],
                )
                dest_stub = self._edge_stub(
                    request.residence.lat, request.residence.lon,
                    leg2_path[-1],
                )
                waypoints = (
                    origin_stub[:-1]
                    + self._path_waypoints(leg2_path)
                    + list(reversed(dest_stub))[1:]
                )
                self.db_writer.update_route(
                    self.index, "to_residence",
                    waypoints,
                    self.env.now * 60,
                )

            self.db_writer.log_movement(
                robot_index=self.index,
                lat=self.current_lat,
                lon=self.current_lon,
                sim_timestamp=self.env.now * 60,
                status="traveling",
                delivery_id=delivery_id,
            )
            yield self.env.timeout(leg2_time / 60)  # seconds → minutes

            self.current_lat  = request.residence.lat
            self.current_lon  = request.residence.lon
            self.current_node = residence_node

            self.db_writer.log_movement(
                robot_index=self.index,
                lat=self.current_lat,
                lon=self.current_lon,
                sim_timestamp=self.env.now * 60,
                status="at_residence",
                delivery_id=delivery_id,
            )

            total_dist_km = (leg1_dist + leg2_dist) / 1000
            total_duration_s = int((leg1_time + leg2_time) + PICKUP_MINUTES * 60)

            self.db_writer.mark_delivered(
                delivery_id=delivery_id,
                sim_timestamp=self.env.now * 60,
                distance_km=total_dist_km,
                duration_seconds=total_duration_s,
            )

            self.total_distance_km += total_dist_km
            self.total_deliveries += 1

            logger.info(
                f"Robot {self.index} completed delivery #{delivery_id} "
                f"({total_dist_km:.2f} km, {total_duration_s}s) "
                f"at t={self.env.now:.1f}min"
            )

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def _edge_stub(
        self, lat: float, lon: float, route_node: int
    ) -> list[tuple[float, float]]:
        """
        Return the partial road geometry from the nearest road point to
        (lat, lon) through to route_node, as a list of (lat, lon) tuples.

        Why this approach:
          ox.nearest_edges finds the road segment the building actually sits
          on — typically the block face directly in front of it.  We project
          the building perpendicularly onto that segment, then extract the
          portion of the edge geometry between the projected point and
          route_node using Shapely's substring(), so the stub follows the
          road's actual curve rather than a straight diagonal.

          Intersection nodes ARE the endpoints of every edge's geometry
          LineString, so route_node is almost always u or v of the nearest
          edge.  The stub therefore connects seamlessly: its last coordinate
          equals the first coordinate of _path_waypoints(), with no jump.

        Returns: [road_projected_pt, ...shape_pts..., route_node_coord]

        Callers combine stubs with the main route as:
            origin_stub[:-1] + _path_waypoints(path) + reversed(dest_stub)[1:]
        which avoids duplicating the shared route_node coordinate at each join.
        """
        u, v, key = ox.nearest_edges(self.G, lon, lat)
        edge_data  = self.G[u][v][key]

        if "geometry" in edge_data:
            line = edge_data["geometry"]
        else:
            u_node = self.G.nodes[u]
            v_node = self.G.nodes[v]
            line = LineString(
                [(u_node["x"], u_node["y"]), (v_node["x"], v_node["y"])]
            )

        proj_dist = line.project(Point(lon, lat))

        if route_node == u:
            # route_node is at the start of this edge — extract the portion
            # from u to the projected point, then reverse it so the result
            # runs projected_pt → u (toward the intersection).
            partial = substring(line, 0.0, proj_dist)
            coords  = list(reversed(list(partial.coords)))
        elif route_node == v:
            # route_node is at the end — extract from projected point to v.
            partial = substring(line, proj_dist, line.length)
            coords  = list(partial.coords)
        else:
            # Rare fallback: route_node is not on this edge.
            # Draw a straight line from the projected point to the node.
            proj_pt        = line.interpolate(proj_dist)
            route_node_data = self.G.nodes[route_node]
            coords = [
                (proj_pt.x, proj_pt.y),
                (route_node_data["x"], route_node_data["y"]),
            ]

        return [(y, x) for x, y in coords]  # Shapely (lon,lat) → (lat,lon)

    def _path_waypoints(self, path: list[int]) -> list[tuple[float, float]]:
        """
        Convert a list of OSM node IDs to (lat, lon) tuples, following the
        actual road geometry rather than jumping straight between intersections.

        OSM stores two kinds of points in a road network:
          - Nodes: intersections (or dead-ends) — these are the graph vertices
            that shortest_path() returns.
          - Shape points: intermediate coordinates that describe road curves,
            stored as a Shapely LineString on each edge under
            G[u][v][0]['geometry'].

        If we only use node coordinates and draw straight lines between them,
        the polyline cuts across curves, goes through buildings, etc.  By
        extracting the full edge geometry where it exists, the polyline follows
        the actual road shape.

        Coordinate convention:
          - Shapely LineString coords are (x, y) == (lon, lat) in geographic space.
          - We flip to (lat, lon) to match the rest of the codebase.
        """
        if not path:
            return []

        waypoints: list[tuple[float, float]] = []

        # Always start with the first node
        waypoints.append((self.G.nodes[path[0]]["y"], self.G.nodes[path[0]]["x"]))

        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            edge_data = self.G[u][v][0]  # key=0 selects the first parallel edge

            if "geometry" in edge_data:
                # The edge has a real road geometry — extract every shape point.
                # coords[0] is the u-node (already added); start from coords[1].
                for lon, lat in list(edge_data["geometry"].coords)[1:]:
                    waypoints.append((lat, lon))
            else:
                # No stored geometry (short straight segment) — just add the
                # destination node directly.
                waypoints.append((self.G.nodes[v]["y"], self.G.nodes[v]["x"]))

        return waypoints

    def _route_info(
        self, origin_node: int, dest_node: int
    ) -> tuple[float, float, list[int]]:
        """
        Compute shortest path between two OSM nodes.
        Returns (travel_time_seconds, distance_meters, path_node_ids).
        Falls back to (0, 0, []) if no path exists.

        Routing weight is 'length' (meters) rather than the graph's 'travel_time'
        attribute, because OSMnx derives travel_time from car speed limits
        (25–45 mph in Glendale).  At constant robot speed, shortest distance ==
        shortest time, so using 'length' gives the correct path.  We then compute
        our own travel_time from total distance ÷ self._speed_ms.
        """
        if origin_node == dest_node:
            return 0.0, 0.0, []

        try:
            path = nx.shortest_path(
                self.G, origin_node, dest_node, weight="length"
            )
        except nx.NetworkXNoPath:
            logger.warning(
                f"Robot {self.index}: no path {origin_node} → {dest_node}, skipping"
            )
            return 0.0, 0.0, []

        distance = sum(
            self.G[path[i]][path[i + 1]][0]["length"]
            for i in range(len(path) - 1)
        )
        # Travel time at robot speed (seconds)
        travel_time = distance / self._speed_ms
        return travel_time, distance, path
