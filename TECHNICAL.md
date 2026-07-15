# LLM Evaluation Workbench — Technical Reference

## Overview

The LLM Evaluation Workbench is a Django-based web application for systematically evaluating Large Language Model (LLM) outputs. It provides a repeatable, auditable workflow for testing whether LLMs produce correct outputs for a given task — before any model or prompt is used in production.

The tool was developed for the Clinical Informatics Centre at Royal Melbourne Hospital, but its architecture is general-purpose and applicable to any domain.

---

## Architecture

The application has three runtime components:

| Component | Technology | Role |
|-----------|-----------|------|
| `web` | Django 5 + Gunicorn | Serves the web UI and handles all user interactions |
| `worker` | Celery | Executes test runs asynchronously in the background |
| `redis` | Redis 7 | Message broker between the web server and the worker |

The database is PostgreSQL, provided externally and connected via environment variables at runtime.

```
Browser → Django (web) → Celery (worker) → LLM API
                ↓               ↓
           PostgreSQL       PostgreSQL
```

### Fallback mode

If Redis is unavailable, the application falls back to running test runs in a background thread within the web process. This is sufficient for small workloads and development without a separate worker.

---

## Deployment on OpenShift

The container image is built for OpenShift compatibility:

- Runs as UID 1001 with group 0 permissions (`g=u`), satisfying OpenShift's arbitrary UID policy
- Static files are collected at image build time (`collectstatic --noinput`)
- Database migrations run automatically at container startup via `entrypoint.sh`
- The `./data` directory (uploaded files) should be backed by a `PersistentVolumeClaim`

The application is deployed as two separate workloads (web and worker) sharing the same image, with Redis provided as a managed service or a separate deployment. All configuration is supplied via environment variables.

### Required environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string — see below |
| `CELERY_BROKER_URL` | Redis URL, e.g. `redis://redis-service:6379/0` |
| `DJANGO_SECRET_KEY` | Django secret key — generate a long random string |
| `FIELD_ENCRYPTION_KEY` | Fernet key for encrypting stored API keys — see below |
| `ALLOWED_HOSTS` | Comma-separated list of permitted hostnames / route URLs |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated HTTPS origins for CSRF, e.g. `https://your-app.apps.cluster.example.com` |

### Database connection

The application connects to PostgreSQL using the `DATABASE_URL` environment variable in the standard connection string format:

```
postgresql://USER:PASSWORD@HOST:PORT/DBNAME
```

For example:

```
postgresql://llmeval:secret@postgres-service:5432/llmeval_db
```

The `settings.py` database block reads this variable:

```python
import dj_database_url
DATABASES = {'default': dj_database_url.config(env='DATABASE_URL')}
```

Alternatively, if `dj_database_url` is not used, set individual variables (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`) and configure them explicitly in `settings.py`.

### Generating the Fernet encryption key

The `FIELD_ENCRYPTION_KEY` is a Fernet symmetric key used to encrypt LLM API keys at rest in the database. Generate a new one with:

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

> **Warning**: Changing the `FIELD_ENCRYPTION_KEY` after API keys have been stored will make existing stored keys unreadable. Store this key securely and do not rotate it without first decrypting all stored values.

### Updating `config/settings.py` for production

The current `settings.py` has development defaults that must be overridden for a production deployment. Apply the following changes, reading values from environment variables:

```python
import os

SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = False
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '').split(',')
CSRF_TRUSTED_ORIGINS = os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',')
FIELD_ENCRYPTION_KEY = os.environ['FIELD_ENCRYPTION_KEY']

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
```

### Creating the first user

After the web pod is running, exec into it to create a superuser:

```bash
oc exec -it <web-pod-name> -- python manage.py createsuperuser
```

---

## Data Model

The core data flow is:

```
TestCase
  └── TestCaseVersion  (one per CSV/Excel upload)
        └── TestCaseRow  (one per row)
              └── TestRunResult  (one per row per run)
                    └── EvaluationResult  (one per row per evaluation)

