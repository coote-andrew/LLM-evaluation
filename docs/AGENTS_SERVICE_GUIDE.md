# Build-From-Scratch Guide — `agents` git repo

> **Audience:** whoever is rebuilding the `clinical_graphs` agents service as a
> standalone git repository. This guide is the contract between the agents
> repo and the Django LLM-Evaluation Workbench. The workbench talks to the
> agents service **only over HTTP** — it never reads the agents filesystem and
> it never stores agent Python source in its own database. Everything below
> tells you what Django expects and how to provide it.

---

## 1. Scope and responsibility boundary

| Concern | Owner |
|---|---|
| Agent source files (`tools/`, `nodes/`, `patterns/`, prompts) | **agents repo** |
| `_cut/` immutable snapshots | **agents repo** |
| `git` history / `git pull` | **agents repo** |
| The `cut_version` CLI | **agents repo** |
| Module (re)loading and runtime execution | **agents repo** |
| Per-node tracing / `X-Query-Id` | **agents repo** |
| Pattern invocation (`/v1/chat/completions`) | **agents repo** |
| **Admin / registry API** (new, this guide) | **agents repo** |
| `llm_providers.yaml` file on disk | **agents repo**, written into the container by Django CI or a deploy-time generator |
| Users, runs, comparisons, evaluations, scheduling, UI | **Django workbench** |
| Metadata **cache** of the registry (name, version labels, hashes, pinned deps, declared params) | **Django workbench** |
| Rendering diffs in the UI | **Django workbench** — but it fetches source or unified-diff text from the agents API on demand |

**No Python source code is ever stored in Django's database.** Django only
caches enough *metadata* (asset names, version labels, content hashes, git
SHA, pinned-dep references, declared-parameter values) to let users browse
and pick versions without round-tripping the agents service on every click.
Anything requiring source — rendering a file, rendering a diff, rebuilding a
composition — is a live HTTP call to the agents service.

---

## 2. On-disk layout

The layout matches `Upgrade_proposal.md` §3, reproduced here for completeness.

```
clinical_graphs/
  tools/
    snomed_lookup.py                 # working copy (what the team edits)
    _cut/
      snomed_lookup__v1_0.py         # immutable snapshots
      snomed_lookup__v1_1.py
      snomed_lookup__v1_2.py
  nodes/
    summariser.py
    _cut/
      summariser__v1_0.py
      summariser__v2_0.py
    prompts/
      summariser.txt
      _cut/
        summariser__v1_0.txt
  patterns/
    clinical_note_analysis.py
    _cut/
      clinical_note_analysis__v1_0.py
```

### Rules

1. **Working copies** import other working copies:
   ```python
   from clinical_graphs.tools.snomed_lookup import snomed_lookup
   ```

2. **Cut snapshots** import other cut snapshots by version label:
   ```python
   from clinical_graphs.tools._cut.snomed_lookup__v1_2 import snomed_lookup
   ```

3. Cut files are **append-only**. They must never be edited in place. The
   admin API *must* reject any change to a known `_cut/` file's content hash
   (see §7.2).

4. Working copies declare parameters as a module-level `PARAMS` dict. This is
   the only thing the admin API reads to surface "declared parameters":

   ```python
   PARAMS = {
       "limit": 5,
       "semantic_tags": ["finding", "disorder"],
   }
   ```

   Declared parameters have **no runtime effect** — to change a parameter,
   edit the file and cut a new version. They exist purely so Django can show
   "this version of `snomed_lookup` has `limit=5`".

5. Version labels are `MAJOR_MINOR` on disk (`v1_2`), displayed in Django as
   `1.2`. Labels start at `1.0` and are strictly monotonic per asset.

6. `@latest` is a synthetic label that always points at the working copy. Its
   content hash is computed from the working file at query time.

---

## 3. The `cut_version` CLI

Owned by the agents repo. Called by developers locally after editing files:

```bash
python -m clinical_graphs.cut_version
```

### Algorithm (bottom-up cascade)

