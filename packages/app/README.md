# factcat-app

Local Events chart on **your** BigQuery. Factcat generates SQL and runs it in
place. It does not ingest.

```bash
pip install factcat-app
gcloud auth application-default login
factcat-app
```

Open http://127.0.0.1:8000. Mapping is written to `.factcat.json` in the
directory you started the command from (your warehouse repo, not this
package). Add that file to `.gitignore`.

Entity is caller-supplied. There is no `user_id` default. Day/week/month
buttons fill a `date_trunc` bucket; there is no `period: day|week|month` field.

Queries are capped at 10 GiB scanned. See the root README for field-by-field
first-run notes.
