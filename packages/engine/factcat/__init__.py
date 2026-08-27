"""Factcat - product analytics on the event model already in your warehouse.

A **fact** table is what you build; **cat** is how you read a file. Factcat
reads your fact tables where they sit, and never copies them anywhere.

    from factcat import RetentionSpec, retention_sql

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
from .funnel import build_sql as funnel_sql
from .retention import build_sql as retention_sql
from .spec import FunnelSpec, RetentionSpec

__all__ = [
    "FunnelSpec",
    "RetentionSpec",
    "SUPPORTED",
    "funnel_sql",
    "period_grid",
    "retention_sql",
]
