"""Deterministic project scaffolding for `ai-os init` — start a project from
zero so AI-OS has a real git `main` + a working test baseline to build on.

Templates are written directly (no `npx create-*` / network / host toolchain at
init time), so scaffolding is deterministic and hermetic. Each preset lays down
a minimal-but-WORKING baseline: source + a passing test + a `.ai-os/sandbox.json`
wired for that stack, so the very first `ai-os epic run` already validates.

The `fastapi-react` monorepo is laid out so the existing sandbox validates it
without extra machinery: dependency manifests live at the repo ROOT (Python
installs to global site-packages; the Node copy-image's root `node_modules`
resolves from a subdir upward), and a per-language `.ai-os/sandbox.json`
(`languages` map) points each language's tests at its subdir.
"""
from __future__ import annotations

import json
from pathlib import Path

PRESETS = ("fastapi", "react", "fastapi-react", "spring", "next-prisma", "startup")
# Presets that support the `--with-db` flag (a Postgres sidecar + migration/seed).
DB_CAPABLE = ("fastapi", "fastapi-react", "spring", "next-prisma")

_GITIGNORE_PY = "__pycache__/\n*.pyc\n.venv/\nvenv/\n.pytest_cache/\n.ai-os/worktrees/\n"
_GITIGNORE_NODE = "node_modules/\ndist/\n.vite/\n"

_FASTAPI_MAIN = '''\
from fastapi import FastAPI

app = FastAPI(title="Scaffolded API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
'''

_FASTAPI_TEST = '''\
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
'''

_FASTAPI_REQS = "fastapi\nuvicorn\nhttpx\npytest\n"

_VITE_TSCONFIG = '''\
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noEmit": true
  },
  "include": ["src"]
}
'''

_VITE_APP = '''\
export function greeting(name: string): string {
  return `Hello, ${name}!`
}

export default function App() {
  return <h1>{greeting("world")}</h1>
}
'''

_VITE_APP_TEST = '''\
import { describe, it, expect } from "vitest"
import { greeting } from "./App"

describe("greeting", () => {
  it("greets by name", () => {
    expect(greeting("AI-OS")).toBe("Hello, AI-OS!")
  })
})
'''

_VITE_MAIN = '''\
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import App from "./App"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
'''

_VITE_INDEX_HTML = '''\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Scaffolded App</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
'''

_VITE_CONFIG = '''\
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

export default defineConfig({ plugins: [react()] })
'''


def _react_package_json(name: str, typecheck: str, tsx_dir: str) -> str:
    return f'''\
{{
  "name": "{name}",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {{
    "dev": "vite",
    "build": "tsc && vite build",
    "typecheck": "{typecheck}",
    "test": "vitest run"
  }},
  "dependencies": {{
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  }},
  "devDependencies": {{
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "typescript": "^5.5.3",
    "vite": "^5.4.0",
    "vitest": "^2.1.0"
  }}
}}
'''


def _readme(title: str, body: str) -> str:
    return f"# {title}\n\nScaffolded by `ai-os init`.\n\n{body}\n"


def _dumps(obj) -> str:
    return json.dumps(obj, indent=2) + "\n"


def _db_sandbox_json(env=None, setup_commands=None, test_command=None) -> str:
    """A `.ai-os/sandbox.json` declaring a Postgres sidecar (+ optional env,
    setup/seed, test override). The DB comes up on a `--internal` network; the
    setup/seed + tests run against it, no internet."""
    config: dict = {"database": _DB_SERVICE}
    if env:
        config["env"] = env
    if setup_commands:
        config["setup_commands"] = setup_commands
    if test_command:
        config["test_command"] = test_command
    return _dumps(config)


def _fastapi_files(prefix: str = "") -> dict[str, str]:
    p = prefix
    return {
        f"{p}app/__init__.py": "",
        f"{p}app/main.py": _FASTAPI_MAIN,
        f"{p}tests/__init__.py": "",
        f"{p}tests/test_health.py": _FASTAPI_TEST,
    }


