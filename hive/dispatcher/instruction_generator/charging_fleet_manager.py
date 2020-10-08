from __future__ import annotations

import logging
from typing import Tuple, NamedTuple, TYPE_CHECKING

from hive.reporting import instruction_generator_event_ops
from hive.state.vehicle_state.charging_station import ChargingStation
from hive.state.vehicle_state.idle import Idle
from hive.state.vehicle_state.repositioning import Repositioning

if TYPE_CHECKING:
    from hive.model.vehicle.vehicle import Vehicle
    from hive.state.simulation_state.simulation_state import SimulationState
    from hive.runner.environment import Environment
    from hive.dispatcher.instruction.instruction import Instruction
    from hive.config.dispatcher_config import DispatcherConfig

from hive.dispatcher.instruction_generator.instruction_generator import InstructionGenerator
from hive.dispatcher.instruction_generator.instruction_generator_ops import instruct_vehicles_to_dispatch_to_station, \
    instruct_vehicles_to_sit_idle

log = logging.getLogger(__name__)


class ChargingFleetManager(NamedTuple, InstructionGenerator):
    """
    A manager that instructs vehicles to charge if they fall below an SOC threshold.
    """
    config: DispatcherConfig

    def generate_instructions(
            self,
            simulation_state: SimulationState,
            environment: Environment,
    ) -> Tuple[ChargingFleetManager, Tuple[Instruction, ...]]:
        """
        Generate fleet targets for the dispatcher to execute based on the simulation state.

        :param simulation_state: The current simulation state
        :param environment: The simulation environment

        :return: the updated ChargingFleetManager along with instructions
        """

        # find vehicles that fall below the minimum threshold and charge them.

        def charge_candidate(v: Vehicle) -> bool:
            proper_state = isinstance(v.vehicle_state, Idle) or isinstance(v.vehicle_state, Repositioning)
            if not proper_state:
                return False

            mechatronics = environment.mechatronics.get(v.mechatronics_id)
            range_remaining_km = mechatronics.range_remaining_km(v)
            is_charge_candidate = range_remaining_km <= environment.config.dispatcher.charging_range_km_threshold
            return is_charge_candidate

        def stop_charge_candidate(v: Vehicle) -> bool:
            proper_state = isinstance(v.vehicle_state, ChargingStation)
            if not proper_state:
                return False

            mechatronics = environment.mechatronics.get(v.mechatronics_id)
            battery_soc = mechatronics.fuel_source_soc(v)
            is_stop_charge_candidate = battery_soc >= self.config.ideal_fastcharge_soc_limit
            return is_stop_charge_candidate

        low_soc_vehicles = simulation_state.get_vehicles(filter_function=charge_candidate)
        high_soc_vehicles = simulation_state.get_vehicles(filter_function=stop_charge_candidate)

        # for each low_soc_vehicle that will conduct a refuel search, report the search event
        for v in low_soc_vehicles:
            report = instruction_generator_event_ops.refuel_search_event(v, simulation_state, environment)
            environment.reporter.file_report(report)

        charge_instructions = instruct_vehicles_to_dispatch_to_station(
            n=len(low_soc_vehicles),
            max_search_radius_km=self.config.max_search_radius_km,
            vehicles=low_soc_vehicles,
            simulation_state=simulation_state,
            environment=environment,
            target_soc=environment.config.dispatcher.ideal_fastcharge_soc_limit,
            charging_search_type=environment.config.dispatcher.charging_search_type
        )
        stop_charge_instructions = instruct_vehicles_to_sit_idle(len(high_soc_vehicles), high_soc_vehicles)

        instructions = charge_instructions + stop_charge_instructions

        return self, instructions