```
for each tool in tools/:
    hash = sha256(working file)
    if hash == hash(most-recent cut):
        no action  (@latest *is* that cut version)
    else:
        copy tools/<name>.py → tools/_cut/<name>__v<bumped>.py

for each node in nodes/ and each prompt in nodes/prompts/:
    if file_changed or any_dep_tool_was_cut:
        copy → _cut/, then rewrite imports in the cut snapshot to pin to
        the latest cut label of each dependency

for each pattern in patterns/:
    same as nodes, but deps are nodes
```

Defaults to minor bumps. Prints a plan, asks one question (`Y / edit / abort`).
`edit` lets the user skip an asset or promote minor → major.

On accept: performs the file copies, rewrites imports in the cut snapshots,
makes a single git commit with a generated message like:

```
cut: snomed_lookup v1.2→v1.3, summariser v2.0→v2.1, clinical_note_analysis v1.1→v1.2
```

**Do not rewrite imports in working copies** — working copies always point at
other working copies so developers can iterate freely.

### Implementation notes

- Content hashes are **SHA-256 of the UTF-8 bytes of the file**. No
  normalisation (no whitespace stripping, no EOL normalisation). If the bytes
  differ, it's a new version.
- Dependency extraction is a **static AST parse** of the source file. Look
  for `ImportFrom` nodes under `clinical_graphs.tools.*`,
  `clinical_graphs.nodes.*`, or `clinical_graphs.patterns.*`. Don't import
  the file — you don't want to execute tool decorators during a scan.
- `PARAMS` is extracted by finding the top-level `Assign` node whose target
  is `Name("PARAMS")` and evaluating its `value` with `ast.literal_eval`.
  Anything that isn't literal-evaluable is ignored (treated as `{}`) — you
  must not `exec()` source files during a scan.
- The CLI and the admin API **share** the scanning code. Factor it into
  `clinical_graphs/_registry/scan.py` or similar; the admin API just calls
  `scan()` every time `GET /admin/registry` is hit (it's cheap — pure
  filesystem + AST).

---

## 4. The admin API

FastAPI router mounted at `/admin`. Default port same as the rest of the
service.

### 4.1 Authentication

A single shared secret passed as `X-Admin-Key: <key>`. Compare with
`hmac.compare_digest`. The key is provided via env var
`CLINICAL_GRAPHS_ADMIN_KEY`; if the env var is unset, the `/admin/*` routes
refuse to start (fail-closed).

Rationale: OpenShift handles TLS; the workbench and agents are on the same
cluster; a shared secret gives us enough — no user accounts need to flow
through the admin API because all user identity lives in Django.

### 4.2 Endpoints

All responses are JSON unless noted. All endpoints require the admin key.
Error responses use FastAPI default `{"detail": "..."}` with appropriate
status codes (`401` missing/wrong key, `404` unknown asset, `409` drift
detected, `422` validation, `500` server).

#### `GET /admin/health`

```json
{
  "status": "ok",
  "git_sha": "abc123...",
  "git_dirty": false,
  "service_version": "0.2.0",
  "python": "3.11.9",
  "scanned_at": "2026-04-23T10:00:00Z"
}
```

Used by Django's sync command to decide whether to skip (unchanged SHA) or
re-scan.

#### `GET /admin/registry`

The full snapshot — everything Django needs to refresh its metadata cache in
one request.

