# Upgrade Proposal: Integrating the `clinical_graphs` agents service with the LLM Evaluation Workbench

**Project:** LLM Evaluation Workbench + `clinical_graphs` agent platform
**Status:** Proposal / Phase A shipped; later phases pending
**Date:** April 2026
**Supersedes / extends:** `proposal.md`
**See also:** `docs/AGENTS_SERVICE_GUIDE.md` — the contract for the *separate* agents git repo.

---

## 1. Purpose

The `clinical_graphs` agents service is a standalone FastAPI + LangGraph
application (in its own git repo) that exposes agent **patterns** over an
OpenAI-compatible `/v1` API. The Django-based **LLM Evaluation Workbench**
(this repo, `core/`) currently evaluates single-prompt LLM calls against
test-case rows.

This proposal folds the two systems into a single evaluation platform that can:

1. **Schedule evaluations** (recurring via Celery Beat).
2. **Vary components per evaluation** — for a given run, pin a specific version of a pattern, its nodes, and its tools.
3. **Version tools, nodes, and patterns** through a cascading "cut version" CLI that walks the dependency graph bottom-up.
4. **Store the full composition alongside each evaluation** so results can be meaningfully compared ("rows 1–20 used v1.1, rows 21–40 used v1.2 — v1.2 is 1.2× better on component X").
5. **Pull new agent code into the running app without a full redeploy** via a "Pull & refresh" button in the web UI.
6. **Show diffs between any two versions** of a tool, node, pattern, or full composition tree.
7. **Capture every node's state at every row** so any intermediate step can be replayed from a checkpoint, optionally with a different downstream composition.

---

## 2. Guiding principles

1. **Every cut version is an immutable file on disk.** Frozen snapshots live in a `_cut/` subfolder beside the working copy. They coexist so they can be imported side-by-side in one process, diffed line-by-line, and referenced precisely by an eval row.
2. **Versioning is a bottom-up cascade.** One CLI command cuts tools, then nodes, then patterns, auto-bumping dependents whose deps moved. Users rarely need to think about it mid-work.
3. **Composition is carried in import statements, not in sidecar YAML.** A cut pattern's `.py` file physically imports the specific cut files of its dependencies. What the Python says is what runs.
4. **Parameters are baked into the tool/node version.** To vary a knob (`limit=5` vs `limit=10`), cut a new version. No runtime parameter injection; no recipe-editable overrides. Declared parameters are surfaced as metadata so you can see what each version does.
5. **Every TestRun records the exact composition tree it ran against.** This is the data that lets us say "rows 1–20 used v1.1, rows 21–40 used v1.2" and back that claim with stored inputs/outputs per node.
6. **Git stays as the audit backbone.** The UI deals in labels; under the hood every version is a committed file.
7. **Agents stays a separate service in a separate git repo.** FastAPI + OpenAI-compatible API; Django/Celery calls it over HTTP. Keeps the agents OpenShift deployment intact and the Django container lean.
8. **Django never reads the agents filesystem and never stores agent source in Postgres.** All file access (source, diffs, version listings, git pull) is mediated by an HTTP admin API exposed by the agents service (`/admin/*`). Django keeps a *metadata-only* cache (names, labels, content hashes, pinned deps, declared params) so pickers and lists render without round-trips, but anything that needs source is a live fetch. See `docs/AGENTS_SERVICE_GUIDE.md` for the contract.

---

## 3. File-on-disk versioning scheme

### 3.1 Layout

Working copies sit at the usual path inside the *agents repo*. Frozen
snapshots go in a `_cut/` subfolder beside the working copy:

```
clinical_graphs/                # package root inside the agents repo
  tools/
    snomed_lookup.py            # working copy (latest)
    _cut/
      snomed_lookup__v1_0.py
      snomed_lookup__v1_1.py
      snomed_lookup__v1_2.py
  nodes/
    summariser.py               # working copy
    _cut/
      summariser__v1_0.py
      summariser__v2_0.py
    prompts/
      summariser.txt
      _cut/
        summariser__v1_0.txt
        summariser__v2_0.txt
  patterns/
    clinical_note_analysis.py   # working copy
    _cut/
      clinical_note_analysis__v1_0.py
      clinical_note_analysis__v1_1.py
```

