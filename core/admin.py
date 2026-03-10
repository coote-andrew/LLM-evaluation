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
    list_display = ["name", "provider", "model_name", "rate_limit_rpm", "is_active"]


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
