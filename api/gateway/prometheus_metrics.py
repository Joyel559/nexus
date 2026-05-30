"""Prometheus metrics registry for gateway runtime."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class GatewayPrometheus:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests_total = Counter(
            "gateway_requests_total",
            "Total gateway requests by provider and outcome",
            ["provider_id", "outcome"],
            registry=self.registry,
        )
        self.request_latency_ms = Histogram(
            "gateway_request_latency_ms",
            "Gateway request latency in milliseconds",
            ["provider_id"],
            buckets=(25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
            registry=self.registry,
        )
        self.retries_total = Counter(
            "gateway_retries_total",
            "Gateway retry attempts",
            ["provider_id"],
            registry=self.registry,
        )
        self.fallback_total = Counter(
            "gateway_fallback_total",
            "Gateway fallback switches",
            ["from_provider", "to_provider"],
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "gateway_queue_depth",
            "Current gateway queue depth",
            registry=self.registry,
        )
        self.queue_inflight = Gauge(
            "gateway_queue_inflight",
            "Current inflight request count",
            registry=self.registry,
        )
        self.queue_rejected_total = Counter(
            "gateway_queue_rejected_total",
            "Rejected gateway queue admissions",
            registry=self.registry,
        )
        self.estimated_cost_usd = Counter(
            "gateway_estimated_cost_usd_total",
            "Estimated total request cost in USD",
            ["provider_id"],
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)

    @staticmethod
    def content_type() -> str:
        return CONTENT_TYPE_LATEST
