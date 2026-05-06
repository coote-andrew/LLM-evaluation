# Pagination for Run Outputs — Implementation Record

## What was built

Server-side pagination for `TestRunDetailView` and `EvaluationRunDetailView`. Default page size is 50 rows, switchable to 100 via a page-size chooser. Export views are unchanged and always stream all rows.

Live updates during an active run use HTMX partial swaps on the **tail page only** (the last, still-filling page). Sealed pages (any page that is full) are static HTML — no polling, no re-render. A separate lightweight stats poll (status badge, progress counter) continues to run on all pages via existing JS.

---

## Concepts

### Sealed page vs. tail page

A page is **sealed** when it is full (contains `page_size` rows). Earlier pages become sealed as the run progresses. The tail page is the last (and only incomplete) page.

| Page | During active run | After completion |
|------|-----------------|-----------------|
| Pages 1 … N-1 | Sealed — table content is static HTML | Static |
| Page N (tail) | HTMX polls `results-partial/` every 10 s | Static (sealed when run ends) |

### What refreshes during an active run

| Element | Mechanism | Frequency |
|---------|-----------|-----------|
| Tail page results table + pagination bar | HTMX `hx-trigger="every 10s"` on `#results-section` | 10 s |
| Status badge + progress counter | Existing JS `fetch` to `/runs/<pk>/status/` | 10 s |
| Sealed page table content | Nothing — static | — |

When the tail page fills to `page_size`, the next HTMX poll returns a full table with a "Next →" link. The user navigates forward manually — no auto-jump.

---

## Files changed

### `core/views/runs.py`

Added at the top:

```python
from django.core.paginator import Paginator

RESULTS_PAGE_SIZE_DEFAULT = 50
RESULTS_PAGE_SIZE_MAX = 100

def _parse_page_size(request, default=RESULTS_PAGE_SIZE_DEFAULT):
    try:
        size = int(request.GET.get("page_size", default))
    except (TypeError, ValueError):
        size = default
    return min(max(size, 10), RESULTS_PAGE_SIZE_MAX)
```

**`TestRunDetailView.get_queryset`** — removed `"results__test_case_row"` from `prefetch_related` (paginated queryset handles its own join):

```python
def get_queryset(self):
    return TestRun.objects.select_related(
        "prompt_template",
        "model_config",
        "test_case_version__test_case",
    ).prefetch_related(
        "evaluation_runs__evaluation_config",
        "evaluation_runs__results",
    )
```

**`TestRunDetailView.get_context_data`** — always paginates; flags whether current page is the tail:

```python
def get_context_data(self, **kwargs):
    from core.views.evaluations import compute_accuracy
    ctx = super().get_context_data(**kwargs)
    ctx["eval_runs_with_accuracy"] = [
        (er, compute_accuracy(er))
        for er in self.object.evaluation_runs.all()
    ]

    page_size = _parse_page_size(self.request)
    results_qs = (
        self.object.results
        .select_related("test_case_row")
        .order_by("test_case_row__row_number")
    )
    paginator = Paginator(results_qs, page_size)
    page_obj = paginator.get_page(self.request.GET.get("page", 1))

    ctx["page_obj"] = page_obj
    ctx["page_results"] = page_obj.object_list
    ctx["is_paginated"] = paginator.num_pages > 1
    ctx["page_size"] = page_size
    ctx["is_tail_page"] = (
        self.object.status in ("pending", "running")
        and page_obj.number == paginator.num_pages
    )
    return ctx
```

**`TestRunResultsPartialView`** — now accepts `?page=` and `?page_size=` and returns a full results fragment (table + pagination bar) rather than bare `<tr>` rows:

```python
class TestRunResultsPartialView(LoginRequiredMixin, View):
    def get(self, request, pk):
        run = get_object_or_404(
            TestRun.objects.select_related(
                "prompt_template", "model_config", "test_case_version__test_case"
            ),
            pk=pk,
        )
        page_size = _parse_page_size(request)
        results_qs = (
            run.results
            .select_related("test_case_row")
            .order_by("test_case_row__row_number")
        )
        paginator = Paginator(results_qs, page_size)
        page_obj = paginator.get_page(request.GET.get("page", 1))

        return render(request, "core/testrun_results_partial.html", {
            "test_run": run,
            "page_obj": page_obj,
            "page_results": page_obj.object_list,
            "is_paginated": paginator.num_pages > 1,
            "page_size": page_size,
            "is_tail_page": (
                run.status in ("pending", "running")
                and page_obj.number == paginator.num_pages
            ),
        })
```

---

### `core/views/evaluations.py`

Added `from django.core.paginator import Paginator`.

**`EvaluationRunDetailView.get_queryset`** — removed `prefetch_related("results__test_run_result__test_case_row")`:

```python
def get_queryset(self):
    return EvaluationRun.objects.select_related(
        "evaluation_config",
        "test_run__test_case_version__test_case",
        "test_run__prompt_template",
        "test_run__model_config",
    )
```

**`EvaluationRunDetailView.get_context_data`** — added pagination (evaluation runs have no live-update requirement):

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    ctx["accuracy"] = compute_accuracy(self.object)
    ctx["sens_spec"] = compute_sens_spec(self.object)
    ctx["config_description"] = describe_config(self.object.evaluation_config)
    version = self.object.test_run.test_case_version
    ctx["output_columns"] = version.output_columns or []

    try:
        page_size = int(self.request.GET.get("page_size", 50))
    except (TypeError, ValueError):
        page_size = 50
    page_size = min(max(page_size, 10), 100)

    results_qs = (
        self.object.results
        .select_related("test_run_result__test_case_row")
        .order_by("test_run_result__test_case_row__row_number")
    )
    paginator = Paginator(results_qs, page_size)
    page_obj = paginator.get_page(self.request.GET.get("page", 1))

    ctx["page_obj"] = page_obj
    ctx["page_results"] = page_obj.object_list
    ctx["is_paginated"] = paginator.num_pages > 1
    ctx["page_size"] = page_size
    return ctx
```

> `compute_accuracy` and `compute_sens_spec` both call `eval_run.results.all()` internally and continue to operate on the full result set.

---

### `core/templates/core/_pagination_bar.html` *(new)*

Shared snippet included by both detail templates and the results partial. Expects `page_obj`, `is_paginated`, and `page_size` in context.

Shows Prev/Next links, "Page X of Y · N rows total", and 50/100 page-size chooser buttons. When there is only one page it still shows the row count and size chooser.

---

### `core/templates/core/testrun_results_partial.html`

Rewritten from bare `<tr>` fragments to a full results section fragment (table + `_pagination_bar.html` include). This is what HTMX swaps into `#results-section` on the tail page.

---

### `core/templates/core/testrun_detail.html`

The results section is now wrapped in `<div id="results-section">`. On the tail page of an active run this div carries HTMX polling attributes:

```html
<div id="results-section"
     {% if is_tail_page %}
     hx-get="{% url 'core:testrun_results_partial' pk=test_run.pk %}?page={{ page_obj.number }}&page_size={{ page_size }}"
     hx-trigger="every 10s"
     hx-swap="innerHTML"
     hx-on::after-swap="document.querySelectorAll('#results-section .fmt-response').forEach(formatResponse)"
     {% endif %}>
    {% include "core/testrun_results_partial.html" %}
</div>
```

On sealed pages (or after completion) the div has no HTMX attributes — it is plain static HTML.

The results section now renders via `{% include "core/testrun_results_partial.html" %}` on first load, keeping the table markup in one place.

**JS polling block** — the old `setInterval` loop that fetched the partial and spliced rows into the tbody has been simplified. It now only polls `/runs/<pk>/status/` to update the badge, progress counter, and failure count, and triggers `location.reload()` on terminal status. The tbody refresh responsibility moved entirely to HTMX.

---

### `core/templates/core/evaluationrun_detail.html`

- Loop changed from `{% for er in eval_run.results.all %}` to `{% for er in page_results %}`
- `{% include "core/_pagination_bar.html" %}` added after `</table>`
- No HTMX attributes (evaluation runs complete before the page is visited)

---

### `core/tests.py`

Added `EvalRunStatus` and `ResultStatus` to imports. Added three test classes at the end of the file:

**`TestRunDetailPaginationTests`** (8 tests)
- Default page size is 50
- Page 2 returns correct rows
- Page 3 returns the 20-row remainder of a 120-row run
- `page_size=100` works
- `page_size=9999` is clamped to 100
- `is_tail_page` is `False` for a completed run
- `is_tail_page` is `True` on the last page of an active run
- `is_tail_page` is `False` on a sealed page of an active run

**`TestRunResultsPartialPaginationTests`** (4 tests)
- Partial defaults to page 1
- Page 2 returns the remainder
- `is_tail_page` is `True` on the last page of a running run
- `is_tail_page` is `False` on a sealed page

**`EvaluationRunDetailPaginationTests`** (3 tests)
- Default page size is 50
- Page 2 returns the 25-row remainder of a 75-row eval
- `page_size=100` shows all 75 rows on one page with `is_paginated=False`

---

## What was not changed

| Area | Status |
|------|--------|
| `ExportTestRunView` | Unchanged — always exports all rows |
| `ExportEvaluationRunView` | Unchanged — always exports all rows |
| `TestRunStatusView` (JSON) | Unchanged — still polled by the stats JS |
| `HumanReviewView` | Unchanged |
| URL patterns | No new URLs |
| Models / migrations | No model changes |
