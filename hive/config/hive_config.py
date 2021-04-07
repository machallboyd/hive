from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Dict, Union, Tuple

import pkg_resources
import yaml

from hive.config.config_builder import ConfigBuilder
from hive.config.dispatcher_config import DispatcherConfig
from hive.config.global_config import GlobalConfig
from hive.config.input import Input
from hive.config.network import Network
from hive.config.sim import Sim
from hive.util import fs

log = logging.getLogger(__name__)


class HiveConfig(NamedTuple):
    global_config: GlobalConfig
    input_config: Input
    sim: Sim
    network: Network
    dispatcher: DispatcherConfig

    scenario_output_directory: Path = Path("")

    @classmethod
    def build(cls, scenario_file_path: Path, config: Dict = None) -> Union[Exception, HiveConfig]:
        return ConfigBuilder.build(
            default_config={},
            required_config=(),
            config_constructor=lambda c: HiveConfig.from_dict(c, scenario_file_path),
            config=config
        )

    @classmethod
    def from_dict(cls, d: Dict, scenario_file_path: Path) -> Union[Exception, HiveConfig]:
        # collect the global hive configuration
        global_config = fs.global_hive_config_search()

        # i wish to make these comprehensive print-outs DEBUG-only, but, i am totally perplexed by Python Logging's
        # behavior for setting log level
        # https://stackoverflow.com/questions/43109355/logging-setlevel-is-being-ignored
        # https://stackoverflow.com/questions/44312978/python-logger-is-not-printing-debug-messages-although-it-is-set-correctly

        # logging.basicConfig(level=global_config.log_level)
        root_logger = logging.getLogger("")
        root_logger.setLevel(global_config.log_level)

        log.info(f"global hive configuration loaded from {global_config.global_settings_file_path}")
        for k, v in global_config.asdict().items():
            log.info(f"  {k}: {v}")

        # start build using the Hive config defaults file
        defaults_file_str = pkg_resources.resource_filename("hive.resources.defaults", "hive_config.yaml")
        defaults_file = Path(defaults_file_str)

        with defaults_file.open('r') as f:
            conf = yaml.safe_load(f)

            # append input_config file to default configuration with overwrite
            conf['input'].update(d['input'])
            conf['sim'].update(d['sim'])
            conf['network'].update(d['network'])
            conf['dispatcher'].update(d['dispatcher'])

            # is this still possible now with the default config loading from hive.resources.defaults?
            warn_missing_config_keys = ['input', 'sim', 'network']
            for key in warn_missing_config_keys:
                if key not in conf:
                    log.warning(f"scenario file is missing a '{key}' section may cause errors")

            sconfig = Sim.build(conf.get('sim'))
            iconfig = Input.build(conf.get('input'), scenario_file_path, conf.get('cache'))
            nconfig = Network.build(conf.get('network'))
            dconfig = DispatcherConfig.build(conf.get('dispatcher'))

            scenario_name = sconfig.sim_name + "_" + datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            scenario_output_directory = Path(global_config.output_base_directory) / Path(scenario_name)

            hive_config = HiveConfig(
                global_config=global_config,
                input_config=iconfig,
                sim=sconfig,
                network=nconfig,
                dispatcher=dconfig,
                scenario_output_directory=scenario_output_directory,
            )

            log.info(f"output directory set to {hive_config.input_config.scenario_directory}")
            log.info(f"hive config loaded from {str(scenario_file_path)}")
            log.info(f"\n{yaml.dump(conf)}")

            return hive_config

    def asdict(self) -> Dict:
        out_dict = {}
        cache = {}
        input_configuration = self.input_config.asdict()

        for name, value in input_configuration.items():
            if not value:
                continue
            else:
                path = Path(value)
                if not path.is_file():
                    continue
                else:
                    with path.open(mode='rb') as f:
                        data = f.read()
                        md5_sum = hashlib.md5(data).hexdigest()
                        cache[name] = md5_sum
        out_dict['cache'] = cache

        for name, config in self._asdict().items():
            if issubclass(config.__class__, Tuple):
                out_dict[name] = config.asdict()
            else:
                out_dict[name] = config

        return out_dict

    def set_scenario_output_directory(self, output_directory: Path) -> HiveConfig:
        return self._replace(scenario_output_directory=output_directory)

