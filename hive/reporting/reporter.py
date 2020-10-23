from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, NamedTuple, Optional

from hive.reporting.report_type import ReportType
from hive.reporting.handler.stats_handler import StatsHandler

if TYPE_CHECKING:
    from hive.runner.runner_payload import RunnerPayload
    from hive.reporting.handler import Handler
    from hive.config.global_config import GlobalConfig


class Report(NamedTuple):
    report_type: ReportType
    report: Dict[str, str]

    def as_json(self) -> Dict[str, str]:
        out = {str(k): str(v) for k, v in self.report.items()}
        out['report_type'] = self.report_type.name.lower()
        return out


class Reporter:
    """
    A class that generates reports for the simulation.
    """

    def __init__(self, config: GlobalConfig):
        self.config = config
        self.reports: List[Report] = []
        self.handlers: List[Handler] = []

    def add_handler(self, handler: Handler):
        self.handlers.append(handler)

    def flush(self, runner_payload: RunnerPayload):
        """
        called at each sim step.

        :param runner_payload: The runner payload.
        :return: Does not return a value.
        """

        # TODO: This is too fragile. We should think about introducing a sim step parameter.
        if runner_payload.s.sim_time % self.config.log_period_seconds != 0:
            return

        for handler in self.handlers:
            handler.handle(self.reports, runner_payload)

        self.reports = []

    def file_report(self, report: Report):
        """
        files a single report to be handled later.

        :param report:
        :return:
        """
        self.reports.append(report)

    def get_summary_stats(self) -> Optional[Dict]:
        """
        if a summary StatsHandler exists, return the final report from the collection of statistics
        :return: the stats Dictionary, or, None
        """
        final_report = None
        for handler in self.handlers:
            if isinstance(handler, StatsHandler):
                final_report = handler.get_stats()
        return final_report

    def close(self, runner_payload: RunnerPayload):
        """
        wrap up anything here. called at the end of the simulation

        :return:
        """
        for handler in self.handlers:
            handler.close(runner_payload)
