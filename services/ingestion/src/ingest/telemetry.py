"""Metrics with their own dimensions (error_code, status, Trigger), written at once as one EMF
record. The handlers' Metrics objects share one dimension set per flush, so a metric that needs
a per-value dimension goes through here. The service dimension and namespace come from the
Powertools environment, like every other metric."""

from __future__ import annotations

from aws_lambda_powertools.metrics import MetricUnit, single_metric


def emit(name: str, value: float, unit: MetricUnit = MetricUnit.Count, **dimensions: str) -> None:
    with single_metric(name=name, unit=unit, value=value) as metric:
        for key, dim in dimensions.items():
            metric.add_dimension(name=key, value=dim)
