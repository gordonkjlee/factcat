# Contributing to Factcat

Factcat is product analytics on the event model already in a data warehouse. It generates
SQL, runs it in your warehouse, and never ingests. One person maintains it on a weekly release
cycle. This page is what you need before opening an issue or a pull request.

## Set up

From the repo root:

```bash
pip install -e "packages/engine[dev,all]"
```

`dev` brings DuckDB (the reference dialect the suite executes against) and pytest; `all`
brings every warehouse driver. The suite:

```bash
cd packages/engine && python -m pytest
```

It runs entirely on DuckDB with hand-computed ground truth; no warehouse credentials are
needed. A live warehouse tier exists for maintainers and is not required.

To run the app against your own warehouse, follow [Run the app](README.md#run-the-app) in
the README.

## Bugs and requests

Use the issue forms; they ask for what makes a report actionable.

- A good **bug report** names the warehouse and the Factcat version, the steps, the exact
  error text or what the chart showed, and where relevant the generated SQL from the SQL pane
  and the lines from `factcat.log`, the rotating log the app writes beside your mapping file.
  Strip project ids and anything private first.
- A good **request** states the problem in product terms, what you are trying to learn from
  your events, and what you do today instead. Read [ROADMAP.md](ROADMAP.md) first: under Now
  or Next it is already planned; in the Never table it has been decided, and the row says why.
- A **security vulnerability** goes through the repository's Security tab (private
  vulnerability reporting), never a public issue.

## The thesis, as a constraint on contributions

Every other product analytics tool hard-codes `entity = user`, `period = calendar bucket`,
`retained = did any event`. Factcat makes all three caller-supplied SQL. That is the product,
so a PR that re-introduces one of them will be declined, kindly, with a pointer at the Never
table in [ROADMAP.md](ROADMAP.md): a default `user_id` entity, a `period` enum replacing
`period_days`, or a `retained` default of "any event". Sugar over the general form is
welcome; replacing the general form is not.

## Three things a review will ask about

1. **One SQL emitter per construct.** Portability comes from sqlglot. Per-dialect SQL text
   lives in `packages/engine/factcat/dialects.py`, and only after sqlglot has been shown not
   to do it (a construct it silently strips counts). The dialect tests capture the sqlglot
   logger and fail on any warning; do not remove that capture.
2. **Every adapter is walked.** A change that generates SQL, runs a warehouse job, or shows
   warehouse chrome is checked on each shipped adapter (BigQuery, Snowflake): it behaves the
   same, it is gated off by that adapter's declared capabilities, or it has a named branch
   with a test. Say which in the PR. A green suite that only exercised one warehouse on a
   path the other also runs is not done.
3. **A new guard is shown to fail.** Break the code it protects, see the suite go red,
   restore, and say so in the PR. A test that cannot fail is documentation.

## Pull requests

- Conventional Commits: `type(scope): summary`, scope one of `engine`, `dialects`, `tests`,
  `ci`, `docs`, `app`; the body says why, in prose.
- The PR template asks for What, Why, Tests, the Adapters walk, and Known limits. Fill it in.
- Keep PRs small and focused; they land fastest.
- Generated columns are namespaced `fc_`; caller SQL is interpolated, never rewritten;
  comments say why, in a line or two, never what.

## How the maintainer works

One person, weekly cycle. Issues are triaged on Mondays. Pull requests are reviewed within
the week. An outside PR gets the same review gates as the maintainer's own before merge.
Automated merges apply only to the maintainer's own small PRs; nothing from outside merges
without a human review.

## Licence

MIT. By contributing you agree that your contribution is licensed under the same terms.

The [code of conduct](CODE_OF_CONDUCT.md) applies everywhere this project talks.
