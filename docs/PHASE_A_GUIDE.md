# Phase A — Agent Integration: Deployment & Use Guide

**Scope:** what Phase A delivers, how to deploy it to PRD, and how to use it.
**Audience:** you (the sole user/operator right now).
**Status:** Phase A complete.

> **Note:** The agents service has been extracted into its own git repository
> (see `docs/AGENTS_SERVICE_GUIDE.md`). This guide assumes the agents service
> is a separately-deployed HTTP endpoint reachable over the network — not a
> subfolder of this repo.

---

## 1. What Phase A ships

| Change | Purpose |
|---|---|
| `ModelConfig.is_agent` + `ModelConfig.agent_alias` fields | Flag a `ModelConfig` row as pointing at the (external) `clinical_graphs` agents service instead of a raw LLM. |
| `core/services/llm_client.py` — agent dispatch path | When `is_agent=True`, `call_llm` posts to `/v1/chat/completions`, skips irrelevant OpenAI knobs, and surfaces the agent's graph state in `result["parsed"]` and `result["agent_state"]`, plus the `X-Query-Id` header. |
| `core/services/llm_providers_yaml.py` + `generate_llm_providers_yaml` management command | Render an `llm_providers.yaml` deploy artefact from `ModelConfig` rows (non-agent, active). API keys stay in env vars. The file is shipped to the external agents service by CI/deploy tooling. |
| `core/signals.py` | Optional auto-regeneration of `llm_providers.yaml` on every `ModelConfig` save/delete — gated by `AUTO_GENERATE_LLM_PROVIDERS_YAML = True`. Writes to `<BASE_DIR>/dist/llm_providers.yaml` by default. |
| `config/test_settings.py` | SQLite in-memory settings so `make test` runs without Postgres. |
| `.github/workflows/ci.yml` | Django tests + Django Docker image build. The agents smoke test lives in the agents repo now. |
| Migration `0011_modelconfig_is_agent_and_alias` | Adds the two new columns. |

Phase A is purely additive. Pre-existing prompt→LLM TestRuns keep working exactly as before.

---

## 2. Deploying to PRD

### 2.1 Apply the database migration

```bash
python manage.py migrate
```

The migration adds two nullable columns to `core_modelconfig`. Zero data loss; zero downtime.

### 2.2 (Optional) Enable auto-regeneration of `llm_providers.yaml`

In `config/settings.py` (or via environment variables):

```python
AUTO_GENERATE_LLM_PROVIDERS_YAML = True
# Optional — defaults to <BASE_DIR>/dist/llm_providers.yaml (gitignored).
# LLM_PROVIDERS_YAML_PATH = "/srv/agents-deploy/llm_providers.yaml"
```

Or via environment:

```bash
AUTO_GENERATE_LLM_PROVIDERS_YAML=true
LLM_PROVIDERS_YAML_PATH=/srv/agents-deploy/llm_providers.yaml
```

Leave this off if you'd rather regenerate explicitly (safer if two Django instances could race on one file).

### 2.3 Confirm the smoke path

After deploy:

```bash
# 1. Generate llm_providers.yaml from current ModelConfigs
python manage.py generate_llm_providers_yaml --print        # dry-preview
python manage.py generate_llm_providers_yaml                # write to <BASE_DIR>/dist/llm_providers.yaml

# 2. Ship that YAML to the external agents service — see docs/AGENTS_SERVICE_GUIDE.md §6.1
# 3. Run the full test suite (SQLite, no Postgres needed)
python manage.py test --settings=config.test_settings
```

---

## 3. Day-to-day use

### 3.1 Point a ModelConfig at the agents service

In the admin or the **Model Configurations** page:

| Field | Value |
|---|---|
| Name | e.g. `Clinical notes (agent)` |
| Provider | `custom` (or `local`/`vllm` — all OK; the agents service is OpenAI-compatible) |
| API endpoint | `http://<agents-host>:<port>` — the root; `/v1/chat/completions` is appended automatically |
| API key | Leave blank (the agents service is auth-free internally) or set any non-empty token if you've enabled auth on the agents ingress |
| Model name | The pattern alias — e.g. `clinical_note_analysis`, `discharge_summary`, `admission_snomed_coding`. See `GET /v1/models` on the agents service for the live list. |
| **Is agent** | ✅ check |
| **Agent alias** | A unique slug, e.g. `clinical-notes`. Needed for UI tagging and future YAML references. |
| Default timeout | 300+ seconds — agent patterns can take a while. |

### 3.2 Run a TestRun against the agent

Use the existing **Create Test Run** form — no UI changes in Phase A. Select:

- A `TestCaseVersion`
- A `PromptTemplate` whose `template_text` is what you want the agent to receive as a user message (often just `{input_notetext}`)
- The agent-flagged `ModelConfig`

Execution is unchanged — `core.tasks.execute_test_run` dispatches through `call_llm`, which now routes agent configs through the OpenAI-compatible path to the agents service.

**What gets stored** on each `TestRunResult`:

- `raw_response` — JSON-encoded graph state (everything public the pattern exposed)
- `response_parsed` — the same graph state as a Python dict (populated automatically for agent runs — no need to set `response_format=JSON` on the prompt template)
- `latency_ms`, `input_tokens`, `output_tokens` — from the agents service `usage` block

The `X-Query-Id` header is captured in memory on the `call_llm` return value (`result["query_id"]`). Phase A does not yet persist this — that's Phase C (`test_run_node_result` hydration).

### 3.3 Evaluate the output

Because `response_parsed` is populated, **Field match** and **Python eval** evaluation configs work out of the box:

```python
# Python eval example — check the agent extracted an ICD code
result = {
    "has_code": bool(response_parsed and response_parsed.get("primary_icd")),
    "matches_expected": (
        response_parsed.get("primary_icd", "").strip()
        == expected_output_fields.get("output_primary_icd", "").strip()
    ),
}
```

### 3.4 Regenerate `llm_providers.yaml`

Whenever you add/edit/deactivate a non-agent `ModelConfig`:

```bash
# Default path: BASE_DIR/dist/llm_providers.yaml (gitignored).
python manage.py generate_llm_providers_yaml

# Or point at the exact drop location for your deploy pipeline:
python manage.py generate_llm_providers_yaml --output /srv/agents-deploy/llm_providers.yaml

# Or just print, no side effects:
python manage.py generate_llm_providers_yaml --print
python manage.py generate_llm_providers_yaml --dry-run
```

The YAML is a **deploy artefact** — it needs to be shipped into the external
agents service's container (CI copy, ConfigMap, or boot-time pull; see
`docs/AGENTS_SERVICE_GUIDE.md` §6.1). After the file lands, restart the
agents service or POST `/admin/reload` on it so the new providers are
picked up.

### 3.5 Secrets in the generated YAML

The generator **never** inlines API keys. For a `ModelConfig` named `Claude Sonnet` it emits e.g.:

```yaml
providers:
  - id: mc-a1b2c3d4e5f6
    type: anthropic
    api_key_env: LLM_CLAUDE_SONNET_A1B2C3D4_API_KEY
    models:
      - name: claude-sonnet-4-5
        alias: claude-sonnet
```

The agents service resolves `api_key_env` against its environment at startup. In PRD, set these env vars on the agents OpenShift Deployment (not in the YAML). The env-var name is deterministic — regenerating the YAML for the same `ModelConfig` always produces the same name.

### 3.6 Verifying an agent call end-to-end

```bash
# From the Django host, with DJANGO_SETTINGS_MODULE set:
python manage.py shell -c "
from core.models import ModelConfig
from core.services.llm_client import call_llm
mc = ModelConfig.objects.get(agent_alias='clinical-notes')
r = call_llm(mc, prompt='Patient presents with chest pain and SOB.')
print('error:', r.get('error'))
print('query_id:', r.get('query_id'))
print('keys in graph state:', list(r.get('agent_state', {}).keys()))
print('latency_ms:', r.get('latency_ms'))
"
```

---

## 4. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `API error 404: ... pattern 'xxx' not found` | `model_name` on the `ModelConfig` doesn't match a registered pattern. | `curl <agent-host>/v1/models` — use one of the `id`s. |
| `API error 422: ... extra inputs` | Agents service version pre-dates `temperature`/`max_tokens` tolerance. | Should not happen with Phase A — the client drops these for `is_agent=True`. If it does, pull agents main. |
| `response_parsed` is `None` on an agent TestRun | Agent returned plain text in `content` without `message.parsed`. | The `content` JSON is still usable; set `response_format=JSON` on the `PromptTemplate` so `llm_client` parses `content` as a fallback. |
| `llm_providers.yaml` not being refreshed | `AUTO_GENERATE_LLM_PROVIDERS_YAML` not set, or the generated file never made it onto the agents service's disk. | Run `python manage.py generate_llm_providers_yaml --output <absolute-path>` as a post-deploy step, then ship that file to the agents service (it runs in a separate Pod). |
| `make test` fails on Postgres connect | Old Makefile, or you ran `python manage.py test` without `--settings`. | Use `make test` (updated) or pass `--settings=config.test_settings`. |

---

## 5. What Phase A does NOT do (coming later)

- **No versioning.** `model_name = "pattern@v1.2"` syntax is not yet honoured; the agents service is called with whatever you put in `model_name`, and the agent returns whatever the current working copy computes. Phase B adds `_cut/` versioning and the `cut_version` CLI.
- **No per-node trace storage.** `X-Query-Id` is captured but not persisted on `TestRunResult`. Phase C adds `test_run_node_result`.
- **No composition overrides.** You can't yet say "use `summariser@v2.0` within this TestRun". Phase C adds `X-Agent-Composition`.
- **No checkpoints / resume.** Phase D.
- **No experiments, no scheduling.** Phase E.

All additions so far are additive and reversible — the `is_agent` / `agent_alias` columns are nullable, the YAML generator only writes when you ask it to, and the signal is opt-in.