- **Working copies** (e.g. `summariser.py`) import other working copies:
  `from clinical_graphs.tools.snomed_lookup import snomed_lookup`
- **Cut snapshots** import other cut snapshots with version labels baked into the path:
  `from clinical_graphs.tools._cut.snomed_lookup__v1_2 import snomed_lookup`

This means a cut file is fully self-contained: the tree of imports statically determines its composition.

### 3.2 Version label format

- Semver-style, auto-computed by the CLI: `MAJOR_MINOR` (stored on disk as `v1_2`, displayed in the UI as `1.2`).
- Starts at `1.0`. Minor bump: `1.2 → 1.3`. Major bump: `1.3 → 2.0`.
- Labels are immutable and unique per asset.

### 3.3 Working copy is a first-class version

A TestRun may pin to `summariser@latest` (the working copy). In the `composition_snapshot` this is recorded with both the symbolic `@latest` marker and the SHA-256 `content_hash` of the working file at run time. Such runs are flagged `uses_working_copy=True` — they still show up in comparisons, but with a visible "working copy" badge. If you want reproducibility, cut a version first.

### 3.4 Django-side registry (metadata cache)

Two lightweight tables that **cache** what the agents service exposes via its
admin API. They mirror the snapshot returned by `GET /admin/registry`. Django
never stores source code — only the metadata needed for pickers, diffs (which
are fetched live), and composition-snapshot FKs:

```
agent_asset              # 1 row per (kind, name)
  id, kind (tool | node | pattern | system_prompt), name, description,
  is_active, last_synced_at

agent_asset_version      # 1 row per cut file; also 1 synthetic row for "@latest"
  id, asset_id, label (e.g. "1.2" or "@latest"), is_working_copy,
  file_path, git_sha, content_hash, declared_params JSON,
  pinned_deps JSON,         # {"tool.snomed_lookup": "1.2"}  (label strings, not FKs)
  is_deprecated, ready, import_error,
  created_at_agent, first_seen_at, last_synced_at
```

`pinned_deps` is populated from the snapshot; the agents service derives it
from a static AST parse of the cut file's import statements, so it's
authoritative without Django needing to see source. Composition is still
encoded in the cut file's Python imports on the agents side — Django stores
only the resolved labels for display/filtering.

`sync_agent_registry` (see §16 Phase B) pulls the snapshot via
`core.services.agents_client.AgentsClient.registry()` and upserts these
tables. Cut-file content hashes are append-only by policy; drift is reported
and the sync command exits non-zero.

---

## 4. The `cut_version` CLI

### 4.1 Invocation

Run locally by the developer in their terminal (post-edit, pre-push):

```bash
python manage.py cut_version
```

No arguments. The CLI walks the dependency graph bottom-up.

### 4.2 Cascade algorithm

1. **Tools (leaves)** — for each file in `tools/*.py`:
   - Compute the content hash. If it matches the most recent cut version → no action; the registry simply records that `@latest` currently *is* that version.
   - If it differs from the most recent cut version → auto-minor-bump, copy the working file into `_cut/<name>__v<new>.py`. Leaf files have no deps to rewrite.
2. **Nodes** — for each file in `nodes/*.py` (and prompts in `nodes/prompts/*.txt`):
   - Has the node file itself changed since its last cut? If yes, cut.
   - Have any of its tool deps moved to a new cut version during step 1? If yes, cut.
   - When cutting: copy into `_cut/`, then **rewrite imports** in the snapshot to pin against the currently-latest cut version of every dependency.
   - System prompts in `nodes/prompts/` follow the same logic — a prompt change or underlying node change cuts a new prompt version.
3. **Patterns** — same logic. A pattern is cut if its own file changed *or* any node it uses was cut in step 2. Imports in the cut snapshot are rewritten to pin node versions.

All cuts default to **minor** bumps. The CLI tells the user what it did and asks a single final question:

```
Cuts planned:
  tool   snomed_lookup          v1.2 → v1.3   (content changed)
  node   summariser             v2.0 → v2.1   (dep snomed_lookup bumped)
  node   orchestrator           unchanged
  pattern clinical_note_analysis v1.1 → v1.2   (dep summariser bumped)

Accept [Y], edit [e], abort [a]?
```

