# Agents Extraction — Cut-Over Notes

One-off notes documenting the split of the `clinical_graphs` agents service
out of this repo and into its own git repository.

**Read this once, then follow through, then archive.** Day-to-day operation is
covered by `docs/PHASE_A_GUIDE.md` (Django side) and
`docs/AGENTS_SERVICE_GUIDE.md` (agents side).

---

## 1. Why we split

- OpenShift deploys the two services as separate Pods. Sharing a filesystem
  between them (RWX PVC, sidecar mount, etc.) added ops complexity without
  a real win.
- We considered storing Python source in Django's Postgres (so Django could
  browse versions and diffs). Rejected — `git log` should remain the
  authoritative history of agent code.
- With an HTTP contract, each side evolves independently: the agents team
  can rewrite scanning/cut logic without Django migrations, and Django can
  rework its UI without touching agent internals.

See `Upgrade_proposal.md` §12 for the final architecture rationale.

---

## 2. What was cut from this repo

These are the changes landed on the `llm-evaluation` side in preparation
for the split. After they are merged, the `agents/` folder in this repo is
safe to delete.

### Code changes

- `core/signals.py` — default output of the `AUTO_GENERATE_LLM_PROVIDERS_YAML`
  signal moved from `<BASE_DIR>/agents/llm_providers.yaml` to
  `<BASE_DIR>/dist/llm_providers.yaml`. Override with
  `LLM_PROVIDERS_YAML_PATH` if your deploy pipeline wants to write
  elsewhere.
- `core/management/commands/generate_llm_providers_yaml.py` — same default
  path change; `--output` still accepts an explicit destination.
- `core/services/llm_providers_yaml.py` — module docstring + YAML header
  reworded: the file is a **deploy artefact** produced here and *consumed*
  by the external agents service.
- `config/settings.py` — new `LLM_PROVIDERS_YAML_PATH` setting (env-var
  backed). Comment on `AUTO_GENERATE_LLM_PROVIDERS_YAML` updated.
- `.gitignore` — adds `dist/` (generated outputs never go in git).

### CI changes

`.github/workflows/ci.yml`:

- Removed the `agents-smoke` job (boots the agents FastAPI service and
  pings `/v1/models`). That test belongs in the agents repo now.
- Removed the agents-image build step from the `docker-build` job. Only the
  Django image is built here.

### Docs changes

- `Upgrade_proposal.md` — §1, §3, §6, §12, §16, §18 reworded to reflect
  "separate repos, HTTP-only integration". No more `agents/` paths.
- `docs/PHASE_A_GUIDE.md` — default path, deploy flow, and troubleshooting
  rows updated for the new boundary.
- `docs/AGENTS_SERVICE_GUIDE.md` — **new**, the contract the external
  agents repo must implement.
- `docs/AGENTS_EXTRACTION.md` — this file.

### What stayed

- Everything under `core/` that talks to the agents service *at runtime* is
  untouched: `ModelConfig(is_agent=True)`, `core.services.llm_client`,
  `core.services.agents_client`, the `AgentAsset` / `AgentAssetVersion`
  metadata-cache models, `sync_agent_registry`. These are the intended
  production integration points.

---

## 3. What to delete from this repo

Once the merge with this doc lands, these paths in `llm-evaluation` are
dead weight and should be removed in a follow-up commit:

```
agents/                       # entire folder
```

Everything inside (`agents/clinical_graphs/...`, `agents/Dockerfile`,
`agents/requirements.txt`, `agents/openshift/*`, `agents/entrypoint.sh`)
moves to the new agents repo.

### Verifying nothing breaks

Before you `git rm -r agents/`:

```bash
# 1. No Django code imports from agents:
grep -r "from agents" core/ config/  # should return nothing
grep -r "import agents" core/ config/  # should return nothing

# 2. No default path references remain:
grep -R "agents/llm_providers.yaml" . --exclude-dir=.git  # should return nothing

# 3. Tests pass without the folder:
mv agents /tmp/agents-backup
python manage.py test --settings=config.test_settings
mv /tmp/agents-backup agents

# 4. The Django image still builds:
docker build -t llm-eval:check .
```

If any of those produce output or fail, fix before deleting.

---

## 4. What to set up in the new `agents` repo

Follow `docs/AGENTS_SERVICE_GUIDE.md` §5 for the full sprint plan.
Abbreviated checklist:

1. `git subtree split --prefix=agents/ -b agents-extraction` (optional —
    preserves history; alternatively start fresh with the current code
    snapshot).
2. Push that branch to a new git repo (e.g. `clinical-graphs`).
3. In the new repo:
   - Move all contents up one level so `clinical_graphs/` is at the root.
   - Add `_cut/` subfolders beside every module that has versions.
   - Add `clinical_graphs/_registry/` (scanner, hasher, AST dep extractor,
     `PARAMS` parser).
   - Add `clinical_graphs/cut_version.py` (the CLI).
   - Add `clinical_graphs/admin_routes.py` with the endpoints from
     `AGENTS_SERVICE_GUIDE.md` §4.2.
   - Add `CLINICAL_GRAPHS_ADMIN_KEY` to the OpenShift Deployment env.
4. In the new repo's CI: keep the smoke test that was removed from here
   (boot the server, curl `/v1/models` and `/admin/health`).
5. In the new repo's OpenShift manifests: expose two routes (or one route
   with two path prefixes) — `/v1/*` (pattern invocation) and `/admin/*`
   (registry / pull / reload). Or put both on one hostname if that's
   simpler.

---

## 5. Wiring the two sides together after the split

On the `llm-evaluation` side, set in Django's env/settings:

```
AGENTS_SERVICE_URL       = https://<agents-host>
AGENTS_SERVICE_ADMIN_KEY = <same shared secret exported in the agents Pod>
AGENTS_SERVICE_TIMEOUT   = 30
```

Create a `ModelConfig` row per pattern (Django admin or
`Model Configurations` page):

- `provider`: `custom` (or `local`/`vllm` — all fine, the agents service is
  OpenAI-compatible).
- `api_endpoint`: `https://<agents-host>` (the root — `/v1/chat/completions`
  is appended automatically by `llm_client`).
- `is_agent`: ✅.
- `agent_alias`: short slug, used in UI labels.
- `model_name`: the pattern alias (`clinical_note_analysis`,
  `admission_snomed_coding`, ...).

Regenerate the providers YAML and ship it to the agents Pod:

```bash
python manage.py generate_llm_providers_yaml --output ./dist/llm_providers.yaml
# Then copy dist/llm_providers.yaml into the agents container via your
# deploy pipeline (CI artifact / ConfigMap / image bake — pick one; see
# AGENTS_SERVICE_GUIDE.md §6.1).
```

Optional: populate Django's metadata cache:

```bash
python manage.py sync_agent_registry
```

Do a smoke call end-to-end:

```bash
python manage.py shell -c "
from core.models import ModelConfig
from core.services.llm_client import call_llm
mc = ModelConfig.objects.get(agent_alias='clinical-notes')
r = call_llm(mc, prompt='Patient presents with chest pain and SOB.')
print('error:', r.get('error'))
print('query_id:', r.get('query_id'))
print('keys in graph state:', list(r.get('agent_state', {}).keys()))
"
```

---

## 6. Rollback

If the split causes a regression:

- **The Django side is backwards-compatible.** `ModelConfig(is_agent=True)`
  still points at any running agents service (monorepo or separate repo);
  the `api_endpoint` is the only URL that matters for runtime calls.
- **Reverting just CI and docs is cheap** — the code changes are
  path-default-only.
- **If the external agents repo is broken**, the fallback is to continue
  running the old monorepo image from your image registry until it's
  fixed. Nothing in Django has hard-coded the new deploy path.

---

## 7. Done-when

The split is complete when, in this repo:

- [ ] `agents/` directory is gone.
- [ ] `grep -R "agents/" . --exclude-dir=.git` returns only references that
  are either historical notes in `docs/AGENTS_EXTRACTION.md`,
  `Upgrade_proposal.md` or docstrings describing the external service.
- [ ] `python manage.py test --settings=config.test_settings` passes.
- [ ] `docker build .` builds the Django image clean.
- [ ] CI (`.github/workflows/ci.yml`) has one tests job + one image-build
  job; no agents jobs.

...and in the new agents repo:

- [ ] `clinical_graphs` package compiles, `/v1/models` returns the
  expected patterns.
- [ ] `/admin/health` and `/admin/registry` return valid JSON with the
  shared admin key.
- [ ] Its own CI has a smoke test that boots the app and pings both
  endpoints.
