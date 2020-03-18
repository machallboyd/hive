from __future__ import annotations

from typing import NamedTuple, Dict, Optional, TYPE_CHECKING

import immutables
from h3 import h3

from hive.model.energy.charger import Charger
from hive.model.energy.energysource import EnergySource
from hive.model.energy.powercurve import Powercurve
from hive.model.energy.powertrain import Powertrain
from hive.model.passenger import Passenger
from hive.model.roadnetwork.link import Link
from hive.model.roadnetwork.roadnetwork import RoadNetwork
from hive.model.roadnetwork.route import Route
from hive.model.roadnetwork.routetraversal import traverse, RouteTraversal
from hive.model.vehicle.vehicle_type import VehicleType
from hive.model.vehicle.vehiclestate import VehicleState, VehicleStateCategory
from hive.util.exception import EntityError
from hive.util.helpers import DictOps
from hive.util.typealiases import *
from hive.util.units import Kilometers, Seconds, SECONDS_TO_HOURS, Currency

if TYPE_CHECKING:
    from hive.runner.environment import Environment


class Vehicle(NamedTuple):
    """
    Tuple that represents a vehicle in the simulation.

    :param id: A unique vehicle id.
    :type id: :py:obj:`VehicleId`
    :param powertrain_id: Id for the vehicle's respective powertrain
    :type powertrain_id: :py:obj:`PowertrainId`
    :param powercurve_id: Id for the vehicle's respective powercurve
    :type powercurve_id: :py:obj:`PowercurveId`
    :param energy_source: The energy source for the vehicle
    :type energy_source: :py:obj:`EnergySource`
    :param link: The current location of the vehicle
    :type link: :py:obj:`Link`
    :param operating_cost_km: the operating cost per kilometer of this vehicle
    :type operating_cost_km: :py:obj:`Currency`
    :param route: The route of the vehicle. Could be empty.
    :type route: :py:obj:`Route`
    :param vehicle_state: The state that the vehicle is in.
    :type vehicle_state: :py:obj:`VehicleState`
    :param passengers: A map of passengers that are in the vehicle. Could be empty
    :type passengers: :py:obj:`Dict[PasengerId, Passengers]`
    :param charger_intent: The charger type a vehicle intends to plug into.
    :type charger_intent: :py:obj:`Optional[Charger]`
    :param idle_time_s: A counter to track how long the vehicle has been idle.
    :type idle_time_s: :py:obj:`seconds`
    :param distance_traveled: A accumulator to track how far a vehicle has traveled.
    :type distance_traveled_km: :py:obj:`kilometers`
    """
    # core vehicle properties
    id: VehicleId
    powertrain_id: PowertrainId
    powercurve_id: PowercurveId
    energy_source: EnergySource
    link: Link
    operating_cost_km: Currency

    # vehicle planning/operational properties
    route: Route = ()
    vehicle_state: VehicleState = VehicleState.IDLE
    passengers: immutables.Map[PassengerId, Passenger] = immutables.Map()
    charger_intent: Optional[Charger] = None

    # vehicle analytical properties
    balance: Currency = 0.0
    idle_time_seconds: Seconds = 0
    distance_traveled_km: Kilometers = 0.0

    @property
    def geoid(self):
        return self.link.start

    @classmethod
    def from_row(cls, row: Dict[str, str], road_network: RoadNetwork, env: Environment) -> Vehicle:
        """
        reads a csv row from file to generate a Vehicle

        :param row: a row of a .csv which matches hive.util.pattern.vehicle_regex.
        this string will be stripped of whitespace characters (no spaces allowed in names!)
        :param road_network: the road network, used to find the vehicle's location in the sim
        :param env: the scenario environment
        :return: a vehicle, or, an IOError if failure occurred.
        """

        if 'vehicle_id' not in row:
            raise IOError("cannot load a vehicle without a 'vehicle_id'")
        elif 'lat' not in row:
            raise IOError("cannot load a vehicle without a 'lat'")
        elif 'lon' not in row:
            raise IOError("cannot load a vehicle without a 'lon'")
        else:
            try:
                vehicle_id = row['vehicle_id']
                lat = float(row['lat'])
                lon = float(row['lon'])
                vehicle_type_id = row['vehicle_type_id']
                vehicle_type: VehicleType = env.vehicle_types.get(vehicle_type_id)
                if vehicle_type is None:
                    file = env.config.io.vehicle_types_file
                    raise IOError(f"cannot find vehicle_type {vehicle_type_id} in provided vehicle_type_file {file}")
                powertrain_id = vehicle_type.powertrain_id
                powercurve_id = vehicle_type.powercurve_id
                energy_type = env.energy_types.get(powercurve_id)
                capacity = vehicle_type.capacity_kwh
                ideal_energy_limit = vehicle_type.ideal_energy_limit_kwh
                max_charge_acceptance = vehicle_type.max_charge_acceptance
                operating_cost_km = vehicle_type.operating_cost_km
                initial_soc = float(row['initial_soc'])

                if not 0.0 <= initial_soc <= 1.0:
                    raise IOError(f"initial soc for vehicle: '{initial_soc}' must be in range [0,1]")

                energy_source = EnergySource.build(powercurve_id,
                                                   energy_type,
                                                   capacity,
                                                   ideal_energy_limit,
                                                   max_charge_acceptance,
                                                   initial_soc)

                geoid = h3.geo_to_h3(lat, lon, road_network.sim_h3_resolution)
                start_link = road_network.link_from_geoid(geoid)

                return Vehicle(
                    id=vehicle_id,
                    powertrain_id=powertrain_id,
                    powercurve_id=powercurve_id,
                    energy_source=energy_source,
                    link=start_link,
                    operating_cost_km=operating_cost_km
                )

            except ValueError:
                raise IOError(f"a numeric value could not be parsed from {row}")

    def __repr__(self) -> str:
        return f"Vehicle({self.id},{self.vehicle_state},{self.energy_source})"

    def charge(self,
               powercurve: Powercurve,
               duration_seconds: Seconds) -> Vehicle:

        """
        applies a charge event to a vehicle

        :param powercurve: the vehicle's powercurve model
        :param station: the station we are charging at
        :param duration_seconds: duration_seconds of this time step in seconds
        :return: the updated Vehicle
        """

        if not self.charger_intent:
            raise EntityError("Vehicle attempting to charge but has no charger intent")
        elif self.energy_source.is_at_ideal_energy_limit():
            # should not reach here, terminal state mechanism should catch this condition first
            return self
        else:
            # charge energy source
            updated_energy_source = powercurve.refuel(self.energy_source, self.charger_intent, duration_seconds)
            return self._replace(
                energy_source=updated_energy_source
            )

    def move(self, road_network: RoadNetwork, power_train: Powertrain, duration_seconds: Seconds) -> Optional[Vehicle]:
        """
        Moves the vehicle and consumes energy.

        :param road_network: the road network
        :param power_train: the vehicle's powertrain model
        :param duration_seconds: the duration_seconds of this move step in seconds
        :return: the updated vehicle or None if moving is not possible.
        """
        if not self.has_route():
            return self.transition(VehicleState.IDLE)

        traverse_result = traverse(
            route_estimate=self.route,
            duration_seconds=duration_seconds,
        )

        if not traverse_result:
            # TODO: Need to think about edge case where vehicle gets route where origin=destination.
            no_route_veh = self.assign_route(tuple())
            return no_route_veh

        experienced_route = traverse_result.experienced_route
        energy_used = power_train.energy_cost(experienced_route)
        step_distance_km = traverse_result.traversal_distance_km
        remaining_route = traverse_result.remaining_route

        # todo: we allow the agent to traverse only bounded by time, not energy;
        #   so, it is possible for the vehicle to travel farther in a time step than
        #   they have fuel to travel. this can create an error on the location of
        #   any agents at the time step where they run out of fuel. feels like an
        #   acceptable edge case but we could improve. rjf 20200309

        updated_energy_source = self.energy_source.use_energy(energy_used)
        less_energy_vehicle = self._replace(energy_source=updated_energy_source).assign_route(remaining_route)
        updated_vehicle = less_energy_vehicle.transition(
            VehicleState.OUT_OF_SERVICE) if updated_energy_source.is_empty() else less_energy_vehicle

        if not updated_vehicle:
            return None
        elif not remaining_route:
            geoid = experienced_route[-1].end
            return updated_vehicle._replace(
                link=road_network.link_from_geoid(geoid),
                distance_traveled_km=self.distance_traveled_km + step_distance_km,
            )
        else:
            return updated_vehicle._replace(
                link=remaining_route[0],
                distance_traveled_km=self.distance_traveled_km + step_distance_km,
            )

    def idle(self, time_step_seconds: Seconds) -> Vehicle:
        """
        Performs an idle step.

        :param time_step_seconds: duration_seconds of the idle step in seconds
        :return: the updated vehicle
        """
        if self.vehicle_state != VehicleState.IDLE:
            raise EntityError("vehicle.idle() method called but vehicle not in IDLE state.")

        idle_energy_rate = 0.8  # (unit.kilowatthour / unit.hour)

        idle_energy_kwh = idle_energy_rate * (time_step_seconds * SECONDS_TO_HOURS)
        updated_energy_source = self.energy_source.use_energy(idle_energy_kwh)
        less_energy_vehicle = self.modify_energy_source(updated_energy_source)

        next_idle_time = (less_energy_vehicle.idle_time_seconds + time_step_seconds)
        vehicle_w_stats = less_energy_vehicle._replace(idle_time_seconds=next_idle_time)

        return vehicle_w_stats

    def can_transition(self, vehicle_state: VehicleState) -> bool:
        """
        Returns whether or not a vehicle can transition to a new state from its current state

        :param vehicle_state: the new state to transition to
        :return: Boolean
        """
        if not VehicleState.is_valid(vehicle_state):
            raise TypeError("Invalid vehicle state type.")
        elif self.vehicle_state == vehicle_state:
            return False
        elif self.vehicle_state == VehicleState.OUT_OF_SERVICE:
            return False
        elif self.has_passengers():
            return False
        else:
            return True

    def modify_state(self, vehicle_state: VehicleState) -> Vehicle:
        return self._replace(vehicle_state=vehicle_state)

    def modify_link(self, link: Link) -> Vehicle:
        return self._replace(link=link)

    def apply_route_traversal(self,
                              traverse_result: RouteTraversal,
                              road_network: RoadNetwork,
                              env: Environment) -> Vehicle:

        powertrain = env.powertrains.get(self.powertrain_id)
        experienced_route = traverse_result.experienced_route
        energy_used = powertrain.energy_cost(experienced_route)
        step_distance_km = traverse_result.traversal_distance_km
        remaining_route = traverse_result.remaining_route

        # todo: we allow the agent to traverse only bounded by time, not energy;
        #   so, it is possible for the vehicle to travel farther in a time step than
        #   they have fuel to travel. this can create an error on the location of
        #   any agents at the time step where they run out of fuel. feels like an
        #   acceptable edge case but we could improve. rjf 20200309

        updated_energy_source = self.energy_source.use_energy(energy_used)
        less_energy_vehicle = self.modify_energy_source(
            energy_source=updated_energy_source)  # .assign_route(remaining_route)

        if not remaining_route:
            geoid = experienced_route[-1].end
            return less_energy_vehicle._replace(
                link=road_network.link_from_geoid(geoid),
                distance_traveled_km=self.distance_traveled_km + step_distance_km,
            )
        else:
            return less_energy_vehicle._replace(
                link=remaining_route[0],
                distance_traveled_km=self.distance_traveled_km + step_distance_km,
            )

    def transition(self, vehicle_state: VehicleState) -> Optional[Vehicle]:
        """
        Transitions the vehicle to a new state if possible.

        :param vehicle_state: the new state to transition to
        :return: the updated vehicle or None if not possible
        """
        previous_vehicle_state = self.vehicle_state
        if previous_vehicle_state == vehicle_state:
            return self
        elif self.can_transition(vehicle_state):
            transitioned_vehicle = self._replace(vehicle_state=vehicle_state)

            if vehicle_state == VehicleState.OUT_OF_SERVICE:
                # todo: do something with passengers? for now, they just get stranded
                return transitioned_vehicle._replace(
                    route=()
                )
            elif previous_vehicle_state == VehicleState.IDLE:
                # end of idling
                return transitioned_vehicle._reset_idle_stats()
            elif VehicleStateCategory.from_vehicle_state(previous_vehicle_state) == VehicleStateCategory.CHARGE and \
                    VehicleStateCategory.from_vehicle_state(vehicle_state) != VehicleStateCategory.CHARGE:
                # interrupted charge session
                return transitioned_vehicle._reset_charge_intent()
            elif previous_vehicle_state == VehicleState.DISPATCH_STATION and \
                    VehicleStateCategory.from_vehicle_state(vehicle_state) != VehicleStateCategory.CHARGE:
                # interrupted charge dispatch
                return transitioned_vehicle._reset_charge_intent()
            else:
                return transitioned_vehicle
        else:
            return None

    def add_passengers(self, new_passengers: Tuple[Passenger, ...]) -> Vehicle:
        """
        Loads some passengers onto this vehicle

        :param new_passengers: the set of passengers we want to add
        :return: the updated vehicle
        """
        with self.passengers.mutate() as p:
            for new in new_passengers:
                new_with_veh_id = new.add_vehicle_id(self.id)
                p.set(new_with_veh_id.id, new_with_veh_id)
            return self._replace(passengers=p.finish())

    def drop_off_passenger(self, passenger_id: PassengerId) -> Vehicle:
        """
        Drops off passengers to their destination.

        :param passenger_id:
        :return: the updated vehicle
        """
        if passenger_id not in self.passengers:
            return self
        updated_passengers = DictOps.remove_from_dict(self.passengers, passenger_id)
        return self._replace(passengers=updated_passengers)

    def has_passengers(self) -> bool:
        """
        Returns whether or not the vehicle has passengers.

        :return: Boolean
        """
        return len(self.passengers) > 0

    def has_route(self) -> bool:
        """
        Returns whether or not the vehicle has a route.

        :return: Boolean
        """
        return len(self.route) != 0

    def modify_energy_source(self, energy_source: EnergySource) -> Vehicle:
        """
        Replaces the vehicle energy source with a new energy source.

        :param energy_source: the new energy source
        :return: the updated vehicle
        """
        return self._replace(energy_source=energy_source)

    def assign_route(self, route: Route) -> Vehicle:
        """
        Assigns a route to the vehicle

        :param route: the route to be assigned
        :return: the updated vehicle
        """
        return self._replace(route=route)

    def set_charge_intent(self, charger: Charger) -> Vehicle:
        """
        Sets the intention for a vehicle to charge.

        :param station_id: which station the vehicle intends to charge at
        :param charger: the type of charger the vehicle intends to use
        :return: the updated vehicle
        """
        return self._replace(charger_intent=charger)

    def _reset_idle_stats(self) -> Vehicle:
        return self._replace(idle_time_seconds=0)

    def _reset_charge_intent(self) -> Vehicle:
        return self._replace(charger_intent=None)

    def send_payment(self, amount: Currency) -> Vehicle:
        """
        updates the Vehicle's balance based on sending a payment
        :param amount: the amount to pay
        :return: the updated Vehicle
        """
        return self._replace(balance=self.balance - amount)

    def receive_payment(self, amount: Currency) -> Vehicle:
        """
        updates the Vehicle's balance based on receiving a payment
        :param amount: the amount to be paid
        :return: the updated Vehicle
        """
        return self._replace(balance=self.balance + amount)