def _react_files(prefix: str = "") -> dict[str, str]:
    p = prefix
    return {
        f"{p}index.html": _VITE_INDEX_HTML,
        f"{p}vite.config.ts": _VITE_CONFIG,
        f"{p}tsconfig.json": _VITE_TSCONFIG,
        f"{p}src/main.tsx": _VITE_MAIN,
        f"{p}src/App.tsx": _VITE_APP,
        f"{p}src/App.test.tsx": _VITE_APP_TEST,
    }


_GITIGNORE_JAVA = "target/\n*.class\n.ai-os/worktrees/\n"

# -- Spring Boot (Java + Maven) ----------------------------------------------


def _spring_pom(with_db: bool) -> str:
    db_deps = ""
    if with_db:
        db_deps = '''\
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-data-jpa</artifactId>
    </dependency>
    <dependency>
      <groupId>org.flywaydb</groupId>
      <artifactId>flyway-core</artifactId>
    </dependency>
    <dependency>
      <groupId>org.flywaydb</groupId>
      <artifactId>flyway-database-postgresql</artifactId>
    </dependency>
    <dependency>
      <groupId>org.postgresql</groupId>
      <artifactId>postgresql</artifactId>
      <scope>runtime</scope>
    </dependency>
'''
    return f'''\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.3.2</version>
    <relativePath/>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>0.0.1-SNAPSHOT</version>
  <properties>
    <java.version>17</java.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
{db_deps}    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-test</artifactId>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
'''


_SPRING_APP = '''\
package com.example.demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class DemoApplication {
    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }
}
'''

_SPRING_CONTROLLER = '''\
package com.example.demo;

import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class HealthController {
    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok");
    }
}
'''

# A slice test (no full context / no DB) so the DB-free baseline validates.
_SPRING_TEST = '''\
package com.example.demo;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(HealthController.class)
class HealthControllerTest {
    @Autowired
    MockMvc mvc;

    @Test
    void healthReturnsOk() throws Exception {
        mvc.perform(get("/health"))
           .andExpect(status().isOk())
           .andExpect(jsonPath("$.status").value("ok"));
    }
}
'''


# -- Next.js + Prisma (TypeScript) -------------------------------------------


def _next_prisma_package_json(with_db: bool) -> str:
    seed = ',\n    "prisma": { "seed": "tsx prisma/seed.ts" }' if with_db else ""
    return f'''\
{{
  "name": "app",
  "private": true,
  "version": "0.0.0",
  "scripts": {{
    "dev": "next dev",
    "build": "next build",
    "typecheck": "tsc --noEmit",
    "test": "vitest run"
  }},
  "dependencies": {{
    "next": "^14.2.5",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "@prisma/client": "^5.18.0"
  }},
  "devDependencies": {{
    "@types/node": "^20.14.0",
    "@types/react": "^18.3.3",
    "typescript": "^5.5.3",
    "prisma": "^5.18.0",
    "tsx": "^4.16.0",
    "vitest": "^2.1.0"
  }}{seed}
}}
'''


_NEXT_TSCONFIG = '''\
{
  "compilerOptions": {
    "target": "ES2020",
    "lib": ["dom", "dom.iterable", "esnext"],
    "module": "esnext",
    "moduleResolution": "bundler",
    "jsx": "preserve",
    "strict": true,
    "skipLibCheck": true,
    "noEmit": true,
    "esModuleInterop": true,
    "resolveJsonModule": true
  },
  "include": ["**/*.ts", "**/*.tsx"]
}
'''

_NEXT_LAYOUT = '''\
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
'''

_NEXT_PAGE = '''\
export default function Home() {
  return <h1>Scaffolded Next.js app</h1>
}
'''

_NEXT_LIB = '''\
export function slugify(input: string): string {
  return input.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
}
'''

_NEXT_LIB_TEST = '''\
import { describe, it, expect } from "vitest"
import { slugify } from "./slug"

describe("slugify", () => {
  it("slugifies", () => {
    expect(slugify("  Hello World! ")).toBe("hello-world")
  })
})
'''


def _prisma_schema(with_db: bool) -> str:
    return '''\
generator client {
  provider = "prisma-client-js"
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

model Widget {
  id   Int    @id @default(autoincrement())
  name String @unique
}
'''


