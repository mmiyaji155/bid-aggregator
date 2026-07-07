# GCP deployment direction

## Target architecture

- Run ingestion as Cloud Run Jobs.
- Trigger jobs with Cloud Scheduler.
- Store application data in Cloud SQL, preferably PostgreSQL.
- Store secrets such as Slack webhooks and SMTP credentials in Secret Manager.
- Emit logs to Cloud Logging and alert on non-zero job exits.

## Current local status

The current application is stable enough for local SQLite ingestion smoke tests:

- KKJ ingestion writes raw fetch records and normalized items.
- pportal ingestion writes normalized items.
- Re-running the same query updates existing rows instead of duplicating them.

SQLite should remain the local development backend. It should not be the production
database for Cloud Run Jobs because container filesystems are not durable across
runs.

## Required before production deployment

1. Add a database abstraction or SQLAlchemy layer that supports PostgreSQL.
2. Add migrations for PostgreSQL-compatible DDL.
3. Move scheduled execution from `scripts/daily_run.sh` to Cloud Run Jobs.
4. Pass query settings through environment variables or a checked-in config file.
5. Add a small smoke command that exits non-zero when ingestion returns no rows or
   when source IDs are missing.
6. Add Cloud Logging-friendly structured logs for source, query, fetched, new,
   updated, and error counts.

## Suggested jobs

- `bid-aggregator-kkj-ingest`
  Runs `bid-cli full-ingest` for configured date windows.

- `bid-aggregator-pportal-ingest`
  Runs `python -m bid_aggregator.cli.pportal_ingest` with keyword and date filters.

- `bid-aggregator-backfill`
  Runs `bid-cli backfill` for a selected historical period. This job can be
  re-run safely because rows are upserted by source item ID, URL, or content
  hash.

- `bid-aggregator-notify`
  Runs saved searches and notifications after ingestion jobs finish.