On `edit`, the user can promote any proposed minor bump to major, skip an asset (leaving it uncut), or add release notes. On `Y`, the CLI performs all copies, rewrites imports, inserts `agent_asset_version` rows in Django, and commits the new files to git with a generated message.

### 4.3 Why this design

- Matches how you actually work: edit several files, then cut everything at once.
- Developer doesn't have to reason about each file individually — defaults handle 95% of cases.
- Imports in cut files are always pinned, so runs are bit-reproducible from the Python alone.
- Unchanged files don't accumulate version numbers for no reason.

### 4.4 Anti-mutation guard

The CLI refuses to overwrite an existing `_cut/` file. `sync_agent_registry` also computes content hashes of all known cut versions and fails loudly if any file's hash has drifted — cut files are append-only by policy.

---

## 5. Declared parameters

### 5.1 How parameters are declared

Tools (and optionally nodes) declare a module-level `PARAMS` dict describing the knobs baked into that version:

```python
# clinical_graphs/tools/snomed_lookup.py
from langchain_core.tools import tool

PARAMS = {
    "limit": 5,
    "semantic_tags": ["finding", "disorder"],
}

@tool
def snomed_lookup(term: str) -> list[dict]:
    """Look up SNOMED candidates for a clinical term."""
    ...
```

### 5.2 No runtime overrides

Changing a parameter means editing the working file and cutting a new version. There is no mechanism to override `PARAMS` at run time or from a composition recipe. This keeps the versioning model strict: every parameter value used by an evaluation corresponds to a tangible, diffable file.

If you want to sweep a parameter across values (e.g. `limit ∈ {5, 10, 20}`), upload three tool versions with different `PARAMS` values and reference them as three distinct tool versions in an Experiment (see §7.2).

### 5.3 Where parameters are surfaced in the UI

The `PARAMS` dict is read by `sync_agent_registry` and stored on the `agent_asset_version` row (`declared_params` JSON). It appears in three places:

1. **Asset version detail page** — a "Parameters" section listing each knob and its baked value.
2. **TestRun detail → Composition tab** — effective parameters for every tool/node in the run's composition, resolved from the pinned versions.
3. **Composition diff view** — when two pattern versions or two TestRuns differ in a parameter (even if the code is otherwise identical), the parameter change is highlighted alongside version bumps.

---

## 6. Execution topology

Agents remains a separate FastAPI service; Django/Celery calls it over HTTP.

### 6.1 Selecting a pattern version

The OpenAI `model` field carries the label:

```
model: "clinical_note_analysis@v1.2"
```

`"clinical_note_analysis"` (no suffix) resolves to the working copy (`@latest`). The evaluation framework always pins explicitly.

### 6.2 Per-run composition overrides

A TestRun can override individual node versions at creation time (e.g. "use `summariser@v2.0` even though `clinical_note_analysis@v1.2` pins `@v2.1`"). The Django Celery task sends the resolved recipe on every request:

```
POST /v1/chat/completions
Headers:
  X-Agent-Composition: { ...run recipe JSON... }
Body:
  { "model": "clinical_note_analysis@v1.2", "messages": [...] }
```

If `X-Agent-Composition` is absent, the pattern runs with its Python-pinned defaults. If present, the server swaps node functions according to the recipe before compiling the graph. The composition sent on the header is the authoritative record stored on the TestRun.

### 6.3 Changes required in the agents repo

(These are implemented in the separate agents git repo per
`docs/AGENTS_SERVICE_GUIDE.md` §5.)

- `registry.py`: version-aware `get_pattern(name, label)`, `list_patterns()`, knowledge of `@latest` working copies.
- `composition.py` (new): build a compiled graph from a composition recipe, swapping node functions.
- `server.py`: read `X-Agent-Composition`; attach the resolved composition to the query log; expose `/admin/reload`.
- `logged_node` wrappers remain; per-node snapshots are fed back to Django (see §8).

### 6.4 Provider config unification

Django's `core.models.ModelConfig` becomes the source of truth for LLM endpoints. An `llm_providers.yaml` deploy artefact is *generated* from `ModelConfig` rows (written to `<BASE_DIR>/dist/llm_providers.yaml` by default) and shipped to the external agents service by CI/deploy tooling. A new `is_agent` flag marks configs that point at the agents service (as opposed to a raw LLM); agent configs get an `agent_alias` field.

