"""Factcat — open-source, warehouse-first product analytics.

    from factcat import EventsSpec, events_sql, RetentionSpec, retention_sql

    spec = RetentionSpec(
        table="payments",
        entity="subscription_id",   # not the user
        entity_time="sub_start",
        event_time="paid_at",
        period_days=35,             # not a calendar bucket
        n_periods=2,
        retained="status = 'collected' AND within_period_offset <= 5",
    )
    print(retention_sql(spec, dialect="snowflake"))
"""

from __future__ import annotations

from .dialects import SUPPORTED, period_grid
from .events import build_sql as events_sql
from .funnel import build_sql as funnel_sql
from .retention import build_sql as retention_sql
from .spec import (
    EVENT_MEASURES,
    PROPERTY_MEASURES,
    EventsSpec,
    FunnelSpec,
    RetentionSpec,
)

__all__ = [
    "EVENT_MEASURES",
    "EventsSpec",
    "FunnelSpec",
    "PROPERTY_MEASURES",
    "RetentionSpec",
    "SUPPORTED",
    "events_sql",
    "funnel_sql",
    "period_grid",
    "retention_sql",
]
