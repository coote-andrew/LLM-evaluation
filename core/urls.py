"""Core app URL configuration."""

from django.urls import path

from core.views.dashboard import DashboardView
from core.views.cases import (
    TestCaseCreateView,
    TestCaseDetailView,
    TestCaseListView,
    upload_csv_view,
)
from core.views.prompt_templates import PromptTemplateCreateView, PromptTemplateUpdateView
from core.views.model_configs import (
    ModelConfigCreateView,
    ModelConfigListView,
    ModelConfigUpdateView,
)
from core.views.runs import TestRunCreateView, TestRunDetailView, TestRunListView

app_name = "core"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("test-cases/", TestCaseListView.as_view(), name="testcase_list"),
    path("test-cases/create/", TestCaseCreateView.as_view(), name="testcase_create"),
    path("test-cases/upload/", upload_csv_view, name="testcase_upload"),
    path("test-cases/<uuid:pk>/", TestCaseDetailView.as_view(), name="testcase_detail"),
    path(
        "test-cases/<uuid:test_case_id>/prompts/create/",
        PromptTemplateCreateView.as_view(),
        name="prompttemplate_create",
    ),
    path(
        "prompts/<uuid:pk>/edit/",
        PromptTemplateUpdateView.as_view(),
        name="prompttemplate_edit",
    ),
    path("models/", ModelConfigListView.as_view(), name="modelconfig_list"),
    path("models/create/", ModelConfigCreateView.as_view(), name="modelconfig_create"),
    path("models/<uuid:pk>/edit/", ModelConfigUpdateView.as_view(), name="modelconfig_edit"),
    path("runs/", TestRunListView.as_view(), name="testrun_list"),
    path("runs/create/", TestRunCreateView.as_view(), name="testrun_create"),
    path("runs/<uuid:pk>/", TestRunDetailView.as_view(), name="testrun_detail"),
]
