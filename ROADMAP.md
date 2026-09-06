# Roadmap

Factcat is product analytics on the event model already in a data warehouse: it generates SQL,
runs it in the caller's warehouse, never ingests, and the three things every other tool
hard-codes — the entity, the period, and what "retained" means — are caller-supplied SQL. This
file is the public plan; releases are weekly, and each version's notes are on the
[Releases](https://github.com/gordonkjlee/factcat/releases) page. Bugs and requests go to
[Issues](https://github.com/gordonkjlee/factcat/issues).

## Now

In flight for the next release.

- **Live dry-run tier.** The hermetic suite executes on DuckDB and mocks the adapters, so a
  construct BigQuery rejects and DuckDB coerces gets through. A `live` tier dry-runs every
  statement shape Factcat emits against a real BigQuery and Snowflake at zero bytes scanned;
  the production mapping basename is refused and nothing read from the mapping is printed.
- **Production isolation.** The development launch prints which warehouse its mapping points
  at and defaults to a development mapping; the test suite never resolves a real one.
- **Snowflake spend ceiling.** The timeout on Setup becomes a session statement timeout, so a
  runaway query has a hard stop; a live test settles result-column casing on a real connection.
- **The epoch unit is never guessed.** Compile makes no warehouse call; a Unix-epoch unit is
  inferred once on Setup and persisted, and a failure raises instead of defaulting to seconds.
- **Logging.** One rotating log beside the loaded mapping; an adapter failure is logged with
  the generated SQL, so a failed run leaves an artefact.
- **Managed-table lifecycle on rows.** Create, refresh, heal and evict run on real rows in the
  suite, not on mocks.
- **CI and release hardening.** Least-privilege workflow tokens, a security policy, dependency
  updates; the publish job verifies the tag it published from, smokes the wheel with each
  warehouse extra, and alarms when PyPI lags a tag.
- **Public surfaces say the same thing.** README, PyPI page and site agree on the install
  block, the Snowflake adapter's experimental status, what managed tables are, and where the
  changelog is; a test holds them in parity. The Snowflake setup guide joins the site.

Confirmed defects live on Issues, and an open one outranks anything under Next.

## Next

In this order. Each entry says what the problem is in product terms, what the behaviour will
be, what it is not, and where the idea comes from.

**Grain switch in the app.** Setup maps one entity column, and the chart answers "weekly
active *what*?" for that one grain. This item is a switch in the Events app between the mapped
id columns — user, account, subscription, session, journey — with NULL ids excluded and event
types that never carry the chosen id hidden. A session or journey id is a legal grain wherever
the column exists, and waits on no session engine. Non-goal: no default column, no inferred
grain. IP: generic; the thesis made into a control, not a vendor's persons-versus-groups toggle.

**dbt package.** Compile Factcat specs inside a dbt project, so a retention or funnel model is
a spec file the project owns and `dbt run` materialises. Non-goal: no GitHub app, nothing
hosted. IP: generic dbt packaging; not a YAML metric layer (see Never).

**Reports as code.** Saved reports are files: a serialised spec plus chart layout, committed
beside the code that produces the events. The spec stays the API; the file is a serialisation
of it, not a second schema. Non-goal: no YAML metric types standing in for the spec. IP:
generic; dashboards as code is a category (Lightdash ships one), not a mechanism.

**dbt/GitHub connector.** Read the project's `schema.yml` for column names, types and
descriptions, so Setup and the pickers show the documentation the team already wrote;
`information_schema` stays the default and the fallback. Non-goal: no writes to the
repository. IP: generic; the names come from the caller's own project.

**Managed-tables autopilot.** Factcat already keeps a column index and an event-name census in
a write destination inside the caller's warehouse, indexing a column when a query needs it and
evicting it when demand stops. This item is the policy envelope, set once, with Factcat
autonomous inside it: caps on index storage and refresh bytes, an inclusion mode (`auto` /
`suggest`), PII exclusions by name pattern and wherever BigQuery policy tags already deny
Factcat's principal a column, and staleness targets. Inside it: columns added on a bytes-saved
calculus (full-scan cost at the observed run frequency against the cache plus its refresh,
both priced free by a dry run); a budget on how many columns may be indexed, the invariant
that stops a set of managed relations from reconstructing a source row; a rotating per-name
checksum that catches a same-count rewrite the row counts cannot; a scan ceiling on Snowflake
builds; and a physical-form advisor choosing query, table, or materialized view / dynamic
table under one portable staleness knob, switching loudly and creating before dropping. Every
decision is reversible, the live query is always the fallback, and the envelope is the
consent. IP: the database-advisor category is generic; no vendor mechanism.

**Look-back lever.** A user-chosen "search at most N days back" bound on the scans that find a
carried or anchor column value: the index is right for sparse columns and wrong for dense
ones, and the look-back is the cheap-history lever for the dense case. "All history" stays the
default, because a silent bound would turn the flagship sparse case — a tier recorded once,
years back — into `(null)`. The mechanism exists (`Breakdown.since`); the work is emitting the
bound against the raw event-time column so partition pruning holds (today's bounds compare the
converted instant, which defeats it), plus the app control and honest copy about what a bound
can miss. IP: generic predicate pushdown.

**`(frequent)` events picker group.** A `(frequent)` group at the top of the event picker, fed
by a log of the events you query, which this item starts recording. Workload-based, never
volume-based: sorting by volume reorders the list as data arrives and kills picker muscle
memory. A stable partition like `(inactive)` at the tail, not a reorder. Ships once there is
enough history to be honest. IP: generic.

**Setup layout: derived cluster keys and inverted layouts.** Setup's per-filter prune ticks
read only bare cluster keys, so a derived key — `TO_DATE(event_time)`, Snowflake's own
recommendation, or `DATE(event_time)` on BigQuery — is invisible, and Setup can warn wrongly
or miss that date filters skip blocks. Partitioning on the entity and clustering on time is a
legal warehouse; the ticks must say which filter prunes, not lecture "partition by day". On
Snowflake, say "clustering key is set; automatic maintenance is off" when that is the case.
Both warehouses. Non-goal: no health score, no second layout diagnostic. IP: generic.

**Same-instant ordering and duplicate events.** Ties at one instant resolve today by a fixed
rule — the greatest value wins for one entity, funnels take the earliest qualifying event per
step, retention the entity's first observation — which is deterministic but arbitrary whenever
two events share a timestamp: two event names at the same second, a batch load stamping one
instant on many rows, or fully duplicated rows. This item makes the tiebreak caller-supplied:
one `event_order` SQL expression on the spec (a sequence id, an ingestion timestamp, an
event-name priority `CASE`) used as the secondary key wherever instants tie, including the
index build so cached equals live; unset keeps greatest-value-wins as the documented fallback.
Fully duplicated events are a separate, explicit `dedupe_on` opt-in, because counts change and
that must never be silent; the census can report an exact-duplicate rate so the app can
suggest it. IP: sequence columns and explicit tie-breakers are ordinary SQL practice, not a
vendor's uuid ordering.

**Event-time vs current dimension.** A property as it was when the event happened (carried on
the row, or a snapshot table) against as it is now (joined from a current dimension table).
Both are legitimate questions; the report must say which one it answers. After mapping exists
for dimension tables. IP: generic.

**Stickiness.** How many of the last N periods an entity was active in: caller entity, caller
period, caller "active" predicate. After there is an app to show it in. Non-goal: not user /
day / any-event. IP: generic.

**Paths.** Path SQL from caller predicates, not from event names only. Vendor paths are usually
scoped within a session; whether later steps share the first step's episode key is an open
question below, not a silent blocker and not a reason to build a session engine first. IP:
generic.

**Lifecycle.** New, returning, resurrecting and dormant per period, with a caller period and a
caller predicate. It is the easiest report to hard-code as user / day / any-event, which is
why it is not in the first app. IP: generic.

**Experiment analysis.** Assignment is already in the warehouse: an exposure table joined to
the events on the caller's entity, with the measures the other reports already compute.
Non-goal: no flag serving — that is a write path at request time, and a Never row. IP:
generic warehouse experiment analysis (GrowthBook is the reference shape); no vendor mechanism.

**Mapping panel dock and resize.** Move the filter / mapping column to the left or right of
the results and drag its width. Independent scrolling of filters and results already exists.
Chart / table / SQL splitters, collapse and pane order are the pane layout item below. IP:
generic.

**Brand across surfaces.** The Events app carries the mark, favicon, tokens and the waiting
cat; the README carries the lockup and a social card. This item is the rest: the same tokens
and mark on Setup and later screens, new empty states, and a PyPI listing image if the
platform allows. The mascot stays the chunky slate cat with ochre glasses; copy is
thesis-first, then a grin; motion is still at idle, may pace while a job runs, and freezes
under reduced-motion. IP: our own brand.

**Axis vs data-label value formats.** One value format currently formats Y-axis ticks, point
labels and the table together. Split them: the axis can stay compact while data labels show
two decimals, and the table follows the labels. IP: generic.

**Log / ln / asinh Y-axis.** The Y-axis is linear and begins at zero, so series spanning
orders of magnitude squash. This is a display scale, not a measure: SQL, table, tooltips and
data labels stay in original units. Options: linear (default), log10, ln, and asinh — defined
at zero and on negatives, so it still works when a day is empty or a signed Sum goes below
zero. Log and ln refuse or drop zero and negative points with a note rather than clipping to
an epsilon. Non-goal: not a library field, not a `LOG(count)` measure on the spec. IP: generic
visualisation; asinh is a published statistical transform, not a vendor control.

**Legend format, layout and sort.** Today the legend is the chart library's default: shown for
more than one series, on top, in order of first appearance. Format (show / hide / auto, font
size, truncation), layout (position, alignment, wrap, a cap so eight series plus `(other)` do
not eat the plot) and sort (query rank, A–Z, Z–A, total in window), all app-side; `(other)`
pins last and `(null)` is a real group that sorts with the rest. Display-only: no re-query, no
change to `top_n`. IP: generic chart legend.

**Series colour bound to the name.** Colour is by slot today — the first series is ochre, the
second blue — so a change in top-N or sort can recolour "UK". This item keys colour by series
name: a name keeps its hex across runs and sorts until it leaves the chart; `(other)` stays
muted; `(null)` gets a real palette slot. Overrides persist on the report, not in user
preferences. Display-only. IP: generic.

**Pane resize, collapse and layout.** The workspace is a fixed grid: filter column, then Chart
/ Table / SQL stacked. Add splitters between the three panes, collapse-to-header for each, and
a few named arrangements: the current stack; Chart beside Table with SQL under; Table above
Chart. Collapsed is not closed — data and SQL stay — and the default stays the current stack
with all three open. Non-goal: no freeform tile canvas, no pop-outs, no second layout system
for narrow screens. IP: generic.

**Auto-sampling.** Events SQL always scans the filtered population; the approximate mode is
HyperLogLog-style counting on that full scan, not a row sample. Vendors sample at a fixed rate
on their user key inside their own store; that is their user hard-code and their engine. The
proposed product: the user targets a confidence interval (level and margin) and Factcat picks
the sample fraction — more breakdowns, more buckets and rarer cells mean a larger sample, or a
wider interval shown on those cells — with a manual rate as an override, not the control. The
sample unit is the caller's entity (an optional `sample_key` may pick another grain, never
replacing `entity`) and all rows for a kept id stay in, because event-level sampling breaks
Average, funnels and retention; top-N labels come from the full population. The spec must say
whether a mechanism buys a statistical guarantee or bytes billed: a hash filter on the entity
is often still a full scan, while `TABLESAMPLE` cuts bytes and is not entity-consistent.
Non-goal: not ingest downsampling, not a period enum, not a replacement for exact mode. IP:
generic survey sampling plus warehouse sampling clauses; "sample the grain, not the event" is
learned from published vendor practice, nothing is pasted.

**Type-to-filter dropdowns.** The catalog pickers — event, breakdown column, and the Setup
table, column and timezone lists — are native selects that jump by first letter only. One
shared vanilla combobox filters the already-loaded options as you type, with arrow keys,
Enter, Escape and an empty-match state; short enums stay native. Non-goal: no query per
keystroke, no property-search API, no vendor query language. IP: generic combobox.

**Hosted demo site.** A public, read-only deployment of the real app at a stable URL, so a
reader can feel the tool before `pip install factcat`. Its data is a synthetic dataset from a
generator checked into the repo — nobody's events — with an entity that is deliberately not a
user (a subscription or workspace grain), so the demo teaches caller-supplied entity, period
and retained instead of re-teaching user / day / any-event. Setup is shown, read-only or
sandboxed per visitor, because how mapping feels is half the pitch. The open question is which
engine runs it: a DuckDB execute adapter (a real adapter, walked like the others) or a
demo-owned cloud project with hard cost caps; rate limits either way, and the SQL pane still
shows real generated SQL. Non-goal: never a "connect your warehouse" path, credential entry or
upload; not a hosted product, not a trial tier, no visitor telemetry. IP: public demo
instances are generic (Lightdash, Metabase and PostHog all run one); the shape is learned,
nothing of theirs is copied.

**Screenshots in the README and on the site.** The README and site describe the Events app in
text; the only image is the mascot. Capture the shipped Events app and the Setup mapping
screen on synthetic data in the thesis's shape (a non-user grain), through a scripted,
reproducible capture so the images are retaken whenever the UI changes — a stale screenshot
lies about the product. Once shipped, retaking them joins the definition of done for any
change that visibly alters the Events or Setup UI. IP: screenshots of our own app, not staged
to imitate anyone's chrome.

**Retention and funnel in the app.** Wire the two shipped library reports into the same app as
Events, with the breakdowns the Events report already has (the retention and funnel specs
still have no `breakdowns` today). Funnel does not need sessions; whether later steps should
share the first step's episode key is an open question below, not part of this item. IP:
generic.

## Open questions

Two design questions are undecided. Neither is scheduled, and each needs full discovery before
it could be.

**Identity resolution inside Factcat.** Not ruled out, and not before it has been properly
considered. The recommended pattern today is the warehouse's own late-binding view: an
identity-free event hub plus a thin identity-join view, so a remap is a view change with no
backfill. Candidate shapes if it is ever taken up, none chosen: a query-time join to the
caller's identity map (the map stays theirs; Factcat never owns resolution logic); managed
resolved tables in the caller's write destination, which must stop short of a fully resolved
events copy because that would be the copy Factcat promises never to take; and its interplay
with dimension joins.

**Session / episode grouping.** Grain already works: an entity of `session_id` or
`journey_id` is a legal spec today, and the grain switch is the dropdown. What a session
engine would add falls into three forks, none chosen: (A) grain — already the product wherever
the column exists; (B) scope — "later steps of a funnel or path share the first step's group
key", a caller SQL expression parallel to the completion window, which would be the only new
analysis API; (C) assignment — a warehouse column first; else a dimension join; else an opt-in
managed view. Inline generation of sessions that owns the definition is refused. A default
30-minute session in the library is a Never row, not this question.

## Never

| Idea | Why not |
|------|---------|
| Tracking SDK / hosted events | Generate SQL, never take a copy. |
| Session replay storage | Ingest. |
| HogQL (or any vendor language) as an engine dialect | We speak warehouse SQL. |
| Flag serving | Write path at request time. |
| `period: day\|week\|month` replacing `period_days` | Sugar buttons are fine; replacing the general form is not. |
| Default entity column `user_id` in the library | Entity is caller-supplied. |
| Default session definition (30-minute inactivity) in the library | Session / episode is caller-supplied on the same grounds as entity. A timeout preset is legitimate sugar; it must not become the general form. The grouping itself is an open question above; this row is only the default. |
| YAML/Looker metric types replacing the spec | A later app collects a spec. |
| Pasting vendor source, trademarks, autocapture, replay | Learn; rewrite from our spec. |

To propose something, open an issue. The Never table is where the thesis lives: an idea that
lands there is not waiting for a better argument, it is the boundary of the product.
