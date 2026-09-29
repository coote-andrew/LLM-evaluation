"""Illustrative views only — not imported by Cicada.

A view is the HTTP boundary: it receives a request, enforces access rules,
coordinates models/forms/services, and returns an HTTP response. This is a
small teaching version of the production Cicada views in core/views/.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import DetailView, View

from .models import Project


class ProjectDetailView(LoginRequiredMixin, DetailView):
    """GET /projects/<uuid>/ → query one Project → render an HTML response.

    Class-based views are reusable behaviour plus configuration:
    DetailView supplies the generic “look up one object and render it” flow.
    This class supplies the model, template, and context-variable names.
    """

    # LoginRequiredMixin runs before DetailView. An anonymous request is
    # redirected to LOGIN_URL; an authenticated request continues to DetailView.
    model = Project

    # Django loads this template after it has found the object.
    template_name = "training_example/project_detail.html"

    # This is the key exposed to the template: {{ project.name }}.
    # Without it, DetailView's default context name would be `object`.
    context_object_name = "project"

    def get_queryset(self):
        """Restrict lookup before DetailView applies pk=<value> from the URL."""
        # Login alone is not authorisation. In production, use Cicada's
        # visible_projects(request.user), which also supports sharing rules.
        return Project.objects.filter(created_by=self.request.user)


class StartRunView(LoginRequiredMixin, View):
    """POST /projects/<uuid>/runs/ → create a durable run → redirect.

    View is Django's minimal class-based view. It dispatches by HTTP method:
    a POST request calls this post() method; a GET request would need a get()
    method (or Django returns “method not allowed”).
    """

    def post(self, request, project_id):
        # get_object_or_404 returns the matching object or an HTTP 404 response.
        # Include an access filter so a logged-in user cannot start runs for an
        # arbitrary UUID they discovered.
        project = get_object_or_404(
            Project, pk=project_id, created_by=request.user
        )

        # A real Cicada view would first use a Form to parse and validate
        # submitted settings, then create TestRun with those clean values.
        # run = TestRun.objects.create(project=project, created_by=request.user)
        #
        # LLM calls are slow and unreliable enough to need a background worker.
        # The request creates the run record, then hands only its ID to a task.
        # dispatch_task(execute_test_run, run.id)

        # POST-Redirect-GET avoids duplicate form submission when the user
        # refreshes their browser. `redirect` resolves this named URL via urls.py.
        return redirect("training_example:project_detail", pk=project.pk)
