"""
Dispatcher — central coordinator that assigns delivery requests to robots.

When a new request arrives:
  - If idle robots are available, assign to the one closest to the restaurant
  - Otherwise, hold it in the pending queue

When a robot becomes idle:
  - If pending requests exist, assign the one whose restaurant is closest to the robot
  - Otherwise, apply the configured idle behavior:
      "stay"            → robot stays where it is (added to idle pool)
      "post_restaurant" → robot travels to the nearest unoccupied restaurant
      "post_location"   → robot travels to the nearest unoccupied user-defined stand

IdlePostCommand is sent via the robot's inbox (same channel as DeliveryRequest).
The robot's run() loop checks the type of message it received and handles
repositioning separately before looping back to signal availability.

Distance is calculated with simple Euclidean on lat/lon — fast, and accurate
enough for dispatch decisions. Actual routing uses NetworkX.
"""

import logging
import math
from dataclasses import dataclass

from delivery_generator import DeliveryRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command sent to a robot that should reposition to a standby location
# ---------------------------------------------------------------------------

@dataclass
class IdlePostCommand:
    """
    Instructs a robot to travel to a standby (post) location while idle.
    The dispatcher sends this instead of a DeliveryRequest when there is no
    pending work and the idle strategy calls for repositioning.
    """
    lat: float
    lon: float
    name: str = ""          # human-readable label for logging


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class Dispatcher:
    def __init__(
        self,
        idle_strategy: str = "stay",
        post_restaurants: list = None,   # list of Location objects (from delivery_generator)
        post_locations: list = None,     # list of dicts: {name, lat, lon}
    ):
        self._idle_robots: list = []       # RobotAgent objects currently waiting
        self._pending: list[DeliveryRequest] = []  # requests with no robot yet

        # Idle behavior config
        self._idle_strategy = idle_strategy
        self._post_restaurants = post_restaurants or []   # Location objects
        self._post_locations   = post_locations   or []   # dicts

        # Track which (lat, lon) spots are currently claimed by a repositioning robot.
        # robot_index → (lat, lon) so we can unclaim on arrival.
        self._claimed_posts: dict[int, tuple[float, float]] = {}

    # ------------------------------------------------------------------
    # Called by DeliveryGenerator when a new request is generated
    # ------------------------------------------------------------------

    def request_delivery(self, request: DeliveryRequest):
        if self._idle_robots:
            robot = self._nearest_robot(request.restaurant.lat, request.restaurant.lon)
            self._idle_robots.remove(robot)
            robot.inbox.put(request)
            logger.debug(
                f"Dispatcher assigned delivery to robot {robot.index} "
                f"(closest to restaurant {request.restaurant.db_id})"
            )
        else:
            self._pending.append(request)
            logger.debug(
                f"Dispatcher queued request — no idle robots "
                f"({len(self._pending)} pending)"
            )

    # ------------------------------------------------------------------
    # Called by RobotAgent when it finishes a delivery (or repositioning)
    # and is ready for new work
    # ------------------------------------------------------------------

    def robot_available(self, robot):
        # Release any post-location claim this robot held
        self._claimed_posts.pop(robot.index, None)

        if self._pending:
            # Pull the oldest pending request (FIFO).
            request = self._pending.pop(0)
            robot.inbox.put(request)
            logger.debug(
                f"Dispatcher gave pending request to robot {robot.index} "
                f"({len(self._pending)} still pending)"
            )
        else:
            # No work — apply idle strategy
            post_cmd = self._idle_post_command(robot)
            if post_cmd:
                # Claim the spot and send the robot there
                self._claimed_posts[robot.index] = (post_cmd.lat, post_cmd.lon)
                robot.inbox.put(post_cmd)
                logger.debug(
                    f"Robot {robot.index} repositioning → '{post_cmd.name}' "
                    f"({post_cmd.lat:.5f}, {post_cmd.lon:.5f})"
                )
            else:
                # "stay" strategy or no suitable location found — park in-place
                self._idle_robots.append(robot)
                logger.debug(
                    f"Robot {robot.index} idle — pool size={len(self._idle_robots)}"
                )

    # ------------------------------------------------------------------
    # ETA estimation (used by DeliveryGenerator for rejection check)
    # ------------------------------------------------------------------

    def estimate_delivery_eta(
        self,
        restaurant_lat: float, restaurant_lon: float,
        residence_lat: float,  residence_lon: float,
        speed_kmh: float,
        pickup_minutes: float,
    ) -> float:
        """
        Estimate total delivery time in minutes for a prospective request:
            robot_wait  +  robot→restaurant  +  pickup  +  restaurant→residence

        Uses haversine distances scaled by 1.4 to approximate road distance
        (straight-line × 1.4 is a reasonable city-block overhead factor).

        If an idle robot is available, robot_wait = 0 and robot→restaurant is
        the actual nearest idle robot's travel time.

        If no idle robot is available, we approximate the wait as one full
        delivery cycle (restaurant→residence + pickup) — a conservative but
        simple estimate that keeps the logic self-contained.  A single pending
        delivery adds one cycle; multiple pending add proportionally more.
        """
        ROAD_FACTOR = 1.4

        def travel_min(la1, lo1, la2, lo2):
            return _haversine_km(la1, lo1, la2, lo2) * ROAD_FACTOR / speed_kmh * 60

        rest_to_res = travel_min(
            restaurant_lat, restaurant_lon,
            residence_lat,  residence_lon,
        )

        if self._idle_robots:
            best          = self._nearest_robot(restaurant_lat, restaurant_lon)
            robot_to_rest = travel_min(
                best.current_lat, best.current_lon,
                restaurant_lat,   restaurant_lon,
            )
            robot_wait = 0.0

        elif self._claimed_posts and not self._pending:
            # All robots are repositioning to standby posts (post_restaurant /
            # post_location strategy) with no deliveries already queued.
            # They will be available as soon as they arrive at their posts —
            # use the nearest claimed post location as the robot start point.
            best_lat, best_lon = min(
                self._claimed_posts.values(),
                key=lambda c: _euclidean(c[0], c[1], restaurant_lat, restaurant_lon),
            )
            robot_to_rest = travel_min(best_lat, best_lon, restaurant_lat, restaurant_lon)
            robot_wait    = 0.0

        else:
            robot_to_rest = 0.0
            # One cycle per pending delivery ahead in queue (rough lower bound).
            # Each pending delivery takes at least rest→res + pickup minutes.
            cycles     = 1 + len(self._pending)
            robot_wait = cycles * (rest_to_res + pickup_minutes)

        return robot_wait + robot_to_rest + pickup_minutes + rest_to_res

    # ------------------------------------------------------------------
    # Idle post logic — pick a standby location for a robot with no work
    # ------------------------------------------------------------------

    def _idle_post_command(self, robot) -> IdlePostCommand | None:
        """
        Return an IdlePostCommand for the robot to travel to, or None if the
        strategy is "stay" or no suitable location is available.
        """
        strategy = self._idle_strategy

        if strategy == "post_restaurant":
            return self._nearest_unoccupied_restaurant(robot)

        if strategy == "post_location":
            return self._nearest_unoccupied_post_location(robot)

        return None  # "stay"

    def _nearest_unoccupied_restaurant(self, robot) -> IdlePostCommand | None:
        """
        Pick a standby restaurant for an idle robot, or None to stay put.

        Exclusion is NODE-AWARE, not coordinate-aware. Travel time is computed
        on the road GRAPH: several restaurants (and the robot's own spot) can
        have distinct lat/lon yet snap to the SAME OSM node via
        ox.nearest_nodes(). A move to a restaurant on the robot's current node
        routes in zero distance — a zero-delay SimPy timeout that never advances
        the clock — so the whole simulation freezes. Two rules prevent that:

          1. If the robot is already standing on a post-restaurant node, it is
             already optimally posted → return None (stay and wait for work).
          2. Otherwise pick the nearest restaurant whose node isn't the robot's
             own node nor occupied/claimed by another robot, guaranteeing a real,
             time-advancing move.
        """
        if not self._post_restaurants:
            return None

        robot_node = getattr(robot, "current_node", None)

        # Rule 1 — already posted at a restaurant node → stay.
        post_nodes = {getattr(r, "node", None) for r in self._post_restaurants}
        if robot_node in post_nodes:
            return None

        # Rule 2 — nearest restaurant on a free node.
        occupied  = self._occupied_coords(robot)            # coords claimed/occupied
        occupied.add((round(robot.current_lat, 6), round(robot.current_lon, 6)))
        occupied_nodes = {robot_node}
        for other in self._idle_robots:
            if other.index != robot.index:
                occupied_nodes.add(getattr(other, "current_node", None))

        candidates = [
            r for r in self._post_restaurants
            if (round(r.lat, 6), round(r.lon, 6)) not in occupied
            and getattr(r, "node", None) not in occupied_nodes
        ]
        if not candidates:
            return None

        best = min(
            candidates,
            key=lambda r: _euclidean(robot.current_lat, robot.current_lon, r.lat, r.lon),
        )
        return IdlePostCommand(lat=best.lat, lon=best.lon, name=best.name or f"Restaurant #{best.db_id}")

    def _nearest_unoccupied_post_location(self, robot) -> IdlePostCommand | None:
        """
        Find the nearest user-defined post location not already claimed by
        another robot.
        """
        if not self._post_locations:
            logger.warning(
                "idle_strategy='post_location' but no post_locations defined in config "
                "— falling back to 'stay'"
            )
            return None

        # Node-aware, same as _nearest_unoccupied_restaurant: stay if already on
        # a stand's node, otherwise move to the nearest free-node stand.
        robot_node = getattr(robot, "current_node", None)
        stand_nodes = {p.get("node") for p in self._post_locations}
        if robot_node in stand_nodes:
            return None

        occupied  = self._occupied_coords(robot)
        occupied.add((round(robot.current_lat, 6), round(robot.current_lon, 6)))
        occupied_nodes = {robot_node}
        for other in self._idle_robots:
            if other.index != robot.index:
                occupied_nodes.add(getattr(other, "current_node", None))

        candidates = [
            p for p in self._post_locations
            if (round(p["lat"], 6), round(p["lon"], 6)) not in occupied
            and p.get("node") not in occupied_nodes
        ]
        if not candidates:
            # All stands occupied (including robot's own) — stay put
            return None

        best = min(
            candidates,
            key=lambda p: _euclidean(robot.current_lat, robot.current_lon, p["lat"], p["lon"]),
        )
        return IdlePostCommand(lat=best["lat"], lon=best["lon"], name=best.get("name", "Post"))

    def _occupied_coords(self, requesting_robot) -> set[tuple[float, float]]:
        """
        Return the set of (lat, lon) coordinates that are currently occupied
        by idle robots or robots en route to a post location.
        Includes the requesting robot's own current position so it is never
        dispatched to the restaurant it just arrived at (which would produce a
        zero-travel-time loop that hangs SimPy).
        """
        occupied = set()
        # Always exclude the robot's own current location from candidates.
        occupied.add((round(requesting_robot.current_lat, 6), round(requesting_robot.current_lon, 6)))
        for r in self._idle_robots:
            if r.index != requesting_robot.index:
                occupied.add((round(r.current_lat, 6), round(r.current_lon, 6)))
        for robot_idx, coords in self._claimed_posts.items():
            if robot_idx != requesting_robot.index:
                occupied.add((round(coords[0], 6), round(coords[1], 6)))
        return occupied

    # ------------------------------------------------------------------
    # Distance helpers
    # ------------------------------------------------------------------

    def _nearest_robot(self, target_lat: float, target_lon: float):
        """Return the idle robot closest to the given coordinates."""
        return min(
            self._idle_robots,
            key=lambda r: _euclidean(r.current_lat, r.current_lon, target_lat, target_lon),
        )

    def _nearest_request(self, robot_lat: float, robot_lon: float):
        """Return the pending request whose restaurant is closest to the robot."""
        return min(
            self._pending,
            key=lambda req: _euclidean(robot_lat, robot_lon, req.restaurant.lat, req.restaurant.lon),
        )


def _euclidean(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return math.sqrt((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))