Because parameters (including model bindings) are baked into node files, the only thing `llm_providers.yaml` needs to describe is how to *reach* each LLM. Node code still calls `get_llm("sonnet")`, but which endpoint "sonnet" resolves to is driven by `ModelConfig`.

---

## 7. Variance mechanisms

### 7.1 Explicit per-run selection

The existing `testrun_create` form gains two fields when the selected `ModelConfig.is_agent` is true:

1. **Pattern version** — dropdown of non-deprecated cut versions of the selected pattern asset (plus `@latest`).
2. **Overrides table** — one row per node in the selected pattern; default version is the one pinned by the pattern's cut file; a dropdown allows selecting a different cut version of the same node. (No per-parameter overrides; if you want different parameters, pick a different node version.)

A YAML preview of the resolved composition is shown before submit. On submit, the recipe is locked into `TestRun.composition_snapshot`.

### 7.2 Matrix / Experiments

A new `Experiment` table declares dimensions:

```
experiment
  id, name, test_case_version_id, prompt_template_id (optional),
  dimensions JSON,   # {"pattern": ["clinical_note_analysis@v1.1", "@v1.2"],
                     #  "nodes.summariser": ["summariser@v2.0", "@v2.1"]}
  deny_list JSON,    # optional list of combinations to skip
  created_by, created_at

experiment_run
  id, experiment_id, test_run_id, cell_label
```

The planner expands the dimensions into the Cartesian product (minus deny-list), creates one `TestRun` per cell via the normal pipeline, and auto-populates a `Comparison`.

This is how parameter sweeps work: upload three `snomed_lookup` versions with `limit ∈ {5, 10, 20}`, then add `"tools.snomed_lookup": ["@v2.0", "@v2.1", "@v2.2"]` to the experiment dimensions.

### 7.3 Row-level variance

**Preferred:** two `TestRun`s against the same `test_case_version` with `row_limit=20`, different compositions, grouped in a `Comparison`. Uses existing `parent_run` / `skip_rows_from_parent` semantics.

*Not planned:* in-run version scheduling (`rows 1–20 → v1.1, 21–40 → v1.2`).

---

## 8. Checkpoints and resumed runs

### 8.1 State capture (always on)

Every run's per-node input/output is captured for every row — the agents service already emits these via `logged_node` + `query_logging`. Django persists them as:

```
test_run_node_result
  id, test_run_result_id, node_name, node_version_id,
  input  JSON,          # full graph state going into this node
  output JSON,          # partial state returned by this node
  elapsed_ms, error, created_at
```

No opt-in is required — all rows of all runs get this treatment. (Large string fields are capped per `CLINICAL_GRAPH_LOG_MAX_CHARS`.)

### 8.2 Named checkpoints (bookmarks)

Any `test_run_node_result` row can be **named** by a user to make it easier to find later:

```
run_checkpoint
  id, test_run_node_result_id (FK),
  name, description, tags JSON,
  created_by, created_at
```

Naming doesn't change the underlying data; it's just a bookmark. The resume UI surfaces all `test_run_node_result` rows plus named checkpoints first.

### 8.3 Resumed TestRuns

Resumption always produces a **new TestRun** linked to the original. Two UI flows, both creating the same data structure:

- **Single-row replay** — pick one checkpoint, create a TestRun with `row_limit=1` and a single pre-hydrated state. Useful for "what if I used `snomed_lookup@v2.2` here?".
- **Batch replay** — pick the *node* at which to resume from an original TestRun; the system loads every row's saved state at that node boundary and continues downstream. Useful for "re-run only the confirmer step across all 200 rows with the new version".

New fields on `TestRun`:

```
TestRun
  + resume_from_testrun       FK → TestRun (nullable)
  + resume_from_node          CharField (nullable; which node to start from)
  + resume_from_checkpoint    FK → run_checkpoint (nullable; set only for single-row replay)
```

### 8.4 How resumption executes

The agents API gains two optional headers on `/v1/chat/completions`:

```
X-Resume-From-Node: "snomed_confirmer"
X-Resume-State:     <base64-encoded JSON of the graph state at that node boundary>
```