```json
{
  "git_sha": "abc123...",
  "scanned_at": "2026-04-23T10:00:00Z",
  "assets": [
    {
      "kind": "tool",
      "name": "snomed_lookup",
      "description": "Look up SNOMED candidates for a clinical term.",
      "versions": [
        {
          "label": "1.2",
          "file_path": "clinical_graphs/tools/_cut/snomed_lookup__v1_2.py",
          "content_hash": "sha256:...",
          "git_sha": "abc123...",
          "declared_params": {"limit": 5, "semantic_tags": ["finding", "disorder"]},
          "pinned_deps": {},
          "created_at": "2026-04-20T09:00:00Z",
          "is_deprecated": false,
          "ready": true
        },
        {
          "label": "@latest",
          "file_path": "clinical_graphs/tools/snomed_lookup.py",
          "content_hash": "sha256:...",
          "git_sha": "abc123...",
          "declared_params": {"limit": 10, "semantic_tags": ["finding"]},
          "pinned_deps": {},
          "created_at": null,
          "is_working_copy": true,
          "ready": true
        }
      ]
    },
    {
      "kind": "node",
      "name": "summariser",
      "description": "...",
      "versions": [
        {
          "label": "2.1",
          "file_path": "clinical_graphs/nodes/_cut/summariser__v2_1.py",
          "content_hash": "sha256:...",
          "git_sha": "abc123...",
          "declared_params": {},
          "pinned_deps": {
            "tool.snomed_lookup": "1.2"
          },
          "created_at": "2026-04-20T09:00:01Z",
          "is_deprecated": false,
          "ready": true
        }
      ]
    }
  ]
}
```

Field notes:

- `kind`: one of `"tool"`, `"node"`, `"pattern"`, `"system_prompt"`.
- `label`: version string without the `v` prefix (e.g. `"1.2"`, `"@latest"`).
- `file_path`: path relative to the agents repo root. Used only as a display
  hint; Django does not open the file.
- `content_hash`: `"sha256:" + hex`. This is how Django detects drift.
- `git_sha`: the commit that last touched this file, or `HEAD` for the
  working copy.
- `pinned_deps`: map of dotted dependency key → version label. Keys are of
  the form `"tool.<name>"`, `"node.<name>"`, `"prompt.<name>"`. For cut
  files this is parsed from `_cut/..._v<label>` imports; for working copies
  it's the set of working-copy imports resolved to the *currently-latest*
  cut version of each dep.
- `ready`: `true` once a sandbox import has confirmed the file parses and
  loads. Versions that fail to import are included with `ready: false` and
  an `error` field; Django hides them in pickers by default but stores them
  in the registry for diagnostics.

#### `GET /admin/assets/{kind}/{name}`

Same shape as one element of `assets[]` above.

#### `GET /admin/assets/{kind}/{name}/versions/{label}/source`

```
Content-Type: text/plain; charset=utf-8
X-Content-Hash: sha256:...
```

Body is the raw file bytes. Django streams this to the browser for its
"View source" button and passes it to its diff renderer.

**Response size limit:** cap at 1 MiB; any file larger returns `413 Payload
Too Large` with the `content_hash` header still set. No legitimate tool or
node file should approach this limit.

#### `GET /admin/assets/{kind}/{name}/diff?from=1.2&to=1.3`

Server-side unified diff. The agents service has the files; Django doesn't.
Centralising the diff here keeps Django source-free.

```
Content-Type: text/plain; charset=utf-8
X-From-Hash: sha256:...
X-To-Hash:   sha256:...
```

Body is `difflib.unified_diff` output with reasonable context lines (3).
Optional query params:

- `context=N` — context-line count (default 3, max 20).
- `format=json` — return a structured response:

  ```json
  {
    "from": {"label": "1.2", "content_hash": "..."},
    "to":   {"label": "1.3", "content_hash": "..."},
    "unified": "--- ...\n+++ ...\n@@ ..."
  }
  ```

#### `POST /admin/pull`

```json
Request:
  {"ref": "main"}          # optional, defaults to configured default branch

Response:
  {
    "ok": true,
    "old_sha": "abc...",
    "new_sha": "def...",
    "changed_files": ["clinical_graphs/tools/_cut/snomed_lookup__v1_3.py", ...],
    "log": "Updating abc..def\nFast-forward\n ..."
  }
```

Runs `git fetch && git merge --ff-only origin/<ref>`. If the merge is not
fast-forward, returns `409 Conflict` with the log in the body; no state is
changed. On success the service re-scans the registry and marks
`/admin/registry` as fresh.

#### `POST /admin/reload`

Rebuild the in-memory pattern cache. Specifically:

1. Call `importlib.invalidate_caches()`.
2. Iterate every module in `sys.modules` whose name starts with
   `clinical_graphs.` and call `importlib.reload()` bottom-up (tools →
   nodes → patterns → registry).
