"""deploy/prometheus/alerts.yml only references argus_* metrics that the API actually exposes."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from argus.core.metrics import REGISTRY

RULES = Path(__file__).resolve().parents[2] / "deploy" / "prometheus" / "alerts.yml"
_SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")


def _exposed() -> set[str]:
    names: set[str] = set()
    for metric in REGISTRY.collect():
        names.add(metric.name)
        names.update(s.name for s in metric.samples)
        if metric.type == "counter":
            names.add(metric.name + "_total")
        if metric.type == "histogram":
            names.update(metric.name + s for s in ("_bucket", "_count", "_sum"))
    return names


def test_alert_rules_parse_and_reference_exposed_metrics():
    doc = yaml.safe_load(RULES.read_text())
    rules = [r for g in doc["groups"] for r in g["rules"]]
    assert len({r["alert"] for r in rules}) == len(rules), "duplicate alert names"
    exposed = _exposed()
    missing = set()
    for rule in rules:
        assert rule["labels"]["severity"] in {"page", "warn", "info"}
        assert rule["annotations"]["summary"]
        for name in re.findall(r"\b(argus_[a-z0-9_]+)", rule["expr"]):
            if name not in exposed and not any(
                name.endswith(s) and name[: -len(s)] in exposed for s in _SUFFIXES
            ):
                missing.add(name)
    assert not missing, f"alert rules reference unknown metrics: {sorted(missing)}"
    exprs = " ".join(r["expr"] for r in rules)
    # Process metrics come from the ProcessCollector on the custom registry.
    assert "process_resident_memory_bytes" in exprs