When both are present, the server builds the composition as usual, then uses LangGraph's state-seeding mechanism to start execution at `X-Resume-From-Node` with the provided state instead of from `START`. Downstream nodes run normally and their output is returned.

For batch replay, Django's Celery worker fetches each row's saved state from `test_run_node_result`, sends one request per row with that row's `X-Resume-State`, and records the new per-row results under the new TestRun. The original run is untouched.

### 8.5 Composition overrides + resumption

The `X-Agent-Composition` header works exactly as before. A resumed run can change versions of any node, including the node it resumes at (that node will then run on the hydrated state). Whatever is sent in the recipe becomes the resumed run's `composition_snapshot`.

---

## 9. "Pull & refresh" flow

Accessed under **Agents → Refresh registry**, admin-only. Because Django has no
filesystem access to the agents repo, every step below is an HTTP call to the
agents service's admin API (specified in `docs/AGENTS_SERVICE_GUIDE.md` §4).

1. User clicks **Pull & refresh**.
2. Celery task `git_pull_and_refresh`:
   1. `POST /admin/pull` on the agents service → the service does
      `git fetch && git merge --ff-only` in its own checkout, then re-scans
      its filesystem and returns `{old_sha, new_sha, changed_files}`.
   2. `python manage.py sync_agent_registry` (which now calls
      `GET /admin/registry` on the agents service):
      - Receives one JSON snapshot of every asset and every version.
      - For each cut version, upserts an `agent_asset_version` row keyed on
        `(asset_id, label)`.
      - If a previously-seen cut version's `content_hash` has drifted
        (remote hash ≠ stored hash), **abort with "immutable version
        mutated"** and list offenders; cut files are append-only by policy
        on the agents side, but Django double-checks so a corrupted
        service is caught early.
      - Updates the `@latest` synthetic version row for each working copy
        with the latest working-file hash.
      - Copies `declared_params` and `pinned_deps` straight from the
        snapshot — the agents service parsed the `PARAMS` constant and the
        static import graph when it scanned its own files.
      - Marks `agent_asset`s missing from the snapshot as `is_active=False`
        (rows never deleted; TestRuns reference by FK).
      - Records any failed-to-import versions (`ready=False`, `import_error`)
        from the `/admin/validate` results if they were in the snapshot.
   3. `POST /admin/reload` to the agents FastAPI service so it re-imports
      the new cut modules. If non-200, fall back to rolling-restart of the
      agents OpenShift Deployment.
3. The page streams the task log (HTMX) and shows a summary: **Added /
   Deprecated / Failed** version lists.

All actions (who pulled, which SHA range, which versions moved, whether any
drift was detected) are written to an `agent_registry_event` table for audit.

---

## 10. Diff viewers

All source and diff rendering fetches text from the agents service on demand
via `AgentsClient.source()` / `AgentsClient.diff()`; Django never stores the
bytes. Responses are short-cached in-process (e.g. per-request memoisation)
to keep a diff view with many panels cheap.

- **Single-asset diff.** Pick two cut versions of any asset → call
  `GET /admin/assets/{kind}/{name}/diff?from=X&to=Y` and render the returned
  unified diff, syntax-highlighted. Parameters from `PARAMS` (held in the
  metadata cache) are shown above the source diff, with changed keys
  highlighted.
- **Composition diff.** Pick two pattern versions (or two TestRuns) → tree diff:

  ```
  pattern        clinical_note_analysis  v1.1 → v1.2
    node.summariser                      v2.0 → v2.1   [diff]
    tool.snomed_lookup                   v1.2 → v1.3   [diff]
       params                            limit: 10 → 5
    node.problem_list                    v1.3 (unchanged)
    prompt.summariser                    v1.0 (unchanged)
  ```

  Each `[diff]` opens the file-level diff.
- **Eval-result diff.** Given two runs with different compositions, render per-row side-by-side outputs with textual highlighting. Can be scoped to a single node's output, which is especially useful for inspecting resumed-run differences.

---

## 11. Scheduling

Use `django-celery-beat` (DB-backed schedules):

```
scheduled_evaluation
  id, name, cron_expression, enabled,
  target_kind        (test_run | experiment),
  target_template    JSON,   # pattern version, overrides, row limit, etc.
  next_run_at, last_run_at,
  created_by, created_at
```