3. Reset the pattern cache inside `clinical_graphs/registry.py`.

Returns `{"ok": true, "reloaded": 42}` on success.

If reload throws (e.g. a new cut file has a broken import), revert to the
previous state if practical, otherwise respond with `500` and a structured
error; Django treats this as a failed refresh and falls back to asking
OpenShift for a rolling restart.

#### `POST /admin/validate`

Optional but strongly recommended. Subprocess-sandbox-imports every cut
file and returns their `ready` status. Called by `cut_version` after a
successful cut, and by the sync command on demand.

```json
{
  "checked": 120,
  "ready": 118,
  "failed": [
    {
      "kind": "node",
      "name": "summariser",
      "label": "2.1",
      "error": "ImportError: cannot import name 'X' from ..."
    }
  ]
}
```

### 4.3 Composition overrides on `/v1/chat/completions`

Already described in `Upgrade_proposal.md` §6.2. Recapped so this guide is
self-contained:

Client sends:

```
POST /v1/chat/completions
Headers:
  X-Agent-Composition: {
    "pattern":          {"name": "clinical_note_analysis", "label": "1.2"},
    "node_overrides":   {"summariser": "2.0", "snomed_confirmer": "1.3"}
  }
  X-Admin-Key: <same shared secret>     # only if composition overrides used
Body:
  { "model": "clinical_note_analysis@1.2", "messages": [...] }
```

If `X-Agent-Composition` is present, the server compiles the graph from the
pinned cut files instead of the pattern's default composition. The label in
the model name must match `pattern.label` (or be omitted / `@latest`).

`X-Query-Id` is always returned in the response headers. Django records it
so per-node traces can be pulled via the existing `log_routes.py`.

### 4.4 Resume headers (Phase D — not required on day one)

For completeness: `X-Resume-From-Node` + `X-Resume-State` let Django resume
a previously-captured row from a node boundary. See `Upgrade_proposal.md`
§8.4. Safe to defer; Django will feature-detect by calling
`/admin/health` for a `features: ["resume"]` list (not required in v1).

---

## 5. Suggested repo layout

```
agents/                              # git repo root
├── README.md
├── pyproject.toml
├── requirements.txt
├── Dockerfile
├── openshift/
│   └── deployment.yaml
├── clinical_graphs/
│   ├── __init__.py
│   ├── server.py                    # FastAPI app factory
│   ├── admin_routes.py              # NEW — the routes from §4
│   ├── registry.py
│   ├── composition.py               # NEW — build graph from override recipe
│   ├── _registry/
│   │   ├── __init__.py
│   │   ├── scan.py                  # NEW — filesystem → registry snapshot
│   │   ├── hashing.py               # NEW — sha256 helpers
│   │   ├── params.py                # NEW — AST literal_eval of PARAMS
│   │   ├── deps.py                  # NEW — AST import-graph extractor
│   │   └── diff.py                  # NEW — unified_diff wrapper
│   ├── cut_version.py               # NEW — the CLI
│   ├── log_routes.py
│   ├── query_logging.py
│   ├── tools/        { .py, _cut/ }
│   ├── nodes/        { .py, _cut/, prompts/ { .txt, _cut/ } }
│   └── patterns/     { .py, _cut/ }
└── tests/
    ├── test_scan.py
    ├── test_cut_version.py
    ├── test_admin_routes.py
    └── fixtures/
```

### Mounting the admin router

```python
# clinical_graphs/server.py
import os
from fastapi import Depends, FastAPI, Header, HTTPException
import hmac

from clinical_graphs.admin_routes import router as admin_router

ADMIN_KEY = os.environ.get("CLINICAL_GRAPHS_ADMIN_KEY")
if not ADMIN_KEY:
    raise RuntimeError("CLINICAL_GRAPHS_ADMIN_KEY is required for /admin routes")

def require_admin_key(x_admin_key: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="invalid admin key")

app = FastAPI(title="clinical_graphs", version="0.2.0", lifespan=lifespan)
app.include_router(admin_router, prefix="/admin", dependencies=[Depends(require_admin_key)])
```