PromptTemplate  ─┐
ModelConfig     ─┴─→  TestRun  →  TestRunResult
EvaluationConfig       →  EvaluationRun  →  EvaluationResult
```

### Key models

**`TestCase`** — A named container for an evaluation task (e.g. "ICD-10 coding").

**`TestCaseVersion`** — Each CSV/Excel upload creates a new version. The schema (column names) is stored on the version, not the case, so datasets can be updated without losing history.

**`TestCaseRow`** — One row from the uploaded file. Input fields (columns prefixed `input_`) and expected output fields (columns prefixed `output_`) are stored separately as JSON.

**`PromptTemplate`** — A reusable text template using `{column_name}` placeholders. Supports `free_text` or `json` response formats. The full template text is snapshotted on the `TestRun` at execution time for reproducibility.

**`ModelConfig`** — Connection details for an LLM endpoint: provider type, API endpoint URL, API key (encrypted at rest), model name, temperature, token limit, and rate limit (RPM).

**`TestRun`** — One execution of a prompt template against a model config on a specific dataset version. Tracks status, progress, token usage, and timing.

**`EvaluationConfig`** — Defines how to score results: keyword matching, field-level JSON comparison, AI-as-judge, or human review. The `scoring_criteria` JSON field holds the check definitions.

**`EvaluationRun`** — One application of an evaluation config to a test run. Can be marked as `is_gold_standard` to designate the authoritative human assessment.

---

## User Workflow

1. **Upload test data** at `/test-cases/upload/` — provide a CSV or Excel file with columns named `input_*` and `output_*`.
2. **Create a prompt template** — write a template using `{input_column_name}` placeholders.
3. **Add a model configuration** at `/models/create/` — select a provider, enter the API key and endpoint, set defaults.
4. **Create a test run** at `/runs/create/` — select the dataset version, prompt, and model. Optionally limit to the first N rows.
5. **Monitor progress** on the run detail page (live-updating via polling).
6. **Evaluate results** — create an evaluation run using a keyword, field-match, AI-judge, or human review config.
7. **Review accuracy stats** on the evaluation run detail page.

---

## Key Areas for Customisation

### 1. LLM providers (`core/models.py` and `core/services/llm_client.py`)

The tool currently supports seven provider types:

| Provider value | Description | Auth header |
|---------------|-------------|-------------|
| `openai` | OpenAI API | `Authorization: Bearer <key>` |
| `azure_openai` | Azure OpenAI (classic deployment URL) | `api-key: <key>` |
| `azure_ai_foundry` | Azure AI Foundry / cognitive-services | `api-key: <key>` |
| `anthropic` | Anthropic Messages API | `x-api-key: <key>` |
| `vllm` | vLLM OpenAI-compatible server | `Authorization: Bearer <key>` |
| `local` | Local server (Ollama, LM Studio, etc.) | `Authorization: Bearer <key>` |
| `custom` | Any OpenAI-compatible endpoint | `Authorization: Bearer <key>` |

**To add a new provider:**

1. Add the new choice to the `Provider` class in `core/models.py`:

```python
class Provider(models.TextChoices):
    # ... existing choices ...
    MY_PROVIDER = 'my_provider', 'My Provider'
```

2. Add a handler function in `core/services/llm_client.py` (following the pattern of `_call_anthropic`).

3. Add a branch in the `call_llm()` dispatcher:

```python
if model_config.provider == Provider.MY_PROVIDER:
    result = _call_my_provider(client, url, api_key, model_name, prompt, temp, max_tok, timeout)
```

4. Run `python manage.py makemigrations && python manage.py migrate` to apply the model change.

---

### 2. CSV column naming convention (`core/services/csv_parser.py`)

Uploaded files must use a specific column prefix convention:

- Columns starting with `input_` are treated as prompt inputs.
- Columns starting with `output_` are treated as expected outputs (ground truth).
- All other columns are ignored.

This convention is enforced in `core/services/csv_parser.py`. If your source data uses a different naming scheme, update the prefix checks in that file. The parsed column names are stored on `TestCaseVersion.input_columns` and `TestCaseVersion.output_columns`, and these drive the placeholder suggestions in the prompt template UI.

---

### 3. Evaluation / scoring logic (`core/services/scorer.py`)

The `keyword_match` evaluation type runs a list of named checks against each LLM response. Checks are stored as a JSON list in `EvaluationConfig.scoring_criteria`:

```json
{
  "checks": [
    {
      "name": "contains_icd_code",
      "type": "contains_phrase",
      "phrase": "Z87.39",
      "target": "full_response",
      "case_sensitive": false
    },
    {
      "name": "has_diagnosis_key",
      "type": "json_key_exists",
      "key": "diagnosis"
    },
    {
      "name": "correct_category",
      "type": "json_key_equals",
      "json_path": "category",
      "expected_value": "respiratory"
    }
  ]
}
```

Supported check types:

| Type | Description | Required fields |
|------|-------------|----------------|
| `contains_phrase` | Phrase found in the response text (or at a JSON path) | `phrase`, optionally `target` (dot-path or `full_response`) |
| `json_key_exists` | A key with the given name appears anywhere in the parsed JSON | `key` |
| `json_value_exists` | A value containing the phrase appears anywhere in the parsed JSON | `phrase` |
| `json_key_contains` | The value at a JSON path contains the phrase | `phrase`, `json_path` |
| `json_key_equals` | The value at a JSON path exactly equals the expected value | `json_path`, `expected_value` |

All checks support an optional `"case_sensitive": true` flag (default: `false`).

**To add a new check type**, add a new `elif check_type == "my_type":` branch inside `run_keyword_checks()` in `core/services/scorer.py`.

---

### 4. AI judge prompt (`EvaluationConfig.judge_prompt_template`)

When `eval_type` is `ai_judge`, the judge prompt template is rendered with three placeholders before being sent to the judge model:

- `{input}` — the input fields from the test case row
- `{output}` — the LLM's raw response
- `{expected}` — the expected output fields from the test case row

The judge model is expected to return a JSON object. Any LLM configured in the tool can be used as the judge, including the same model being evaluated or a separate, dedicated judge model.

---

### 5. Rate limiting (`core/services/rate_limiter.py`)

Each `ModelConfig` has a `rate_limit_rpm` field (requests per minute, default: 60). The `RateLimiter` class uses a sliding-window algorithm to enforce this limit across all rows in a test run.

To change the default for new model configs, update the field default in `core/models.py`:

```python
rate_limit_rpm = models.PositiveIntegerField(default=60, ...)
```

For providers with tiered rate limits (e.g. different limits for different tiers or deployments), set the value per `ModelConfig` in the UI when creating or editing the model.

---

### 6. Celery worker concurrency and DB connection budget

The worker runs `celery -A config worker -l info` by default, with one process per CPU core.

To increase throughput, set an explicit concurrency level on the worker deployment:

```bash
celery -A config worker -l info --concurrency=4
```

Multiple worker replicas can be run safely — Celery handles task distribution across them. The Gunicorn timeout (default: 120 seconds) is configured in `entrypoint.sh`.

**Postgres connection budget:** each Celery process that runs a test/eval task holds at least one DB connection on the main task thread. LLM worker threads no longer open ORM connections (cancel is via an in-memory event). Still size carefully:

```
approx peak ≈ gunicorn_workers
            + (celery_processes × concurrent_tasks × ~1 main-thread conn)
            + short-lived request connections
