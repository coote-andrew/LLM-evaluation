# Django walkthrough: one Cicada request end to end

Use the “Create Run” feature as the main example. It includes nearly every Django idea worth introducing in one vertical slice: routing, authentication, forms, ORM, redirects, background work, templates, and progressive updates.

## The request path

```text
Browser POST /runs/create/
  → Django middleware (sessions, CSRF, authentication, Cicada password policy, HTMX)
  → config.urls includes core.urls
  → TestRunCreateView
  → TestRunCreateForm validation and visibility-scoped choices
  → TestRun.objects.create(...)
  → dispatch_task(execute_test_run, run_id)
  → redirect to /runs/<uuid>/
  → TestRunDetailView renders a Django template
  → browser polls a JSON route and swaps an HTMX result fragment while running
```

This is deliberately different from a notebook workflow. A notebook tends to retain process state. A web request should be short, authenticated, independently repeatable, and safe to run concurrently with other users.

## 1. Project configuration establishes conventions

Start in `config/settings.py`.

- `INSTALLED_APPS` enables built-in Django subsystems and Cicada’s `core` app.
- `MIDDLEWARE` is an ordered request/response pipeline. Session and authentication middleware make `request.user` available to views; CSRF middleware protects browser form posts; `django_htmx` identifies HTMX requests.
- `TEMPLATES` selects `django.template.backends.django.DjangoTemplates`, searches `core/templates`, and adds useful variables through context processors.
- Database settings define Django’s ORM connection; Redis and Celery settings define background job infrastructure.
- `ENTRA_OIDC_ENABLED` conditionally adds SSO configuration. The feature is operational configuration, not an alternative user model.

Useful presenter line: “Settings are mostly wiring. They tell Django which capabilities exist and where to find infrastructure; they should contain very little domain logic.”

## 2. URL configuration maps HTTP to Python

`config/urls.py` is the root router. It owns admin and account routes, then delegates the application routes:

```python
path("", include("core.urls"))
```

`core/urls.py` has an `app_name = "core"` namespace. The run creation route is:

```python
path("runs/create/", TestRunCreateView.as_view(), name="testrun_create")
```

The URL name matters as much as the literal path. Production templates use `{% url 'core:testrun_create' %}` so internal links do not need to duplicate `"/runs/create/"`.

## 3. A class-based view handles GET and POST

`core/views/runs.py` defines `TestRunCreateView(LoginRequiredMixin, FormView)`.

- `LoginRequiredMixin` turns an unauthenticated request into a redirect to the configured login page.
- `FormView` provides the conventional lifecycle: build a form for `GET`, bind and validate it for `POST`, then call `form_valid` when valid.
- `get_form_kwargs` passes `request.user` into the form. That lets the form limit choices to data the current user can access.
- `get_context_data` adds presentation-specific data such as prompt groups, PHI flags, row counts, and cost metadata.
- `form_valid` is the successful write path. It enforces that a prompt and data version belong to the same project, creates the `TestRun`, queues work, adds a message, and redirects.

This division is intentional:

| Responsibility | Location |
| --- | --- |
| Match URL to code | `core/urls.py` |
| HTTP lifecycle and permission boundary | view |
| Field parsing and choice validation | form |
| Persisted entity relationships | model |
| Long-running LLM execution | task/service |
| HTML presentation | template |

## 4. Models are typed database relationships

`core/models.py` is Cicada’s domain vocabulary.

```text
Project (persisted as TestCase)
  ├── TestCaseVersion ── TestCaseRow
  ├── PromptTemplate
  └── EvaluationConfig

TestRun
  ├── points to one dataset version
  ├── points to one prompt version
  ├── points to one model config
  └── TestRunResult per input row
```

Key distinctions:

- `ForeignKey` represents many-to-one relationships. Django exposes the reverse relation through `related_name`, e.g. a run’s `results`.
- `JSONField` is useful for flexible row payloads and evaluation criteria. It does not replace explicit columns when the application needs database constraints or frequent relational queries.
- UUID primary keys work well for externally visible object IDs.
- A model is not a data frame. It describes persistent records and relations; querysets retrieve only the required rows and can be evaluated lazily.

## 5. The form is where user input becomes safe Python data

`core/forms.py` contains `TestRunCreateForm`. Django forms:

1. render named controls and errors in a template;
2. turn form-encoded browser strings into Python values;
3. validate field and cross-field rules; and
4. expose validated values in `cleaned_data`.

