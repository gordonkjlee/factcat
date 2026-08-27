"""Analysis definitions.

Every field that other product analytics tools hard-code is a parameter here.
That is the whole point of the library, so the dataclasses are the design
document as much as the code is.
"""

from __future__ import annotations

from dataclasses import dataclass


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