When the schedule fires, the Beat task instantiates a new `TestRun` or `Experiment` from the template and dispatches via the existing pipeline.

---

## 12. Repository layout

**Decided: separate repositories, HTTP-only integration.** The agents service
lives in its own git repo (and its own OpenShift Pod); the Django workbench
lives in this repo (and its own OpenShift Pod). They share no filesystem and
no database.

### Why

- OpenShift deploys the two Pods independently; shared RWX PVCs add ops
  complexity without a real win.
- Storing Python source in Django's Postgres was considered and rejected —
  it would make `git log` the non-authoritative history of agent code, and
  duplicate the file-on-disk system the `cut_version` CLI is designed for.
- With an explicit HTTP contract, each side can be rebuilt, versioned, and
  rolled back independently. The agents team can evolve scanning / cut
  logic without Django migrations.

### What's in each repo

**`agents` repo** (built from `docs/AGENTS_SERVICE_GUIDE.md`):

- `clinical_graphs/` — source code, including `_cut/` immutable snapshots.
- `clinical_graphs/cut_version.py` — the cascade CLI (§4).
- `clinical_graphs/_registry/` — filesystem scanner, hashing, AST dep
  extractor, `PARAMS` parser.
- `clinical_graphs/admin_routes.py` — the HTTP admin API consumed by Django.
- `Dockerfile`, `openshift/*`, `requirements.txt` — unchanged shape; adds
  `CLINICAL_GRAPHS_ADMIN_KEY` env var requirement.

**`llm-evaluation` repo** (this repo):

- `core/` — Django app. Adds `AgentAsset`, `AgentAssetVersion` metadata
  tables; `core.services.agents_client.AgentsClient` wraps the agents admin
  API; `sync_agent_registry` management command; composition picker UI.
- No `agents/` subfolder. The agents code lives in its own repo (see
  `docs/AGENTS_SERVICE_GUIDE.md` for the build-from-scratch contract and
  `docs/AGENTS_EXTRACTION.md` for the one-off cut-over notes).

### Inter-service contract

- Runtime: `POST /v1/chat/completions` with optional
  `X-Agent-Composition` + `X-Admin-Key` headers. Driven by
  `ModelConfig(is_agent=True).api_endpoint`.
- Registry: `GET /admin/registry`, `GET /admin/assets/...`,
  `POST /admin/pull`, `POST /admin/reload`, `POST /admin/validate`.
  Driven by `AGENTS_SERVICE_URL` + `AGENTS_SERVICE_ADMIN_KEY` settings.

CI for each repo stays narrow: Django runs its own test suite (including
`AgentsClient` tests against a mock transport); the agents repo runs its own
tests plus a smoke job that boots the FastAPI app and hits `/v1/models` and
`/admin/health`. No cross-repo CI is required.

### Extraction record

The `agents/` subfolder that lived here during Phase A has been removed. See
`docs/AGENTS_EXTRACTION.md` for the exact cut-over notes (what was deleted,
what to set up in the new agents repo, how `llm_providers.yaml` flows from
here to there).

---

## 13. Data model changes (summary)

**New tables** (Django side only; agents service keeps its own state on its filesystem)

- `agent_asset`, `agent_asset_version` — §3.4. **Metadata cache only; no source columns.**
- `agent_registry_event` — §9 audit (tracks calls to `/admin/pull`, `/admin/reload`, and the resulting sync outcomes).
- `experiment`, `experiment_run` — §7.2.
- `scheduled_evaluation` — §11.
- `test_run_node_result` — §8.1.
- `run_checkpoint` — §8.2.

**Changes to existing tables**

- `TestRun`:
  - `composition_snapshot` (`JSONField`).
  - `pattern_version` (nullable FK → `agent_asset_version`).
  - `experiment` (nullable FK).
  - `query_ids` (`JSONField`, list of `X-Query-Id` per row).
  - `resume_from_testrun`, `resume_from_node`, `resume_from_checkpoint` — §8.3.
- `ModelConfig`:
  - `is_agent` (`BooleanField`).
  - `agent_alias` (unique, nullable).
- `Comparison`:
  - `experiment` (nullable FK).
- `EvaluationConfig`:
  - `target_node` (nullable) — for node-level judging.

