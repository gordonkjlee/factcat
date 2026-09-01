"""Analysis definitions.

Every field that other product analytics tools hard-code is a parameter here.
That is the whole point of the library, so the dataclasses are the design
document as much as the code is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

On = Literal["events", "property"]
Measure = Literal["total", "uniques", "average", "sum", "median", "distinct"]
BreakdownAt = Literal["rows", "first", "last", "carried"]
BREAKDOWN_AT: tuple[BreakdownAt, ...] = ("rows", "first", "last", "carried")
OTHER_LABEL = "(other)"

# UI labels live in the README. Two families so "Average" is not two APIs.
EVENT_MEASURES: tuple[Measure, ...] = ("total", "uniques", "average")
PROPERTY_MEASURES: tuple[Measure, ...] = ("sum", "average", "median", "distinct")
ONS: tuple[On, ...] = ("events", "property")


@dataclass(frozen=True)
class Breakdown:
    """One breakdown column: a caller SQL expression plus its value semantics.

    The expression is one general form; this config is the other. A scalar
    expression's scope is the ``where``-filtered relation, so no expression
    written into the slot can reach unfiltered history — the config names the
    relation scope and bounds an expression cannot (ADR-12, amended).

    Attributes:
        expr:      caller SQL expression over source columns. Interpolated,
                   never rewritten.
        at:        value semantics for this column.
                   ``rows``    — the expression on the metric row (filtered table).
                   ``first``   — first non-null by ``event_time`` per entity,
                                 unfiltered table.
                   ``last``    — latest non-null by ``event_time`` per entity,
                                 unfiltered table.
                   ``carried`` — for each metric row, the last non-null value of
                                 the expression at or before that row's
                                 ``event_time``, over the entity's unfiltered
                                 history (a stamp before the chart window still
                                 resolves). A row never borrows a future stamp.
        fill_from: caller SQL boolean narrowing which unfiltered rows may stamp
                   a value (e.g. ``"event_name = 'subscription_started'"``).
                   Any mode except ``rows``.
        since:     caller SQL timestamp expression; stamps at or after it count
                   (``event_time >= since``). ``first`` / ``last`` only.
        until:     caller SQL timestamp expression; stamps at or before it count
                   (``event_time <= until``). ``first`` / ``last`` only.
                   "State as of the window start" is ``at="last",
                   until=<window-start SQL>``.
        backfill:  only with ``at="last"`` and ``until``: entities with no value
                   by the bound fall back to their first recorded value ever. It
                   never overrides a real as-of value.

    Same-instant rules, shared by every non-``rows`` mode: a stamp at exactly
    the row's (or bound's) instant is seen; duplicate stamps at one instant
    resolve to the greatest expression value.
    """

    expr: str
    at: BreakdownAt = "rows"
    fill_from: str | None = None
    since: str | None = None
    until: str | None = None
    backfill: bool = False

    def __post_init__(self) -> None:
        if not self.expr or not self.expr.strip():
            raise ValueError("Breakdown.expr must be a SQL expression")
        if self.at not in BREAKDOWN_AT:
            raise ValueError(
                "Breakdown.at must be 'rows', 'first', 'last', or 'carried'"
            )
        for name in ("fill_from", "since", "until"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"Breakdown.{name} must be SQL if set")
        if self.at == "rows":
            if self.fill_from is not None:
                raise ValueError("fill_from does not apply to at='rows'")
        if self.at not in ("first", "last"):
            if self.since is not None or self.until is not None:
                raise ValueError("since/until apply only to at='first' or 'last'")
        if self.backfill and not (self.at == "last" and self.until is not None):
            raise ValueError("backfill requires at='last' with until set")


@dataclass(frozen=True)
class RetentionSpec:
    """A retention definition.

    Attributes:
        table:         source relation. Typically a dbt mart, not a raw event stream.
        entity:        the grain retention is measured at. **Not** assumed to be the
                       user: a subscription, a household, a device, a seat or an
                       account are all valid, and they give different answers on the
                       same data.
        entity_time:   expression giving the entity's cohort entry instant.
        event_time:    expression giving the instant of an observation.
        period_days:   period length in days. Arbitrary. 35 is a 30-day billing cycle
                       plus a 5-day dunning window, and no calendar bucket expresses it.
        n_periods:     how many periods to project.
        retained:      arbitrary SQL boolean deciding whether a row counts as retention
                       for its period. May reference any source column plus the derived
                       columns below.
        cohort_bucket: expression bucketing entities into cohorts.
        where:         optional filter applied to the source relation.

    Derived columns available to ``retained``:
        ``offset_days``
            whole days from the entity's cohort entry.
        ``period_index``
            which period the observation falls in, counting from zero.
        ``within_period_offset``
            whole days since this period opened. A 5-day dunning window is
            ``within_period_offset <= 5``.
    """

    table: str
    entity: str
    entity_time: str
    event_time: str
    period_days: int
    n_periods: int
    retained: str
    cohort_bucket: str = "date_trunc('month', cohort_ts)"
    where: str | None = None

    def __post_init__(self) -> None:
        if self.period_days < 1:
            raise ValueError("period_days must be >= 1")
        if self.n_periods < 1:
            raise ValueError("n_periods must be >= 1")
        if not self.retained.strip():
            raise ValueError("retained must be a SQL boolean expression")


@dataclass(frozen=True)
class FunnelSpec:
    """An ordered funnel definition.

    Steps are matched in order and each step must occur at or after the previous
    one, taking the **earliest** qualifying event at every stage. As with
    retention, the entity is yours to choose - a funnel over subscriptions and a
    funnel over users are different questions about the same table.

    Attributes:
        table:         source relation.
        entity:        the grain the funnel is counted at.
        event_time:    expression giving the instant of an observation.
        steps:         ordered SQL boolean expressions, one per step. At least two.
        step_labels:   optional display labels, defaulting to ``step_0``, ``step_1``...
        within_days:   optional completion window, measured from the first step. None
                       means unbounded.
        where:         optional filter applied to the source relation.
    """

    table: str
    entity: str
    event_time: str
    steps: tuple[str, ...]
    step_labels: tuple[str, ...] | None = None
    within_days: int | None = None
    where: str | None = None

    def __post_init__(self) -> None:
        if len(self.steps) < 2:
            raise ValueError("a funnel needs at least two steps")
        if self.step_labels is not None and len(self.step_labels) != len(self.steps):
            raise ValueError("step_labels must match steps in length")
        if self.within_days is not None and self.within_days < 0:
            raise ValueError("within_days must be >= 0")

    def labels(self) -> tuple[str, ...]:
        if self.step_labels is not None:
            return self.step_labels
        return tuple(f"step_{i}" for i in range(len(self.steps)))


@dataclass(frozen=True)
class EventsSpec:
    """A time-series of event counts or of a property.

    Two families, because "Average" is two different questions:

    * ``on="events"`` — Total (``COUNT(*)``), Uniques (``COUNT DISTINCT``
      of ``entity``), Average (Total / Uniques: events per unique entity).
    * ``on="property"`` — Sum / Average / Median of ``of``, or Distinct:
      mean number of distinct ``of`` values per entity.

    Uniques is never a column called ``user_id``. Min/max of a property are
    not measures here.

    The x-axis is ``bucket``, a SQL expression. Day/week/month/hour (and
    cyclic weekday / hour-of-day) UI sugar fills ``date_trunc`` or an
    extract. There is no ``period: day|week|month`` field.

    Attributes:
        table:      source relation.
        entity:     grain for Uniques and for distinct-per-entity. Caller-supplied.
        event_time: observation instant; also used in the default day bucket.
        measure:    see ``EVENT_MEASURES`` / ``PROPERTY_MEASURES``.
        on:         ``events`` (default) or ``property``.
        of:         SQL expression. Required for ``on="property"``; forbidden
                    for ``on="events"``.
        bucket:     SQL time-axis expression. Default ``date_trunc('day', {event_time})``.
        where:      optional filter on the source relation.
        exact:      False (default) uses approx NDV / approx median / approx
                    top-N labels where the dialect has them. True is
                    COUNT DISTINCT / PERCENTILE_CONT / GROUP BY LIMIT.
                    One chart toggle sets this. Total / Sum / property Average
                    stay exact; the time axis is always exact GROUP BY bucket.
        breakdowns: caller SQL expressions to split the series — plain strings,
                    or ``Breakdown`` entries carrying per-column value semantics
                    (``at``, ``fill_from``, ``since``/``until`` bounds,
                    ``backfill``). Empty is today's one-line chart. Expressions
                    are interpolated, never rewritten.
        breakdown_at: default ``at`` for plain-string entries. ``rows`` (value
                    on the metric row), ``first`` / ``last`` (one non-null value
                    per entity from the unfiltered table, ordered by
                    ``event_time``), ``carried`` (last non-null at or before the
                    row's instant, unfiltered history). Ignored when
                    ``breakdowns`` is empty.
        breakdown_labels: public column names. Default ``breakdown_0``, …
        top_n:      fold the category axis to this many values plus ``(other)``.
                    Default 8. Ignored when ``breakdowns`` is empty.
        include_other: True (default) emits an ``(other)`` series for the tail.
                    False drops the tail. Ignored when ``breakdowns`` is empty.
    """

    table: str
    entity: str
    event_time: str
    measure: Measure
    on: On = "events"
    of: str | None = None
    bucket: str | None = None
    where: str | None = None
    exact: bool = False
    breakdowns: tuple[str | Breakdown, ...] = ()
    breakdown_at: BreakdownAt = "rows"
    breakdown_labels: tuple[str, ...] | None = None
    top_n: int = 8
    include_other: bool = True

    def __post_init__(self) -> None:
        if self.on not in ONS:
            raise ValueError("on must be 'events' or 'property'")
        allowed = EVENT_MEASURES if self.on == "events" else PROPERTY_MEASURES
        if self.measure not in allowed:
            raise ValueError(f"on={self.on!r} allows measures {allowed}")
        of_set = self.of is not None and bool(self.of.strip())
        if self.on == "property":
            if not of_set:
                raise ValueError("property measures require of= a SQL expression")
        elif of_set:
            raise ValueError("event measures do not take of=")
        if not self.entity.strip():
            raise ValueError("entity is required")
        if not self.event_time.strip():
            raise ValueError("event_time is required")
        if self.bucket is not None and not self.bucket.strip():
            raise ValueError("bucket must be a SQL expression if set")
        if self.breakdown_at not in BREAKDOWN_AT:
            raise ValueError(
                "breakdown_at must be 'rows', 'first', 'last', or 'carried'"
            )
        if self.breakdown_labels is not None and len(self.breakdown_labels) != len(
            self.breakdowns
        ):
            raise ValueError("breakdown_labels must match breakdowns in length")
        if self.top_n < 1:
            raise ValueError("top_n must be >= 1")
        for entry in self.breakdowns:
            if isinstance(entry, Breakdown):
                continue  # Breakdown validates itself
            if not entry or not str(entry).strip():
                raise ValueError("breakdowns must be SQL expressions")

    def bucket_sql(self) -> str:
        if self.bucket is not None:
            return self.bucket
        return f"date_trunc('day', {self.event_time})"

    def bd_labels(self) -> tuple[str, ...]:
        if self.breakdown_labels is not None:
            return self.breakdown_labels
        return tuple(f"breakdown_{i}" for i in range(len(self.breakdowns)))

    def resolved_breakdowns(self) -> tuple[Breakdown, ...]:
        """Every breakdown as a ``Breakdown``; plain strings inherit
        ``breakdown_at``."""
        return tuple(
            entry
            if isinstance(entry, Breakdown)
            else Breakdown(expr=entry, at=self.breakdown_at)
            for entry in self.breakdowns
        )
