# `.ai-os/sandbox.json` — per-project sandbox configuration

Commit this file at your project's repo root (`.ai-os/sandbox.json`) so it lands
in every git worktree AI-OS validates. It lets a project declare what its test
suite needs **beyond** the language default (`pytest` / `npm test` / `mvn test`
under `--network none`) — most importantly a **database**.

The project owns this file and its **reference-data seed script**: AI-OS never
needs to understand your schema, it just starts the declared services on an
isolated network and runs the setup/seed + test commands you provide.

Every field is optional; if the file is absent, validation stays a single,
fully network-isolated container (`--network none`).

## How a database is isolated

When you declare a `database`, the setup + test containers run on a Docker
`--internal` network: they can reach the DB by its hostname, but have **no route
to the internet** — so untrusted, agent-written test code can use the DB yet
still cannot exfiltrate. The DB's third-party dependencies (psycopg2, the Prisma
client, a JDBC driver) are installed in the network-enabled two-phase build, not
at test time.

## Fields

| Field | Meaning |
| --- | --- |
| `database.image` | DB image, e.g. `postgres:16` or `pgvector/pgvector:pg16`. |
| `database.hostname` | DNS name the app connects to on the internal network (default `db`). |
| `database.env` | Env for the DB container (`POSTGRES_USER`/`_PASSWORD`/`_DB`, …). |
| `database.ready_command` | Readiness probe run via `docker exec` until it exits 0 (default `pg_isready -U <user> -d <db>`). |
| `env` | Env injected into the **setup + test** containers — e.g. the connection string. |
| `setup_commands` | Commands run (DB reachable, internet not) before the tests: migrations + your reference-data seed. |
| `test_command` | Overrides the language default test command (optional). |

`setup_commands` / `test_command` run in the language's dependency image, so use
tooling available there (Python image → `python`/`pip` deps; Node → `npm`/`npx`
+ installed deps; Maven → `mvn`). For Postgres-native seeding from an image
without `psql`, seed via your app's own tooling (a Flyway seed migration, a
`tsx`/`python` script that calls your repository layer, etc.).

## Examples

### Python (Postgres, psycopg2)

`requirements.txt` includes `psycopg2-binary`; `scripts/seed.py` populates
reference rows.

```json
{
  "database": {
    "image": "postgres:16",
    "hostname": "db",
    "env": { "POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app" }
  },
  "env": { "PGHOST": "db", "PGUSER": "app", "PGPASSWORD": "app", "PGDATABASE": "app" },
  "setup_commands": ["python scripts/seed.py"]
}
```

### Node / Next.js + Prisma (Postgres)

```json
{
  "database": {
    "image": "postgres:16",
    "hostname": "db",
    "env": { "POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app" }
  },
  "env": { "DATABASE_URL": "postgres://app:app@db:5432/app" },
  "setup_commands": [
    "npx prisma migrate deploy",
    "npx tsx scripts/seed-reference.ts"
  ],
  "test_command": "npm run test:unit"
}
```

> Caveat: the Node profile currently uses `npm` and an out-of-tree
> `node_modules` (via `NODE_PATH`). That is fragile for bundler-based runners
> (Vite/Vitest/Next) and does not cover `pnpm` — both are flagged follow-ups.

### Java / Spring Boot (Postgres)

Seed as a Flyway seed migration (no `psql` needed in the Maven image), or an
app-level seeder invoked via `mvn`.

```json
{
  "database": {
    "image": "postgres:16",
    "hostname": "db",
    "env": { "POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app" }
  },
  "env": {
    "SPRING_DATASOURCE_URL": "jdbc:postgresql://db:5432/app",
    "SPRING_DATASOURCE_USERNAME": "app",
    "SPRING_DATASOURCE_PASSWORD": "app"
  }
}
```