All additions are additive. Today's simple-prompt TestRuns continue to work unchanged.

---

## 14. UI surfaces

**New top-level nav: Agents**

- **Registry** — table of `agent_asset`s with version counts and last-updated.
- **Asset detail** — version list with actions: *View source*, *Diff against…*, *Mark deprecated*. Dedicated "Parameters" section showing each version's baked `PARAMS`.
- **Diff view** — code diff + parameter diff + (for patterns) composition tree diff.
- **Refresh registry** — admin-only "Pull & refresh" with streaming output.

**Changed existing pages**

- **Model Configurations** — `is_agent` toggle; "Used as agent alias" panel.
- **Create Test Run** — pattern version + node-version overrides when `is_agent` config selected; YAML composition preview with effective parameters.
- **Test Run detail**:
  - "Composition" tab showing pinned versions and effective parameters.
  - "Node trace" expand per row (from `test_run_node_result`).
  - "Save as checkpoint" action on any `(row, node)` pair.
  - "Resume from here" action (single-row or batch, pre-filled into the Resume page).
- **Comparisons** — composition diff panel when two runs differ.

**New pages**

- **Experiments → Create / Detail** — matrix builder, cell grid, auto-Comparison.
- **Scheduled evaluations** — thin wrapper over `django-celery-beat` admin.
- **Checkpoints browser** — searchable list of named `run_checkpoint`s plus all `test_run_node_result` rows, with filters for pattern, node, row range.
- **Resume TestRun** — unified form for single-row replay or batch replay, with node-version overrides and a YAML composition preview.

---

## 15. Worked example

*Compare the default `snomed_confirmer` against a variant that uses a tighter `snomed_lookup` on the already-run admission notes — without rerunning the whole pattern.*

1. Edit `tools/snomed_lookup.py`; change `PARAMS["limit"]` from 10 to 5.
2. Run `python manage.py cut_version`. CLI output:

   ```
   Cuts planned:
     tool  snomed_lookup           v1.2 → v1.3   (params changed: limit 10 → 5)
     node  snomed_resolver         v2.0 → v2.1   (dep snomed_lookup bumped)
     pattern admission_snomed_coding v1.4 → v1.5  (dep snomed_resolver bumped)
   Accept [Y/e/a]?
   ```
   Press `Y`, push.
3. In the web UI → **Agents → Refresh registry** → *Pull & refresh*.
4. On the existing TestRun for `admission_snomed_coding@v1.4`, go to its detail page. Click the `snomed_rephraser → snomed_resolver` boundary. Click *Resume from here → batch*.
5. On the Resume form: override `pattern` to `@v1.5`, confirm node version overrides are auto-set. Submit.
6. A new TestRun is created. For each row, the saved state at `snomed_resolver`'s input is hydrated; execution continues from there with the new tool version. Results populate the new TestRun.
7. Add both runs to a Comparison. The composition diff highlights the parameter change (`limit: 10 → 5`). Per-row diffs show which SNOMED concepts differ.

No rerun of the (expensive) `admission_extractor` LLM call; only the downstream portion executes.

---

## 16. Phased delivery

**Phase A — Plumbing**  *(status: shipped)*
1. Django → agents HTTP call via `ModelConfig(is_agent=True)`; end-to-end hello-world pattern run.
2. `ModelConfig` → `llm_providers.yaml` generator.
3. CI running Django tests against the current repo; agents smoke-test job.

**Phase B — Versioning (split between the two repos)**

*In the `agents` repo* (per `docs/AGENTS_SERVICE_GUIDE.md`):
4. `_cut/` folder convention + `cut_version.py` CLI with cascade.
5. `_registry/scan.py` + `admin_routes.py`: `/admin/registry`, `/admin/assets/.../source`, `/admin/assets/.../diff`, `/admin/pull`, `/admin/reload`, `/admin/validate`.

*In the `llm-evaluation` repo (this one)*:  *(status: landing)*
6. `agent_asset`, `agent_asset_version` metadata tables + migration.
7. `core.services.agents_client.AgentsClient`.
8. `sync_agent_registry` management command (calls `/admin/registry`).
9. `agent_registry_event` table + "Pull & refresh" UI; single-asset diff viewer (rendered from `/admin/.../diff` responses); parameter display.

