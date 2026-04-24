"""Django admin for evaluation workbench models."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from core.models import (
    TestCase,
    TestCaseRow,
    TestCaseVersion,
    PromptTemplate,
    ModelConfig,
    TestRun,
    TestRunResult,
    EvaluationConfig,
    EvaluationRun,
    EvaluationResult,
    AgentAsset,
    AgentAssetVersion,
)


@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at", "created_by"]
    search_fields = ["name"]


class TestCaseRowInline(admin.TabularInline):
    model = TestCaseRow
    extra = 0


@admin.register(TestCaseVersion)
class TestCaseVersionAdmin(admin.ModelAdmin):
    list_display = ["test_case", "version_number", "row_count", "uploaded_at"]
    inlines = [TestCaseRowInline]


@admin.register(PromptTemplate)
class PromptTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "test_case", "response_format", "created_at"]


@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "provider",
        "model_name",
        "is_agent",
        "agent_alias",
        "rate_limit_rpm",
        "is_active",
    ]
    list_filter = ["provider", "is_agent", "is_active"]


class TestRunResultInline(admin.TabularInline):
    model = TestRunResult
    extra = 0


@admin.register(TestRun)
class TestRunAdmin(admin.ModelAdmin):
    list_display = ["id", "prompt_template", "model_config", "status", "rows_completed", "rows_total", "created_at"]
    list_filter = ["status"]


@admin.register(TestRunResult)
class TestRunResultAdmin(admin.ModelAdmin):
    list_display = ["test_run", "test_case_row", "status", "latency_ms", "input_tokens", "output_tokens"]


@admin.register(EvaluationConfig)
class EvaluationConfigAdmin(admin.ModelAdmin):
    list_display = ["name", "test_case", "eval_type", "created_at"]
    list_filter = ["eval_type"]


@admin.register(EvaluationRun)
class EvaluationRunAdmin(admin.ModelAdmin):
    list_display = ["id", "evaluation_config", "test_run", "status", "is_gold_standard", "created_at"]
    list_filter = ["status"]


@admin.register(EvaluationResult)
class EvaluationResultAdmin(admin.ModelAdmin):
    list_display = ["evaluation_run", "test_run_result", "assessor_type", "assessor_id", "created_at"]


class AgentAssetVersionInline(admin.TabularInline):
    model = AgentAssetVersion
    extra = 0
    fields = ["label", "content_hash", "is_working_copy", "is_deprecated", "ready", "last_synced_at"]
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(AgentAsset)
class AgentAssetAdmin(admin.ModelAdmin):
    list_display = ["kind", "name", "is_active", "last_synced_at"]
    list_filter = ["kind", "is_active"]
    search_fields = ["name", "description"]
    readonly_fields = ["id", "last_synced_at"]
    inlines = [AgentAssetVersionInline]


@admin.register(AgentAssetVersion)
class AgentAssetVersionAdmin(admin.ModelAdmin):
    list_display = [
        "asset",
        "label",
        "is_working_copy",
        "is_deprecated",
        "ready",
        "content_hash",
        "last_synced_at",
    ]
    list_filter = ["asset__kind", "is_working_copy", "is_deprecated", "ready"]
    search_fields = ["asset__name", "label", "content_hash", "git_sha"]
    readonly_fields = [
        "id",
        "asset",
        "label",
        "file_path",
        "content_hash",
        "git_sha",
        "declared_params",
        "pinned_deps",
        "is_working_copy",
        "ready",
        "import_error",
        "created_at_agent",
        "first_seen_at",
        "last_synced_at",
    ]
