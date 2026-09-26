from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joint_excavation_context.store import EventStore


def load_schema() -> dict[str, Any]:
    return json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))


def make_store() -> EventStore:
    return EventStore(load_schema())


def make_event(
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int = 1,
    summary: str = "联调事件",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "summary": summary,
        **extra,
    }