_PRISMA_SEED = '''\
import { PrismaClient } from "@prisma/client"

const prisma = new PrismaClient()

async function main() {
  await prisma.widget.upsert({
    where: { name: "reference-widget" },
    update: {},
    create: { name: "reference-widget" },
  })
  console.log("seeded")
}

main()
  .then(() => prisma.$disconnect())
  .catch((e) => {
    console.error(e)
    prisma.$disconnect()
    process.exit(1)
  })
'''


# -- Postgres DB config (--with-db) ------------------------------------------

_DB_SERVICE = {
    "image": "postgres:16",
    "hostname": "db",
    "env": {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app"},
}

# Python (psycopg2) DB seed + test, mirroring tests/test_sandbox_db.py's proven shape.
_PY_DB_SEED = '''\
import psycopg2

conn = psycopg2.connect()
conn.autocommit = True
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS widgets (id serial primary key, name text unique)")
cur.execute("INSERT INTO widgets (name) VALUES ('reference-widget') ON CONFLICT DO NOTHING")
print("seeded")
'''

_PY_DB_TEST = '''\
import psycopg2


def test_seeded_reference_row_present():
    conn = psycopg2.connect()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM widgets WHERE name = 'reference-widget'")
    assert cur.fetchone()[0] >= 1
'''

_SPRING_FLYWAY_V1 = '''\
CREATE TABLE widgets (
    id   BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
'''

_SPRING_FLYWAY_V900 = '''\
INSERT INTO widgets (name) VALUES ('reference-widget')
ON CONFLICT (name) DO NOTHING;
'''

_SPRING_APP_PROPS_DB = '''\
spring.jpa.hibernate.ddl-auto=validate
spring.flyway.enabled=true
'''

_SPRING_ENTITY_TEST = '''\
package com.example.demo;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.jdbc.AutoConfigureTestDatabase;
import org.springframework.boot.test.autoconfigure.jdbc.AutoConfigureTestDatabase.Replace;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

@SpringBootTest
@AutoConfigureTestDatabase(replace = Replace.NONE)
class WidgetRepositoryTest {
    @Autowired
    JdbcTemplate jdbc;

    @Test
    void seededReferenceRowIsPresent() {
        Integer count = jdbc.queryForObject(
            "SELECT count(*) FROM widgets WHERE name = 'reference-widget'", Integer.class);
        assertThat(count).isGreaterThanOrEqualTo(1);
    }
}
'''


# -- Startup (Vanilla HTML/CSS/JS + Sim) ------------------------------------

_STARTUP_INDEX_HTML = '''\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Startup Demo</title>
    <link rel="stylesheet" href="styles/reset.css" />
    <link rel="stylesheet" href="styles/tokens.css" />
    <link rel="stylesheet" href="styles/layout.css" />
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="sim/sim.js"></script>
    <script type="module" src="sim/seed.js"></script>
    <script type="module" src="app.js"></script>
  </body>
</html>
'''

_STARTUP_RESET_CSS = '''\
*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}
body {
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
'''

_STARTUP_TOKENS_CSS = '''\
:root {
  --color-primary: #0066cc;
  --color-bg: #ffffff;
  --color-text: #1a1a1a;
  --font-family: system-ui, -apple-system, sans-serif;
  --spacing-unit: 8px;
}

@media (prefers-color-scheme: dark) {
  :root {
    --color-bg: #121212;
    --color-text: #f0f0f0;
  }
}
'''

_STARTUP_LAYOUT_CSS = '''\
.container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 16px;
}

.flex {
  display: flex;
}

.grid {
  display: grid;
}
'''

_STARTUP_SIM_JS = '''\
export const store = {
  get(key) {
    const val = localStorage.getItem(key);
    return val ? JSON.parse(val) : null;
  },
  set(key, val) {
    localStorage.setItem(key, JSON.stringify(val));
  }
};

export async function api(path, body = null) {
  return new Promise((resolve) => {
    setTimeout(() => {
      resolve({ status: "ok", path, data: body });
    }, 100);
  });
}
'''

_STARTUP_SEED_JS = '''\
import { store } from './sim.js';

export const seedData = {
  users: [{ id: 1, name: "Demo User" }],
  items: []
};

export function initSeed() {
  if (!store.get('seeded')) {
    store.set('data', seedData);
    store.set('seeded', true);
  }
}
'''

_STARTUP_APP_JS = '''\
import { initSeed } from './sim/seed.js';

document.addEventListener('DOMContentLoaded', () => {
  initSeed();
  const app = document.getElementById('app');
  if (app) {
    app.innerHTML = '<h1>Startup Demo</h1>';
  }
});
'''


def scaffold_files(preset: str, with_db: bool = False) -> dict[str, str]:
    """Return `{relpath: content}` for a preset (does not touch disk).

    `with_db=True` (only for `DB_CAPABLE` presets) adds a Postgres sidecar to
    `.ai-os/sandbox.json` plus a migration/schema + reference-data seed + a
    DB-backed test."""
    if with_db and preset not in DB_CAPABLE:
        raise ValueError(f"--with-db is not supported for the {preset!r} preset")

    if preset == "startup":
        return {
            "index.html": _STARTUP_INDEX_HTML,
            "styles/reset.css": _STARTUP_RESET_CSS,
            "styles/tokens.css": _STARTUP_TOKENS_CSS,
            "styles/layout.css": _STARTUP_LAYOUT_CSS,
            "sim/sim.js": _STARTUP_SIM_JS,
            "sim/seed.js": _STARTUP_SEED_JS,
            "app.js": _STARTUP_APP_JS,
            ".ai-os/sandbox.json": _dumps({"test_command": "echo ok"}),
        }

    if preset == "spring":
        files = {
            "pom.xml": _spring_pom(with_db),
            "src/main/java/com/example/demo/DemoApplication.java": _SPRING_APP,
            "src/main/java/com/example/demo/HealthController.java": _SPRING_CONTROLLER,
            "src/test/java/com/example/demo/HealthControllerTest.java": _SPRING_TEST,
            ".gitignore": _GITIGNORE_JAVA,
            "README.md": _readme("Spring Boot service", "Run tests: `mvn test`."),
        }
        if with_db:
            files["src/main/resources/application.properties"] = _SPRING_APP_PROPS_DB
            files["src/main/resources/db/migration/V1__widgets.sql"] = _SPRING_FLYWAY_V1
            files["src/main/resources/db/migration/V900__seed.sql"] = _SPRING_FLYWAY_V900
            files["src/test/java/com/example/demo/WidgetRepositoryTest.java"] = _SPRING_ENTITY_TEST
            files[".ai-os/sandbox.json"] = _db_sandbox_json(
                env={
                    "SPRING_DATASOURCE_URL": "jdbc:postgresql://db:5432/app",
                    "SPRING_DATASOURCE_USERNAME": "app",
                    "SPRING_DATASOURCE_PASSWORD": "app",
                },
            )
        else:
            files["src/main/resources/application.properties"] = ""
            # Matches the java profile's default; explicit so it's easy to edit.
            files[".ai-os/sandbox.json"] = _dumps(
                {"test_command": "mvn -o -Dmaven.repo.local=/deps/.m2 test"}
            )
        return files

    if preset == "next-prisma":
        files = {
            "package.json": _next_prisma_package_json(with_db),
            "tsconfig.json": _NEXT_TSCONFIG,
            "next-env.d.ts": '/// <reference types="next" />\n/// <reference types="next/image-types/global" />\n',
            "app/layout.tsx": _NEXT_LAYOUT,
            "app/page.tsx": _NEXT_PAGE,
            "src/lib/slug.ts": _NEXT_LIB,
            "src/lib/slug.test.ts": _NEXT_LIB_TEST,
            "prisma/schema.prisma": _prisma_schema(with_db),
            ".gitignore": _GITIGNORE_NODE + ".next/\n",
            "README.md": _readme("Next.js + Prisma app", "Typecheck: `npm run typecheck`."),
        }
        if with_db:
            files["prisma/seed.ts"] = _PRISMA_SEED
            files[".ai-os/sandbox.json"] = _db_sandbox_json(
                env={"DATABASE_URL": "postgresql://app:app@db:5432/app"},
                setup_commands=[
                    "npx prisma generate",
                    "npx prisma db push --accept-data-loss --skip-generate",
                    "npx tsx prisma/seed.ts",
                ],
                test_command="npm run typecheck",
            )
        else:
            files[".ai-os/sandbox.json"] = _dumps({
                "env": {"DATABASE_URL": "postgresql://app:app@localhost:5432/app"},
                "setup_commands": ["npx prisma generate"],
                "test_command": "npm run typecheck",
            })
        return files

    if preset == "fastapi":
        files = _fastapi_files()
        files[".gitignore"] = _GITIGNORE_PY
        files["README.md"] = _readme("FastAPI service", "Run tests: `pytest -q`.")
        if with_db:
            files["requirements.txt"] = _FASTAPI_REQS + "psycopg2-binary\n"
            files["scripts/seed.py"] = _PY_DB_SEED
            files["tests/test_db.py"] = _PY_DB_TEST
            files[".ai-os/sandbox.json"] = _db_sandbox_json(
                env={"PGHOST": "db", "PGUSER": "app", "PGPASSWORD": "app", "PGDATABASE": "app"},
                setup_commands=["python scripts/seed.py"],
                test_command="pytest -q",
            )
        else:
            files["requirements.txt"] = _FASTAPI_REQS
            files[".ai-os/sandbox.json"] = _dumps({"test_command": "pytest -q"})
        return files

    if preset == "react":
        files = _react_files()
        files["package.json"] = _react_package_json("frontend", "tsc --noEmit", "src")
        files[".gitignore"] = _GITIGNORE_NODE
        files["README.md"] = _readme("React (Vite + TS) app", "Typecheck: `npm run typecheck`. Test: `npm test`.")
        files[".ai-os/sandbox.json"] = '{ "test_command": "npm run typecheck" }\n'
        return files

    if preset == "fastapi-react":
        files: dict[str, str] = {}
        files.update(_fastapi_files(prefix="backend/"))
        files.update(_react_files(prefix="frontend/"))
        # Manifests at ROOT so the sandbox validates without extra machinery:
        # Python installs globally; the Node copy-image installs a root
        # node_modules that resolves from frontend/ upward.
        files["package.json"] = _react_package_json(
            "app", "tsc -p frontend/tsconfig.json --noEmit", "frontend/src"
        )
        files[".gitignore"] = _GITIGNORE_PY + _GITIGNORE_NODE
        files["README.md"] = _readme(
            "FastAPI + React monorepo",
            "Backend in `backend/` (Python), frontend in `frontend/` (TypeScript). "
            "Run each epic per language:\n\n"
            "```bash\nai-os epic run <proj> --prompt \"...\" --language python\n"
            "ai-os epic run <proj> --prompt \"...\" --language typescript\n```",
        )
        files[".ai-os/conventions.md"] = (
            "# Project conventions\n\n"
            "- Backend code lives in `backend/` (Python/FastAPI); frontend in "
            "`frontend/` (React/TypeScript).\n"
            "- Keep dependency manifests at the repo root (`requirements.txt`, "
            "`package.json`) so the sandbox can install them.\n"
        )
        ts_cfg = {"test_command": "npm run typecheck"}
        if with_db:
            files["requirements.txt"] = _FASTAPI_REQS + "psycopg2-binary\n"
            files["backend/scripts/seed.py"] = _PY_DB_SEED
            files["backend/tests/test_db.py"] = _PY_DB_TEST
            py_cfg = {
                "database": _DB_SERVICE,
                "env": {"PGHOST": "db", "PGUSER": "app", "PGPASSWORD": "app", "PGDATABASE": "app"},
                "setup_commands": ["cd backend && python scripts/seed.py"],
                "test_command": "cd backend && pytest -q",
            }
        else:
            files["requirements.txt"] = _FASTAPI_REQS
            py_cfg = {"test_command": "cd backend && pytest -q"}
        files[".ai-os/sandbox.json"] = _dumps({"languages": {"python": py_cfg, "typescript": ts_cfg}})
        return files

    raise ValueError(f"Unknown preset {preset!r}. Available: {', '.join(PRESETS)}")


def write_scaffold(root: Path, preset: str, with_db: bool = False) -> list[str]:
    """Write a preset's files under `root` (created if needed). Refuses to
    overwrite existing files. Returns the sorted list of created relpaths."""
    root = Path(root)
    files = scaffold_files(preset, with_db=with_db)
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for relpath, content in files.items():
        target = root / relpath
        if target.exists():
            raise FileExistsError(f"{target} already exists — refusing to overwrite")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(relpath)
    return sorted(written)