The view then uses `form.cleaned_data["test_case_version"]`, not raw `request.POST` strings. This is an important contrast with writing a quick Flask-style endpoint: the form centralises display, parsing, validation, and constrained choice querysets.

## 6. The response follows POST-Redirect-GET

After it creates a run, `TestRunCreateView.form_valid` returns:

```python
return redirect("core:testrun_detail", pk=run.pk)
```

This is the POST-Redirect-GET pattern. Reloading the detail page cannot accidentally submit the create form a second time. The success message survives the redirect through Django’s messages framework.

`TestRunDetailView` fetches the run with `select_related` and `prefetch_related`, calculates small display values, paginates result rows, and supplies a context dictionary. It does not execute the LLM.

## 7. Background tasks keep requests short

`dispatch_task(execute_test_run, str(run.id))` separates request creation from execution.

`core/tasks.py` reads the run and its rows, builds a prompt using `core/services/prompt_builder.py`, calls the configured LLM client, and writes a `TestRunResult` for each row. In production this is dispatched to a Celery worker through Redis.

Useful presenter line: “The run row is the contract between the web app and the worker. The browser gets an acknowledgement quickly; the worker can continue after that request has ended.”

## 8. Django templates are server-rendered HTML

`core/templates/core/testrun_create.html` begins with:

```django
{% extends "base.html" %}
{% block content %}
...
{% endblock %}
```

The template receives the view’s context, such as `form`, `source_run`, and `prompt_template_groups`. It uses:

- `{{ value }}` to render an escaped value;
- `{% if condition %}` for conditional HTML;
- `{% for item in items %}` for repetition;
- `{% url 'core:name' %}` to construct an internal path by name;
- `{% csrf_token %}` in state-changing browser forms; and
- `{% include "path.html" %}` to reuse a fragment.

### Important: DTL vs Jinja2

The syntax will look familiar if you have seen Jinja, but Cicada’s page templates are **Django Template Language (DTL)**. Do not describe them as Jinja templates. Jinja2 is installed as a Python dependency, but Cicada does not configure Django’s Jinja2 template backend.

The biggest practical lesson is not syntax trivia: templates should display data and simple presentation conditions, while views, forms, services, and tasks own behavior.

## 9. Interactivity uses ordinary endpoints

The run detail page illustrates two lightweight update patterns.

- `TestRunStatusView` returns JSON for progress fields. The page’s small `fetch` loop updates the badge and counters.
- `TestRunResultsPartialView` returns an HTML fragment from `core/testrun_results_partial.html`. HTMX swaps that fragment into the page every ten seconds while a run is active.

The user still receives server-rendered HTML, URL routing, authentication, and permission checks. HTMX is an incremental enhancement, not a separate front-end application.

## 10. Evaluation is a second asynchronous workflow

`EvaluationRunCreateView` in `core/views/evaluations.py` selects an `EvaluationConfig`, creates an `EvaluationRun`, and dispatches the appropriate task for keyword, field-match, Python, or AI-judge evaluation. Human review routes directly to an authenticated reviewer flow.

The model graph preserves provenance:

```text
TestRunResult
  + EvaluationConfig revision
  → EvaluationRun
  → EvaluationResult
```

That is why results include the exact judge prompt and raw judge response where applicable. Metrics are useful only when their inputs and scoring definitions are inspectable.

## Suggested live code stops

| Product screen | Show this code | Explain |
| --- | --- | --- |
| Login / SSO | `config/urls.py`, `config/settings.py` | authentication routing and configuration gates |
| Project data | `core/models.py`, `core/services/csv_parser.py` | versioned tabular data and ORM relationships |
| New run | `core/urls.py`, `core/views/runs.py`, `core/forms.py` | route → view → form → model → redirect |
| Running detail | `core/tasks.py`, `core/templates/core/testrun_detail.html` | background execution and polling/HTMX |
| Evaluation | `core/views/evaluations.py`, `core/services/scorer.py` | evaluation configurations and dispatch |

## Avoid these beginner traps

- Do not edit a migration by hand when changing a model; create it with Django’s migration tools.
- Do not call a slow LLM from a request-response view when the browser can time out; create a durable run and dispatch background work.
- Do not trust an object ID submitted by the browser. Filter querysets through Cicada’s access helpers.
- Do not put complex Python work in templates. If a presentation calculation grows beyond a tiny formatting expression, compute it in Python.
- Do not confuse `{input_note}` prompt substitution with `{{ test_run... }}` Django template rendering. They occur at different stages for different audiences.
