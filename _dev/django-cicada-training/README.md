# Cicada and Django: 30-minute recording guide

Audience: Python-fluent data scientists who are new to web applications and Django.

This is a presenter pack, not product documentation. Use the production Cicada instance for the live tour and these local files to pause on concepts and code.

## Before you record

- Sign in to production and confirm the SSO button appears if it is enabled there.
- Prepare one project with illustrative (not sensitive) data, an uploaded version, a prompt, and an available model.
- Run a small sample before recording so that a completed run and at least one evaluation result exist.
- Have one structured JSON response and one evaluation configuration available. Keep the exact row values and clinical example to your own prepared data.
- Open this folder locally. The HTML pages in `examples/` work without a server or network connection.

## Timing and click-through script

### 0:00–3:00 — What Cicada is, and the web-app mental model

Click: home page → sign in → Dashboard.

- “Cicada is an evaluation workbench: structured examples go in, an LLM or agent generates an output, and an evaluation makes the comparison reproducible.”
- “Django is the Python web framework behind the interface. The browser sends an HTTP request; Django chooses a view; the view reads or writes database records; then a template renders HTML.”
- “The SSO step is an authentication boundary, not a special kind of Cicada data. The production integration is optional Entra OpenID Connect; normal Django users and sessions still exist.”
- “For a data-science analogy: a Project is the experiment container, a Project Version is an immutable-ish dataset snapshot, a Run is a batch inference job, and an Evaluation is a scoring job.”

Code pause: `config/settings.py` for installed apps, middleware, database, Celery, and the `DjangoTemplates` setting; `config/urls.py` for login and Entra routes.

### 3:00–8:00 — Projects and structured inputs

Click: Projects → your illustrative project → data/version details.

- “The interface says Project, while the persisted Django model is still named `TestCase` for migration compatibility. `Project = TestCase` is an alias, not a second table.”
- “A spreadsheet upload becomes a `TestCaseVersion` and one `TestCaseRow` per row. This preserves exactly which inputs and expected outputs were used.”
- “Columns prefixed `input_` are prompt inputs; `output_` values are expected values for evaluation; `file_` fields describe attachments.”
- “A new upload is a new version. That makes a run interpretable later: it points to a particular dataset version, rather than a mutable file.”

Code pause: `core/models.py` for `TestCase`, `TestCaseVersion`, and `TestCaseRow`; `core/services/csv_parser.py` for upload parsing.

### 8:00–12:00 — Prompt templates and model configurations

Click: add/open a prompt → Models → your configured model.

- “Prompt templates are reusable, versioned text. Their `{input_column}` placeholders are Cicada’s own prompt substitution convention, not Django template syntax.”
- “Choose free text when the response is narrative; choose JSON when the downstream evaluator needs named fields.”
- “A model configuration describes how to reach a model: provider, endpoint, credentials, rate limit, timeout, and governance constraints. The API key fields are encrypted at rest.”
- “Keep a prompt, a model, and a dataset separate. That is what lets us vary one dimension and compare runs.”

Code pause: `PromptTemplate` and `ModelConfig` in `core/models.py`; `core/services/prompt_builder.py`; `core/services/llm_client.py`.

### 12:00–18:00 — Create and inspect a run

Click: Runs → Create Run → select project version, prompt, and model → completed run detail.

- “The create screen is a normal Django form. On submit Django validates that the prompt and dataset belong to the same project, writes a `TestRun`, then redirects to its detail page.”
- “The slow LLM work does not run in the browser request. `dispatch_task` hands it to Celery in production, or a development fallback where appropriate.”
- “The detail page lets us inspect row-level provenance: the prompt sent, raw response, parsed JSON, latency, and token counts.”
- “The page refreshes progress with a small JSON endpoint and an HTMX HTML partial. Django can be interactive without becoming a large single-page JavaScript app.”

Code pause: `core/urls.py` → `TestRunCreateView` in `core/views/runs.py` → `TestRunCreateForm` in `core/forms.py` → `core/tasks.py`.

### 18:00–26:00 — Evaluation

Click: completed run → New evaluation → evaluation detail.

- “An evaluation configuration specifies how an output is judged. It is separate from the run so the same model output can be scored in more than one way.”
- “Field match compares structured JSON with expected output fields. This is the most direct option when the schema is stable.”
- “Python evaluation runs a controlled script and expects a result dictionary. It is appropriate for deterministic domain logic that is clearer in Python than in declarative checks.”
- “An LLM judge uses another configured model and a judge prompt. Treat it as a modelled assessment: retain the prompt and raw response so it can be audited.”
- “Human review creates a gold-standard assessment row by row. Accuracy and sensitivity/specificity only mean what the selected ground truth and configuration define.”

Code pause: `EvalType`, `EvaluationConfig`, and `EvaluationResult` in `core/models.py`; `EvaluationRunCreateView` in `core/views/evaluations.py`; `core/services/scorer.py`.

### 26:00–30:00 — Follow one request through the code

Open: `code-walkthrough.md`, then `examples/01-request-flow.html`.

- “Start from a URL, not from a random Python file. Django’s URL configuration is its table of contents.”
- “The view is the HTTP boundary. It checks access, shapes a queryset and context, and delegates domain work to forms, services, or tasks.”
- “Models map Python objects and relationships to database rows. Templates receive only the context they need to display.”
- “Read one complete vertical slice—create run—before browsing every module. That makes the rest of the codebase easier to classify.”

## Core code-reading order

1. `manage.py` — command-line entry point and settings module.
2. `config/settings.py` — application wiring, template backend, middleware, infrastructure.
3. `config/urls.py`, then `core/urls.py` — route table.
4. `core/views/home.py` and `core/templates/core/home.html` — smallest read-only request.
5. `core/models.py` — the domain graph.
6. `core/views/runs.py` → `core/forms.py` → `core/tasks.py` — full write and async flow.
7. `core/templates/core/testrun_detail.html` and `core/templates/core/testrun_results_partial.html` — rendering and live updates.
8. `core/views/evaluations.py` and `core/services/scorer.py` — evaluation orchestration and scoring.

## Template terminology: Django Templates, not Jinja

Cicada is configured with `django.template.backends.django.DjangoTemplates` in `config/settings.py`. It does **not** configure the Jinja2 Django backend. Django Template Language and Jinja look similar because both use `{{ value }}`, `{% if %}`, and `{% for %}`, but they are separate engines with different tags, filters, configuration, and extension mechanisms.

For this recording, call Cicada files “Django templates” or “Django Template Language (DTL) templates.” See `examples/02-template-anatomy.html` for a compact, visual explanation.

## Files in this pack

- `code-walkthrough.md` — the real Cicada request flow and code anchors.
- `examples/01-request-flow.html` — clickable browser-to-Django pipeline.
- `examples/02-template-anatomy.html` — rendered DTL concepts beside source.
- `examples/03-evaluation-results.html` — an illustrative results display and evaluation vocabulary.
- `examples/snippets/` — small companion source files; illustrative only, not production code.
