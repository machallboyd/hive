"""
Dispatcher Object for high-level decision making in HIVE. Includes functions for
vehicle dispatching, and station/base selection.
"""

import datetime
import numpy as np
import osmnx as ox
import networkx as nx
import sys
import os
import utm

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
LIB_PATH = os.path.join(THIS_DIR, '.lib')
sys.path.append(LIB_PATH)
from c_haversine import vector_haversine

from hive import tripenergy as nrg
from hive import charging as chrg
from hive import helpers as hlp
from hive import units

class Dispatcher:
    """
    The Dispatcher object is responsible for coordinating the actions of the fleet.

    Parameters
    ----------
    fleet: list
        list of all vehicles in the fleet.
    fleet_state: np.ndarray
        matrix that represents the state of the fleet. Used for quick numpy vectorized operations.
    stations: list
        list of all charging stations.
    bases: list
        list of all bases
    env_params: dict
        dictionary of all of the constant environment parameters shared across the simulation.
    clock: hive.utils.Clock
        simulation clock shared across the simulation to track simulation time steps.
    """


    def __init__(
                self,
                fleet,
                fleet_state,
                stations,
                bases,
                network,
                env_params,
                clock,
                ):

        self.ID = 0

        self._fleet = fleet
        self._fleet_state = fleet_state
        for veh in self._fleet:
            veh.fleet_state = fleet_state

        self._clock = clock

        self._stations = stations
        self._bases = bases

        self._network = network
        nodes, edges = ox.graph_to_gdfs(network, nodes=True, edges=True)
        self._nodes = nodes
        self._edges = edges

        self.history = []
        self._dropped_requests = 0

        self._ENV = env_params


    def _log(self):
        """
        Function stores the partial state of the object at each time step.
        """

        active_col = self._ENV['FLEET_STATE_IDX']['active']
        active_vehicles = self._fleet_state[:, active_col].sum()

        self.history.append({
                        'sim_time': self._clock.now,
                        'active_vehicles': active_vehicles,
                        'dropped_requests': self._dropped_requests,
                        })

    def _generate_route_crow(self, olat, olon, dlat, dlon, activity="NULL"):
        x0, y0, zone_number, zone_letter = utm.from_latlon(olat, olon)
        x1, y1, _, _ = utm.from_latlon(dlat, dlon)
        trip_dist_mi = hlp.estimate_vmt_2D(x0, y0, x1, y1, self._ENV['RN_SCALING_FACTOR'])
        trip_time_s = (trip_dist_mi / self._ENV['DISPATCH_MPH']) * units.HOURS_TO_SECONDS

        steps = round(trip_time_s/self._clock.TIMESTEP_S)

        if steps <= 1:
            return [((olat, olon), trip_dist_mi, activity), ((dlat, dlon), trip_dist_mi, activity)]
        step_distance_mi = trip_dist_mi/steps
        route_range = np.arange(0, steps + 1)
        route = []
        for i, time in enumerate(route_range):
            t = i/steps
            xt = (1-t)*x0 + t*x1
            yt = (1-t)*y0 + t*y1
            point = utm.to_latlon(xt, yt, zone_number, zone_letter)
            route.append((point, step_distance_mi, activity))
        return route

    def _generate_route(self, olat, olon, dlat, dlon, activity):
        origin = ox.get_nearest_node(self._network, (olat, olon))
        dest = ox.get_nearest_node(self._network, (dlat, dlon))
        try:
            raw_route = nx.shortest_path(self._network, origin, dest)
        except nx.exception.NetworkXNoPath:
            return self._generate_route_crow(olat, olon, dlat, dlon, activity)

        dists = []
        durations = []
        points = []
        for i in range(len(raw_route)-1):
            u = raw_route[i]
            v = raw_route[i+1]
            node = self._nodes.loc[v]
            points.append((node.x, node.y))
            edge = self._edges[(self._edges.u == u) & (self._edges.v == v)]
            speed = int(edge['maxspeed'].values[0])
            dist_mi = edge['length'].values[0] * units.METERS_TO_MILES
            duration_s = (dist_mi / speed) * units.HOURS_TO_SECONDS
            dists.append(dist_mi)
            durations.append(duration_s)

        if len(points) < 1:
            #No movement required
            return None

        route_time = np.cumsum(durations)
        route_dist = np.cumsum(dists)
        bins = np.arange(0,max(route_time), self._clock.TIMESTEP_S)
        route_index = np.digitize(route_time, bins)
        route = [(points[0], dists[0], activity)]
        prev_index = 0
        for i in range(1, len(bins)+1):
            try:
                index = np.max(np.where(np.digitize(route_time, bins) == i))
            except ValueError:
                index = prev_index
            loc = points[index]
            dist = route_dist[index] - route[i-1][1]
            route.append((loc, dist, activity))
            prev_index = index

        return route


    def _find_closest_plug(self, vehicle, type='station'):
        """
        Function takes hive.vehicle.Vehicle object and returns the FuelStation
        nearest Vehicle with at least one available plug. The "type" argument
        accepts either 'station' or 'base', informing the search space.

        Parameters
        ----------
        vehicle: hive.vehicle.Vehicle
            vehicle to which the closest plug is relative to.
        type: str
            string to indicate which type of plug is being searched for.

        Returns
        -------
        nearest: hive.stations.FuelStation
            the station or bases that is the closest to the vehicle
        """
        #IDEA: Store stations in a geospatial index to eliminate exhaustive search. -NR
        INF = 1000000000

        if type == 'station':
            network = self._stations
        elif type == 'base':
            network = self._bases

        def recursive_search(search_space):
            if len(search_space) < 1:
                raise NotImplementedError("""No plugs are available on the
                    entire network.""")

            dist_to_nearest = INF
            for station in search_space:
                dist_mi = hlp.estimate_vmt_latlon(vehicle.x,
                                               vehicle.y,
                                               station.X,
                                               station.Y,
                                               scaling_factor = vehicle.ENV['RN_SCALING_FACTOR'])
                if dist_mi < dist_to_nearest:
                    dist_to_nearest = dist_mi
                    nearest = station
            if nearest.avail_plugs < 1:
                search_space = [s for s in search_space if s.ID != nearest.ID]
                nearest = recursive_search(search_space)

            return nearest

        nearest = recursive_search(network)

        return nearest

    def _get_n_best_vehicles(self, request, n):
        """
        Function takes a single request and returns the n best vehicles with respect
        to that request.

        Parameters
        ----------
        request: NamedTuple
            request named tuple to match n vehicles to.
        n: int
            how many vehicles to return.

        Returns
        -------
        best_vehs_ids: np.ndarray
            array of the best n vehicle ids in sorted order from best -> worst.

        """
        fleet_state = self._fleet_state
        point = np.array([request.pickup_lat, request.pickup_lon])
        # dist = np.linalg.norm(fleet_state[:, :2] - point, axis=1) * METERS_TO_MILES
        x_col = self._ENV['FLEET_STATE_IDX']['x']
        y_col = self._ENV['FLEET_STATE_IDX']['y']
        dist = vector_haversine(
                            fleet_state[:,x_col].astype('double'),
                            fleet_state[:,y_col].astype('double'),
                            point.astype('double'),
                            np.int64(len(self._fleet)),
                            )
        dist = np.asarray(dist) * units.KILOMETERS_TO_MILES

        best_vehs_idx = np.argsort(dist)
        dist_mask = dist < self._ENV['MAX_DISPATCH_MILES']

        available_col = self._ENV['FLEET_STATE_IDX']['available']
        available_mask = (fleet_state[:,available_col] == 1)

        soc_col = self._ENV['FLEET_STATE_IDX']['soc']
        KWH__MI_col = self._ENV['FLEET_STATE_IDX']['KWH__MI']
        BATTERY_CAPACITY_KWH_col = self._ENV['FLEET_STATE_IDX']['BATTERY_CAPACITY_KWH']
        min_soc_mask = (fleet_state[:, soc_col] \
            - ((dist + request.distance_miles) * fleet_state[:, KWH__MI_col]) \
            /fleet_state[:, BATTERY_CAPACITY_KWH_col] > self._ENV['MIN_ALLOWED_SOC'])

        avail_seats_col = self._ENV['FLEET_STATE_IDX']['avail_seats']
        avail_seats_mask = fleet_state[:, avail_seats_col] >= request.passengers

        mask = dist_mask & available_mask & min_soc_mask & avail_seats_mask
        best_vehs_ids = best_vehs_idx[mask[best_vehs_idx]][:n]
        return best_vehs_ids

    def _dispatch_vehicles(self, requests):
        """
        Function coordinates vehicle dispatch actions for a single timestep given
        one or more requests.

        Parameters
        ----------
        requests: list
            list of requests that occur in a single time step.
        """
        self._dropped_requests = 0
        for request in requests.itertuples():
            best_vehicle = self._get_n_best_vehicles(request, n=1)
            if len(best_vehicle) < 1:
                self._dropped_requests += 1
            else:
                vehid = best_vehicle[0]
                veh = self._fleet[vehid]
                disp_route = self._generate_route(
                                        veh.x,
                                        veh.y,
                                        request.pickup_lat,
                                        request.pickup_lon,
                                        activity = "Dispatch to Request")
                trip_route = self._generate_route(
                                        request.pickup_lat,
                                        request.pickup_lon,
                                        request.dropoff_lat,
                                        request.dropoff_lon,
                                        activity = "Serving Trip")

                del disp_route[-1]
                route = disp_route + trip_route

                veh.cmd_make_trip(
                        route = route,
                        passengers = request.passengers,
                        )

    def _charge_vehicles(self):
        """
        Function commands vehicles to charge if their SOC dips beneath the
        LOWER_SOC_THRESH_STATION environment variable.
        """
        soc_col = self._ENV['FLEET_STATE_IDX']['soc']
        available_col = self._ENV['FLEET_STATE_IDX']['available']
        active_col = self._ENV['FLEET_STATE_IDX']['active']
        mask = (self._fleet_state[:, soc_col] < self._ENV['LOWER_SOC_THRESH_STATION']) \
            & (self._fleet_state[:,available_col] == 1) & (self._fleet_state[:, active_col] == 1)
        veh_ids = np.argwhere(mask)

        for veh_id in veh_ids:
            vehicle = self._fleet[veh_id[0]]
            station = self._find_closest_plug(vehicle)
            route = self._generate_route(vehicle.x, vehicle.y, station.X, station.Y, 'Moving to Station')
            vehicle.cmd_charge(station, route)

    def _check_idle_vehicles(self):
        """
        Function checks for any vehicles that have been idling for longer than
        MAX_ALLOWABLE_IDLE_MINUTES and commands those vehicles to return to their
        respective base.
        """
        idle_min_col = self._ENV['FLEET_STATE_IDX']['idle_min']
        idle_mask = self._fleet_state[:, idle_min_col] >= self._ENV['MAX_ALLOWABLE_IDLE_MINUTES']
        veh_ids = np.argwhere(idle_mask)

        for veh_id in veh_ids:
            vehicle = self._fleet[veh_id[0]]
            base = self._find_closest_plug(vehicle, type='base')
            route = self._generate_route(vehicle.x, vehicle.y, base.X, base.Y, 'Moving to Base')
            vehicle.cmd_return_to_base(base, route)

    def process_requests(self, requests):
        """
        process_requests is called for each simulation time step. Function takes
        a list of requests and coordinates vehicle actions for that step.

        Parameters
        ----------
        requests: list
            one or many requests to distribute to the fleet.
        """
        self._charge_vehicles()
        self._dispatch_vehicles(requests)
        self._check_idle_vehicles()
        self._log()
