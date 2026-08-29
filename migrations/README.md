# Migrations

Plain SQL files, applied in filename order with `psql`. No migration
framework: for a single-tenant instance with a handful of tables, a
directory of numbered, idempotent SQL files is easier to audit and impossible
to misconfigure.

## How to apply

Locally (any Postgres):

```bash
psql "$DATABASE_URL" -f migrations/001_baseline.sql
```

On Railway (runs psql with the service's environment, including
`DATABASE_URL`):

```bash
railway run psql '$DATABASE_URL' -f migrations/001_baseline.sql
```

## Rules

1. **Order**: apply files in ascending numeric order (`001_`, `002_`, ...).
   Never renumber a file that has been applied anywhere.
2. **Idempotency**: every statement uses `IF NOT EXISTS` / `ADD COLUMN IF
   NOT EXISTS`, so re-running a migration is always safe. This replaces
   migration-state bookkeeping: the files themselves are the state.
3. **Rollbacks** are commented at the bottom of each file, manual by
   design — dropping tables should never be one accidental command away.
4. **New migrations**: add a new numbered file; never edit an applied one
   (except to fix something that is itself idempotent-safe).