### Sketch of `_registry/scan.py`

```python
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "clinical_graphs"
KINDS = {
    "tool":          PKG / "tools",
    "node":          PKG / "nodes",
    "pattern":       PKG / "patterns",
    "system_prompt": PKG / "nodes" / "prompts",
}
CUT_RE = re.compile(r"^(?P<name>[a-zA-Z_][a-zA-Z_0-9]*)__v(?P<maj>\d+)_(?P<min>\d+)\.(?:py|txt)$")


@dataclass
class Version:
    label: str
    file_path: str
    content_hash: str
    git_sha: str
    declared_params: dict = field(default_factory=dict)
    pinned_deps: dict[str, str] = field(default_factory=dict)
    is_working_copy: bool = False
    ready: bool = True
    error: str | None = None


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return f"sha256:{h.hexdigest()}"


def _git_sha(p: Path) -> str:
    out = subprocess.run(
        ["git", "log", "-n1", "--format=%H", "--", str(p.relative_to(ROOT))],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return out.stdout.strip() or _head_sha()


def _params(p: Path) -> dict:
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "PARAMS":
                    try:
                        return ast.literal_eval(node.value) or {}
                    except (ValueError, SyntaxError):
                        return {}
    return {}


# ... similar _deps(), scan(), etc.
```

Keep this module *import-safe* — no side effects. The admin route imports it
and calls `scan()` on every request.

---

## 6. Deployment

### 6.1 `llm_providers.yaml`

Django generates this file (see `core/services/llm_providers_yaml.py`) and
ships it into the agents container in one of three ways, pick one:

1. **CI artifact** — Django's deploy job runs
   `python manage.py generate_llm_providers_yaml --output ./llm_providers.yaml`
   and the agents image COPYs it at build time. Simplest.
2. **ConfigMap** — Django regenerates on ModelConfig save and writes to a
   Kubernetes ConfigMap via the API; agents Pods mount it. Needs RBAC.
3. **Pull on boot** — agents container calls Django's
   `GET /api/llm_providers.yaml` at start-up. Requires Django to expose
   such an endpoint (not currently planned).

The Django side is agnostic to which you pick.

### 6.2 Env vars

```
CLINICAL_GRAPHS_ADMIN_KEY   required — shared secret
CLINICAL_GRAPHS_GIT_BRANCH  default ref for /admin/pull (default: main)
CLINICAL_GRAPHS_LOG_LEVEL   optional
LLM_<SLUG>_API_KEY          one per llm_providers.yaml entry
LLM_<SLUG>_BASE_URL         one per llm_providers.yaml entry (if not implied)
```

### 6.3 Health / probes

OpenShift should use `GET /v1/models` (no auth) as the readiness probe and
`GET /admin/health` (auth required) as the deeper check run by the workbench.

---

## 7. Guarantees Django relies on

If you change one of these, tell the Django team:

1. **Immutable `_cut/` files.** Content hashes never change. The admin API
   detects drift and returns `409 Conflict` on any mutated cut file.
2. **Version-label format:** `MAJOR_MINOR` on disk, `MAJOR.MINOR` in API
   responses. `@latest` is the working copy. No other label forms.
3. **`pinned_deps` keys are `"<kind>.<name>"`** with no label suffix; the
   value is the dep's label. Example: `{"tool.snomed_lookup": "1.2"}`.
4. **`content_hash` is SHA-256 of the raw UTF-8 bytes.** No EOL
   normalisation, no whitespace stripping. Django uses this to spot drift
   and to deduplicate entries in its cache.
5. **`GET /admin/registry` returns all assets in one response.** Django
   treats it as an atomic snapshot. If you move to pagination later, bump
   the service version and add a `next_cursor` field; Django will
   feature-detect.
6. **`X-Query-Id`** is always returned on `/v1/chat/completions`, including
   for composition-overridden runs and resumed runs.
7. **`/admin/pull` is idempotent.** Calling it when already up-to-date
   returns `200` with `old_sha == new_sha` and an empty `changed_files`.

---

## 8. Build order (sprint plan for the agents repo)