```

Raise Celery `--concurrency` / replicas only when Postgres `max_connections` (or a pooler such as pgBouncer) can absorb that. Per-model pool size is also capped by `MAX_MODEL_CONCURRENCY` (default 16, overridable via env). Set `DB_APPLICATION_NAME` differently on web vs worker deployments to inspect `pg_stat_activity`.

Evaluation runs (keyword, AI judge, field match, Python) are Celery tasks like test runs. The Redis-down thread fallback inside Gunicorn is emergency-only.

---

### 7. Authentication (`config/settings.py`)

The application uses Django's built-in username/password authentication. Users register at `/accounts/register/` and log in at `/accounts/login/`.

For enterprise deployments, Django supports LDAP/Active Directory integration via `django-auth-ldap`, and Microsoft/Azure AD via `django-microsoft-auth` or `mozilla-django-oidc`. These can be added to `INSTALLED_APPS` and configured with minimal changes to the rest of the application.

---

## Dependency Summary

| Package | Version | Purpose |
|---------|---------|---------|
| `Django` | 5.2.x | Web framework |
| `celery` | 5.6.x | Async task queue |
| `redis` | 6.4.x | Celery broker client |
| `httpx` | 0.28.x | HTTP client for LLM API calls |
| `django-htmx` | 1.27.x | Partial page updates without full JS framework |
| `django-encrypted-model-fields` | 0.6.x | Fernet encryption for API keys at rest |
| `gunicorn` | 25.1.x | WSGI server |
| `whitenoise` | 6.9.x | Static file serving without a separate web server |
| `openpyxl` | 3.1.x | Excel file parsing |
| `pandas` | 3.0.x | CSV/Excel ingestion and data handling |

> Note: `requirements.txt` currently contains several unrelated packages (e.g. `esptool`, `Flask`, `huggingface_hub`) as artefacts of an exported environment. These can be removed to reduce the image size and build time.

---

## File Reference

```
config/
  settings.py          # All Django settings: DB, Celery, encryption, static files
  celery.py            # Celery app initialisation
  urls.py              # Root URL routing

core/
  models.py            # All database models and enums
  tasks.py             # Celery task: execute_test_run()
  forms.py             # Django forms
  admin.py             # Django admin registrations

  services/
    csv_parser.py      # CSV/Excel ingestion — column prefix convention here
    prompt_builder.py  # Template placeholder substitution
    llm_client.py      # Multi-provider LLM API client
    scorer.py          # Keyword/field-match scoring logic
    rate_limiter.py    # Per-model RPM throttling

  views/
    auth.py            # Registration
    dashboard.py       # Home page
    cases.py           # Test case CRUD and CSV upload
    prompt_templates.py
    model_configs.py   # LLM endpoint management
    runs.py            # Test run creation, monitoring, deletion
    evaluations.py     # Evaluation configs, runs, human review

  templates/core/      # Django HTML templates (HTMX-powered)
  static/core/         # Logo and favicon assets

Dockerfile             # Container image (OpenShift-compatible)
entrypoint.sh          # Container startup: migrate then start Gunicorn
requirements.txt       # Python dependencies
Makefile               # Developer shortcuts
```
