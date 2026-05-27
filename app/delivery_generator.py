"""
DeliveryGenerator — SimPy process that produces delivery requests over
simulated time using a Poisson arrival process with peak-hour multipliers.

The generator puts DeliveryRequest objects into a simpy.Store. Robot agents
block on store.get() and pull the next request when they become free.
"""

import logging
import random
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone

import psycopg2
import psycopg2.extras
import simpy
import yaml

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class Location:
    db_id: int
    lat: float
    lon: float
    name: str = ""


@dataclass
class DeliveryRequest:
    delivery_id: int        # DB id from robotics.deliveries
    restaurant: Location
    residence: Location
    requested_at: float     # SimPy env.now when the request was created


# ------------------------------------------------------------------
# DeliveryGenerator
# ------------------------------------------------------------------

class DeliveryGenerator:
    """
    Loads simulation config from YAML and runs as a SimPy process.

    Usage:
        store = simpy.Store(env)
        gen = DeliveryGenerator(env, db_url, config_path, db_writer, store)
        env.process(gen.run())
        env.run(until=duration)
    """

    def __init__(
        self,
        env: simpy.Environment,
        db_url: str,
        config_path: str,
        dispatcher,             # Dispatcher
        sim_id: int = None,
    ):
        self.env = env
        self.dispatcher = dispatcher
        self._db_url = db_url
        self._sim_id = sim_id

        # Load YAML config
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        gen_cfg = self.config["delivery_generation"]
        self.base_rate = gen_cfg["rate"]          # deliveries per minute
        self.peak_hours = self._parse_peak_hours(gen_cfg.get("peak_hours", []))

        sim_cfg = self.config["simulation"]
        self.duration_minutes = sim_cfg["duration_hours"] * 60
        self.start_hour = self._parse_start_hour(sim_cfg.get("start_time", "00:00"))

        con_cfg = self.config.get("constraints", {})
        self._max_delivery_time_minutes = con_cfg.get("max_delivery_time_minutes", 30.0)
        self._robot_speed_kmh           = con_cfg.get("robot_speed_kmh", 6.0)
        self._pickup_minutes            = con_cfg.get("pickup_minutes", 1.0)

        # Load restaurants and residences from DB
        self.restaurants = self._load_locations(db_url, "restaurants", name_col=True)
        self.residences  = self._load_locations(db_url, "residences",  name_col=False)

        logger.info(
            f"DeliveryGenerator ready: {len(self.restaurants)} restaurants, "
            f"{len(self.residences)} residences, base rate={self.base_rate}/min"
        )

    # ------------------------------------------------------------------
    # SimPy process
    # ------------------------------------------------------------------

    def run(self):
        """
        Main SimPy generator process. Runs for the full simulation duration,
        yielding timeouts drawn from an exponential distribution.
        """
        while self.env.now < self.duration_minutes:
            # Current rate adjusted for peak hours
            rate = self._rate_at(self.env.now)

            # Exponential inter-arrival time (minutes between orders)
            # expovariate(rate) gives mean = 1/rate
            inter_arrival = random.expovariate(rate)

            yield self.env.timeout(inter_arrival)

            # Don't generate past the end of the simulation
            if self.env.now >= self.duration_minutes:
                break

            self._generate_request()

    # ------------------------------------------------------------------
    # Request creation
    # ------------------------------------------------------------------

    def _generate_request(self):
        """
        Pick a random restaurant and residence.

        Before dispatching, estimate total delivery time (robot wait + travel
        to restaurant + pickup + travel to residence).  If the estimate exceeds
        max_delivery_time_minutes the request is rejected and logged to the
        rejected_deliveries table — no robot is assigned and no delivery row
        is written.  Otherwise the delivery row is written and handed to the
        dispatcher.
        """
        restaurant = random.choice(self.restaurants)
        residence  = random.choice(self.residences)

        estimated_minutes = self.dispatcher.estimate_delivery_eta(
            restaurant_lat=restaurant.lat, restaurant_lon=restaurant.lon,
            residence_lat=residence.lat,   residence_lon=residence.lon,
            speed_kmh=self._robot_speed_kmh,
            pickup_minutes=self._pickup_minutes,
        )

        if estimated_minutes > self._max_delivery_time_minutes:
            self._write_rejected_delivery(
                restaurant_id=restaurant.db_id,
                residence_id=residence.db_id,
                sim_timestamp=self.env.now * 60,
                estimated_minutes=estimated_minutes,
            )
            logger.debug(
                f"t={self.env.now:.1f}min  Rejected: ETA {estimated_minutes:.1f}min "
                f"> max {self._max_delivery_time_minutes}min"
            )
            return

        delivery_id = self._write_pending_delivery(
            restaurant_id=restaurant.db_id,
            residence_id=residence.db_id,
            sim_timestamp=self.env.now * 60,
        )

        request = DeliveryRequest(
            delivery_id=delivery_id,
            restaurant=restaurant,
            residence=residence,
            requested_at=self.env.now,
        )

        self.dispatcher.request_delivery(request)
        logger.debug(
            f"t={self.env.now:.1f}min  Generated delivery #{delivery_id}: "
            f"restaurant {restaurant.db_id} → residence {residence.db_id} "
            f"(ETA {estimated_minutes:.1f}min)"
        )

    def _write_pending_delivery(
        self, restaurant_id: int, residence_id: int, sim_timestamp: float
    ) -> int:
        """
        Insert a delivery row with robot_id = NULL and return the new id.
        Called at generation time so the delivery is visible to the API
        before any robot has been assigned.
        """
        requested_at = datetime.fromtimestamp(sim_timestamp, tz=timezone.utc)
        conn = psycopg2.connect(self._db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO robotics.deliveries
                        (simulation_id, restaurant_id, residence_id, requested_at)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (self._sim_id, restaurant_id, residence_id, requested_at),
                )
                delivery_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return delivery_id

    # ------------------------------------------------------------------
    # Rate calculation
    # ------------------------------------------------------------------

    def _rate_at(self, sim_time_minutes: float) -> float:
        """
        Return the delivery rate (per minute) at the given simulation time,
        applying peak-hour multipliers when applicable.

        sim_time_minutes is measured from the simulation start_hour.
        """
        # Convert sim time to a wall-clock hour (float) for comparison
        current_hour = (self.start_hour + sim_time_minutes / 60) % 24

        for start_h, end_h, multiplier in self.peak_hours:
            if start_h <= current_hour < end_h:
                return self.base_rate * multiplier

        return self.base_rate

    # ------------------------------------------------------------------
    # DB loading
    # ------------------------------------------------------------------

    def _write_rejected_delivery(
        self,
        restaurant_id: int,
        residence_id: int,
        sim_timestamp: float,
        estimated_minutes: float,
    ):
        """Write a rejected request to rejected_deliveries for dashboard tracking."""
        requested_at = datetime.fromtimestamp(sim_timestamp, tz=timezone.utc)
        conn = psycopg2.connect(self._db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO robotics.rejected_deliveries
                        (simulation_id, restaurant_id, residence_id,
                         requested_at, estimated_minutes, reason)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self._sim_id, restaurant_id, residence_id,
                        requested_at, estimated_minutes,
                        "exceeds_max_delivery_time",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _load_locations(
        self, db_url: str, table: str, name_col: bool
    ) -> list[Location]:
        """
        Pull all rows from robotics.restaurants or robotics.residences.
        ST_X / ST_Y extract longitude and latitude from the PostGIS geometry.
        """
        name_select = ", name" if name_col else ", '' AS name"
        sql = f"""
            SELECT id{name_select},
                   ST_Y(location) AS lat,
                   ST_X(location) AS lon
            FROM robotics.{table}
        """
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        finally:
            conn.close()

        return [
            Location(db_id=r["id"], lat=r["lat"], lon=r["lon"], name=r["name"])
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Config parsing helpers
    # ------------------------------------------------------------------

    def _parse_peak_hours(
        self, peak_cfg: list[dict]
    ) -> list[tuple[float, float, float]]:
        """
        Convert peak_hours config entries into (start_hour, end_hour, multiplier)
        tuples where hours are floats (e.g. 11.5 = 11:30am).

        Input format:
            - hours: "11:00-13:00"
              multiplier: 3.0
        """
        parsed = []
        for entry in peak_cfg:
            start_str, end_str = entry["hours"].split("-")
            start_h = self._hhmm_to_float(start_str)
            end_h   = self._hhmm_to_float(end_str)
            parsed.append((start_h, end_h, float(entry["multiplier"])))
        return parsed

    def _parse_start_hour(self, time_str: str) -> float:
        """Convert 'HH:MM' string to a float hour (e.g. '06:30' → 6.5)."""
        return self._hhmm_to_float(time_str)

    @staticmethod
    def _hhmm_to_float(hhmm: str) -> float:
        """'HH:MM' → float hours."""
        h, m = hhmm.strip().split(":")
        return int(h) + int(m) / 60
