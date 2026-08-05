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
| `image` | Overrides the language profile's base image (optional) — e.g. point at a Playwright image so `setup_commands` can install browsers and `test_command` can run e2e tests. |
| `coverage.min_percent` | Minimum coverage % — fails the validation run below it (optional; `0`/absent = off). |
| `coverage.paths` | Packages/dirs to measure (optional; default = whole project). |

## Coverage gate (`coverage`)

A code-coverage threshold that hardens validation against weak/absent tests: a
change with no exercising test drops coverage and **fails the sandbox run**, so
the agent must add real tests on retry instead of shipping untested code.

```json
{ "coverage": { "min_percent": 80, "paths": ["src"] } }
```

- **Python (out-of-the-box):** AI-OS wraps the default `pytest` command with
  `--cov=<path> --cov-report=term-missing --cov-fail-under=<min>` (`pytest-cov`
  is baked into the sandbox image). If you set your own non-pytest
  `test_command`, add coverage flags to it yourself.
- **Node / Java:** these own their coverage tooling, so AI-OS treats the
  threshold as advisory — set a coverage-producing `test_command` (e.g. Vitest
  with a `coverage.thresholds` in its config, or the JaCoCo `check` goal) and it
  fails the run itself.

Separately from the gate, **every completed task** gets an advisory
validator-quality readout in the PR body (*🔎 Validator quality*): whether it
changed code without adding a test, whether it touched CI/sensitive config, and
a cheap-model verdict on whether its tests meaningfully exercise the change.
These never block a merge — they point the human reviewer at what to check.

## Project conventions (`.ai-os/conventions.md`)

Separately from the sandbox, a project can commit a **`.ai-os/conventions.md`**
with project-level rules for the AI agents (provider-agnostic — reaches
Gemini/OpenRouter tasks too, not just the `claude` CLI). AI-OS injects it into
**both** the epic planner's decomposition (so the DAG respects the rules — e.g.
adds an i18n task) **and every task's prompt** (so each task follows them):

```markdown
# Project conventions for AI-OS agents
- All user-facing strings MUST go through the i18n system (`t('key')`), never
  hardcoded. Default language is Hungarian; add English translations.
- UI primitives are Base UI (`@base-ui/react`), NOT shadcn/Radix — compose a
  Button-as-link with `render={<Link/>}` + `nativeButton={false}`.
```

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

> Node uses **copy-isolation**: the code + `node_modules` are baked into one
> per-task image (no bind mount), so Vite/Vitest/Next resolve `node_modules`
> normally. The package manager (**pnpm** / yarn / npm) is auto-detected from the
> lockfile; the default test command is `<pm> test`, overridable with
> `test_command`.

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
