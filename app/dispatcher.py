"""
Dispatcher — central coordinator that assigns delivery requests to robots.

When a new request arrives:
  - If idle robots are available, assign to the one closest to the restaurant
  - Otherwise, hold it in the pending queue

When a robot becomes idle:
  - If pending requests exist, assign the one whose restaurant is closest to the robot
  - Otherwise, add the robot to the idle pool

Distance is calculated with simple Euclidean on lat/lon — fast, and accurate
enough for dispatch decisions. Actual routing uses NetworkX.
"""

import logging
import math

from delivery_generator import DeliveryRequest

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self):
        self._idle_robots: list = []       # RobotAgent objects currently waiting
        self._pending: list[DeliveryRequest] = []  # requests with no robot yet

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
    # Called by RobotAgent when it finishes a delivery and goes idle
    # ------------------------------------------------------------------

    def robot_available(self, robot):
        if self._pending:
            # Pull the oldest pending request (FIFO).  Using _nearest_request
            # here would make the robot always go to the restaurant closest to
            # its current position, which obscures the Poisson arrival order —
            # the restaurants visited would look proximity-driven rather than
            # randomly generated.
            request = self._pending.pop(0)
            robot.inbox.put(request)
            logger.debug(
                f"Dispatcher gave pending request to robot {robot.index} "
                f"({len(self._pending)} still pending)"
            )
        else:
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
            best         = self._nearest_robot(restaurant_lat, restaurant_lon)
            robot_to_rest = travel_min(
                best.current_lat, best.current_lon,
                restaurant_lat,   restaurant_lon,
            )
            robot_wait = 0.0
        else:
            robot_to_rest = 0.0
            # One cycle per pending delivery ahead in queue (rough lower bound).
            # Each pending delivery takes at least rest→res + pickup minutes.
            cycles     = 1 + len(self._pending)
            robot_wait = cycles * (rest_to_res + pickup_minutes)

        return robot_wait + robot_to_rest + pickup_minutes + rest_to_res

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