### Sprint 1 — skeleton
- New repo, copy existing `clinical_graphs/` sources.
- `Dockerfile`, `requirements.txt`, OpenShift manifests unchanged from
  today's monorepo version.
- `GET /v1/models` and `POST /v1/chat/completions` working against
  working copies only (no versioning, no admin API). Parity with today.

### Sprint 2 — on-disk versioning
- `_cut/` directory convention.
- `_registry/scan.py`, `_registry/hashing.py`, `_registry/params.py`,
  `_registry/deps.py`.
- `cut_version.py` CLI (bottom-up cascade, default minor bump).
- Populate `_cut/` with initial `v1_0` snapshots of every current working
  copy.

### Sprint 3 — admin API
- `admin_routes.py` with `X-Admin-Key` auth.
- Implement `/admin/health`, `/admin/registry`, `/admin/assets/.../source`,
  `/admin/assets/.../diff`, `/admin/pull`, `/admin/reload`, `/admin/validate`.
- Unit tests covering drift detection, unknown asset 404s, permission 401s.

### Sprint 4 — composition overrides
- `composition.py` that builds a compiled graph from a recipe.
- `X-Agent-Composition` header handling on `/v1/chat/completions`.
- End-to-end test: run the same pattern with two composition recipes and
  verify the traced per-node versions differ.

### Sprint 5 — resume (Phase D prerequisite)
- `X-Resume-From-Node` / `X-Resume-State` handling.
- Advertise `features: ["resume"]` on `/admin/health`.

Phase A of the Django side already works against Sprint 1. Sprint 2 + 3 unlock
Phase B in Django. Sprint 4 unlocks Phase C. Sprint 5 unlocks Phase D.

---

## 9. What Django does on its side

Summary of this guide's counterpart. Implemented in this repo (`core/`):

- `core/services/agents_client.py` — typed wrapper around the admin API.
  All HTTP goes through here; never hit `agents.foo` from a view directly.
- `core/models.AgentAsset` / `core/models.AgentAssetVersion` — **metadata
  cache** populated from `/admin/registry`. No source columns.
- `core/management/commands/sync_agent_registry.py` — pulls the registry
  snapshot and upserts the cache. Run manually or by Celery Beat.
- `core/services/llm_providers_yaml.py` — existing; produces
  `llm_providers.yaml` content from `ModelConfig`.
- Settings (`config/settings.py`):

  ```python
  AGENTS_SERVICE_URL       = os.environ.get("AGENTS_SERVICE_URL", "")
  AGENTS_SERVICE_ADMIN_KEY = os.environ.get("AGENTS_SERVICE_ADMIN_KEY", "")
  AGENTS_SERVICE_TIMEOUT   = float(os.environ.get("AGENTS_SERVICE_TIMEOUT", "30"))
  ```

  Django's `ModelConfig(is_agent=True).api_endpoint` is the base of the
  *runtime* (`/v1/chat/completions`) path. The admin API defaults to the
  same base; override by setting `AGENTS_SERVICE_URL` if admin traffic goes
  through a different route (e.g. internal-only service name).

---

## 10. FAQ

**Q: Why not just mount the agents filesystem into Django?**
We can't cleanly: Django and agents run in separate OpenShift Pods, often on
separate nodes. PVCs backed by RWX storage add ops complexity and there's no
good story for atomic updates across two Pods.

**Q: Why not store source in Postgres?**
Because the agents repo is the source of truth. Duplicating Python into the
evaluator's database would create a drift risk and make `git log` the
non-authoritative history, which we care about.

**Q: What if the agents service is down when Django needs a diff?**
Django shows the cached metadata (it's kept in its own DB) and surfaces a
"source unavailable — agents service unreachable" banner in the diff view.
Runs that don't need source can continue; scheduling doesn't stop.

**Q: Can I use this guide to build a totally different service (not
`clinical_graphs`)?**
Yes. The contract above is just "an admin API exposing file-based versioned
assets". Any Python app implementing §4 will work with Django. Change the
`KINDS` mapping and you have a general versioning service.
