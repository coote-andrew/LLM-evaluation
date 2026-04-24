"""Core app URL configuration."""

from django.urls import path

from core.views.auth import RegisterView
from core.views.dashboard import DashboardView
from core.views.cases import (
    TestCaseCreateView,
    TestCaseDeleteView,
    TestCaseDetailView,
    TestCaseListView,
    upload_csv_view,
)
from core.views.prompt_templates import (
    PromptTemplateCreateView,
    PromptTemplateDeleteView,
    PromptTemplateUpdateView,
)
from core.views.model_configs import (
    ModelConfigCreateView,
    ModelConfigListView,
    ModelConfigUpdateView,
)
from core.views.runs import (
    CancelTestRunView,
    TestRunCreateView,
    TestRunDeleteView,
    TestRunDetailView,
    TestRunListView,
    TestRunResultsPartialView,
    TestRunStatusView,
)
from core.views.agents import (
    AgentAssetDetailView,
    AgentRegistryActionView,
    AgentRegistryView,
    AgentVersionDiffView,
    AgentVersionSourceView,
)
from core.views.exports import ExportEvaluationRunView, ExportTestRunView
from core.views.evaluations import (
    EvaluationConfigCreateView,
    EvaluationConfigDeleteView,
    EvaluationConfigUpdateView,
    EvaluationRunCreateView,
    EvaluationRunDeleteView,
    EvaluationRunDetailView,
    EvaluationRunListView,
    HumanReviewView,
)

app_name = "core"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("accounts/register/", RegisterView.as_view(), name="register"),
    path("test-cases/", TestCaseListView.as_view(), name="testcase_list"),
    path("test-cases/create/", TestCaseCreateView.as_view(), name="testcase_create"),
    path("test-cases/upload/", upload_csv_view, name="testcase_upload"),
    path("test-cases/<uuid:pk>/", TestCaseDetailView.as_view(), name="testcase_detail"),
    path("test-cases/<uuid:pk>/delete/", TestCaseDeleteView.as_view(), name="testcase_delete"),
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
    path(
        "prompts/<uuid:pk>/delete/",
        PromptTemplateDeleteView.as_view(),
        name="prompttemplate_delete",
    ),
    path(
        "test-cases/<uuid:test_case_id>/eval-configs/create/",
        EvaluationConfigCreateView.as_view(),
        name="evaluationconfig_create",
    ),
    path(
        "eval-configs/<uuid:pk>/edit/",
        EvaluationConfigUpdateView.as_view(),
        name="evaluationconfig_edit",
    ),
    path(
        "eval-configs/<uuid:pk>/delete/",
        EvaluationConfigDeleteView.as_view(),
        name="evaluationconfig_delete",
    ),
    path("models/", ModelConfigListView.as_view(), name="modelconfig_list"),
    path("models/create/", ModelConfigCreateView.as_view(), name="modelconfig_create"),
    path("models/<uuid:pk>/edit/", ModelConfigUpdateView.as_view(), name="modelconfig_edit"),
    path("runs/", TestRunListView.as_view(), name="testrun_list"),
    path("runs/create/", TestRunCreateView.as_view(), name="testrun_create"),
    path("runs/<uuid:pk>/", TestRunDetailView.as_view(), name="testrun_detail"),
    path("runs/<uuid:pk>/delete/", TestRunDeleteView.as_view(), name="testrun_delete"),
    path("runs/<uuid:pk>/cancel/", CancelTestRunView.as_view(), name="testrun_cancel"),
    path("runs/<uuid:pk>/status/", TestRunStatusView.as_view(), name="testrun_status"),
    path("runs/<uuid:pk>/results-partial/", TestRunResultsPartialView.as_view(), name="testrun_results_partial"),
    path("runs/<uuid:pk>/export/", ExportTestRunView.as_view(), name="testrun_export"),
    path("runs/<uuid:test_run_id>/evaluate/", EvaluationRunCreateView.as_view(), name="evaluationrun_create"),
    path("evaluations/", EvaluationRunListView.as_view(), name="evaluationrun_list"),
    path("evaluations/<uuid:pk>/", EvaluationRunDetailView.as_view(), name="evaluationrun_detail"),
    path("evaluations/<uuid:pk>/delete/", EvaluationRunDeleteView.as_view(), name="evaluationrun_delete"),
    path("evaluations/<uuid:pk>/export/", ExportEvaluationRunView.as_view(), name="evaluationrun_export"),
    path("evaluations/<uuid:eval_run_id>/review/", HumanReviewView.as_view(), name="human_review"),
    path("agents/", AgentRegistryView.as_view(), name="agent_registry"),
    path(
        "agents/actions/",
        AgentRegistryActionView.as_view(),
        name="agent_registry_actions",
    ),
    path(
        "agents/<str:kind>/<str:name>/",
        AgentAssetDetailView.as_view(),
        name="agent_asset_detail",
    ),
    path(
        "agents/<str:kind>/<str:name>/versions/<str:label>/source/",
        AgentVersionSourceView.as_view(),
        name="agent_version_source",
    ),
    path(
        "agents/<str:kind>/<str:name>/diff/",
        AgentVersionDiffView.as_view(),
        name="agent_version_diff",
    ),
]