**Phase C — Run integration**
8. `composition_snapshot` on TestRun; create-run UI with pattern version + overrides.
9. `test_run_node_result` hydration from `X-Query-Id` traces.
10. Composition diff viewer; node-level `EvaluationConfig.target_node`.

**Phase D — Checkpoints & resumption**
11. `run_checkpoint` table and bookmarks.
12. Resume TestRun (single-row and batch); `X-Resume-From-Node` / `X-Resume-State` in agents API.
13. Eval-result diff viewer.

**Phase E — Experiments & scheduling**
14. `Experiment` + matrix planner + auto-Comparison.
15. `ScheduledEvaluation` via Celery Beat.

Each phase is independently shippable.

---

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Editing a `_cut/` file in place | `sync_agent_registry` rejects content-hash changes on known cut versions; files are append-only by policy. |
| `cut_version` mis-identifies a dependency | CLI's pre-action summary lists every planned cut; `edit` option lets the user skip or adjust any of them before commit. |
| Stale agents registry after `git pull` | Explicit `/admin/reload`; fallback to OpenShift rolling restart. |
| Broken imports in a new cut crash the agents service | Subprocess-sandbox import check during sync; failing versions marked `ready=False`. |
| Working-copy run drift | `composition_snapshot` stores `content_hash` for every `@latest` component, so the exact code used is recoverable even after subsequent edits. |
| Very large `test_run_node_result` table | Existing `CLINICAL_GRAPH_LOG_MAX_CHARS` truncation applies; optional archival/TTL policy for results older than N days. |
| Resumed run's state schema out of sync with new node version | The resumed-at node receives hydrated state keys; new node version is free to add/override keys. If the new version *removes* a state key a downstream node depends on, the run fails cleanly and the error is surfaced on the resumed run. |
| Celery Beat fires during agents reload | HTTP client retries with backoff; 503 is transient. |
| Reference to a deprecated version in a new run | Run-create form hides deprecated versions by default; existing runs still reference by FK. |

---

## 18. Open questions (for confirmation before implementation)

1. Repo layout — **resolved**: separate repos, HTTP-only integration (see §12).
2. Trace storage in production — use Postgres (existing `clinical_graph_node_log`) or JSONL for the `test_run_node_result` source? Affects the hydration query path and the retention policy.
3. Should `@latest` TestRuns be excluded from Comparisons by default (to discourage cherry-picking unreproducible results into dashboards), or just visually tagged?
4. Phase order — as written, Versioning (B) before Run Integration (C) before Checkpoints (D). Swap C and D if checkpoints are urgent and versioning can wait?

---

## 19. Appendix — glossary

- **Tool** — a `@tool`-decorated function (one per file), invoked by a node or by an LLM via `bind_tools`.
- **Node** — an async function `async def <name>(state: dict) -> dict` representing one LangGraph step.
- **Pattern** — a compiled LangGraph workflow (State + `build()`), selected by clients via the `model` field.
- **Cut version** — an immutable frozen snapshot of a tool / node / pattern / system prompt file, living in a `_cut/` subfolder with a `__v<MAJOR>_<MINOR>.py` suffix.
- **Working copy** — the non-`_cut/` file at its canonical path; always importable as `@latest`.
- **Cascade cut** — the bottom-up algorithm of the `cut_version` CLI: tools first, then nodes, then patterns, with dependents auto-bumped when a dep moves.
- **Declared parameters** — the module-level `PARAMS = {...}` dict on a tool/node, baked into that version; surfaced in UI but not overridable at run time.
- **Composition** — the tree of `{pattern version, node versions, tool versions, system prompts}` that describes a pattern run; encoded as Python imports in a cut file.
- **Composition snapshot** — the JSON recipe stored on a TestRun at creation time; the authoritative record of exactly what ran.
- **Checkpoint** — a saved `(row, node-boundary)` graph-state snapshot, either auto-captured (via `test_run_node_result`) or user-named (`run_checkpoint`).
- **Resumed TestRun** — a new TestRun that hydrates its initial state from a checkpoint and starts execution at a specified node rather than `START`.
- **`X-Query-Id`** — UUID header returned by the agents service; used by Django to pull per-node traces.
