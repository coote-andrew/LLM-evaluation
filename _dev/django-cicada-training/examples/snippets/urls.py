"""Illustrative URL configuration only — not imported by Cicada.

The production Cicada router is core/urls.py. A URL configuration is Django's
lookup table from an HTTP path to a view callable.
"""

from django.urls import path

from .views import ProjectDetailView, StartRunView

# The namespace prevents route-name collisions with other Django apps.
# Templates can refer to "training_example:project_detail" unambiguously.
app_name = "training_example"

urlpatterns = [
    # <uuid:pk> parses a UUID path segment and passes it to the view as the
    # keyword argument `pk`. DetailView recognises `pk` automatically.
    # Example: /projects/5e30.../
    path("projects/<uuid:pk>/", ProjectDetailView.as_view(), name="project_detail"),

    # .as_view() turns the class into a callable Django can invoke per request.
    # The named parameter is deliberately project_id because StartRunView.post()
    # declares that parameter. The route name is used by {% url %} in templates.
    path("projects/<uuid:project_id>/runs/", StartRunView.as_view(), name="start_run"),
]
