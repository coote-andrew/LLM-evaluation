"""Django admin for evaluation workbench models."""

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from types import SimpleNamespace

from core.forms import ModelConfigForm
from core.models import (
    EntraIdentity,
    UserProfile,
    TestCase,
    TestCaseAttachment,
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
    ModelConfigShare,
    ProjectShare,
)


class AdminModelConfigForm(ModelConfigForm):
    """Expose staff-only access controls within Django admin."""

    def __init__(self, *args, **kwargs):
        kwargs["user"] = SimpleNamespace(is_staff=True)
        super().__init__(*args, **kwargs)


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0


class EntraIdentityInline(admin.StackedInline):
    model = EntraIdentity
    can_delete = False
    extra = 0
    readonly_fields = ["tenant_id", "object_id", "issuer", "last_login_at"]
    fields = ["tenant_id", "object_id", "issuer", "last_known_upn", "last_login_at"]


class EntraIdentityTransferForm(forms.Form):
    target_user = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Existing local user",
        help_text=(
            "The selected user keeps their existing roles, project ownership, "
            "and shares. They must not already have an Entra identity."
        ),
    )

    def __init__(self, *args, source_user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_user"].queryset = (
            User.objects.filter(entra_identity__isnull=True)
            .exclude(pk=source_user.pk)
            .order_by("username")
        )


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [UserProfileInline, EntraIdentityInline]
    change_form_template = "admin/auth/user/change_form.html"

    def get_urls(self):
        urls = super().get_urls()
        return [
            path(
                "<int:user_id>/transfer-entra-identity/",
                self.admin_site.admin_view(self.transfer_entra_identity),
                name="auth_user_transfer_entra_identity",
            ),
        ] + urls

    def transfer_entra_identity(self, request, user_id):
        source_user = self.get_object(request, user_id)
        if source_user is None or not self.has_change_permission(request, source_user):
            raise Http404
        try:
            identity = source_user.entra_identity
        except EntraIdentity.DoesNotExist as error:
            raise Http404("This user has no Entra identity to transfer.") from error

        if request.method == "POST":
            form = EntraIdentityTransferForm(request.POST, source_user=source_user)
            if form.is_valid():
                target_user = form.cleaned_data["target_user"]
                with transaction.atomic():
                    if EntraIdentity.objects.filter(user=target_user).exists():
                        form.add_error(
                            "target_user",
                            "This user already has an Entra identity.",
                        )
                    else:
                        identity.user = target_user
                        identity.save(update_fields=["user"])
                        source_user.is_active = False
                        source_user.save(update_fields=["is_active"])
                if not form.errors:
                    self.message_user(
                        request,
                        (
                            f"Transferred Entra identity from {source_user.username} "
                            f"to {target_user.username} and deactivated the former "
                            "SSO-created account."
                        ),
                        messages.SUCCESS,
                    )
                    return redirect(
                        reverse("admin:auth_user_change", args=[target_user.pk])
                    )
        else:
            form = EntraIdentityTransferForm(source_user=source_user)

        context = {
            **self.admin_site.each_context(request),
            "title": "Transfer Entra identity",
            "source_user": source_user,
            "identity": identity,
            "form": form,
            "opts": self.model._meta,
        }
        return TemplateResponse(
            request,
            "admin/auth/user/transfer_entra_identity.html",
            context,
        )


class ProjectShareInline(admin.TabularInline):
    model = ProjectShare
    extra = 0


@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin):
    list_display = ["name", "visibility", "created_at", "created_by"]
    list_filter = ["visibility"]
    search_fields = ["name"]
    inlines = [ProjectShareInline]


class TestCaseRowInline(admin.TabularInline):
    model = TestCaseRow
    extra = 0


class TestCaseAttachmentInline(admin.TabularInline):
    model = TestCaseAttachment
    extra = 0
    readonly_fields = ["relative_path", "mime_type", "size_bytes", "sha256", "created_at"]
    fields = ["relative_path", "file", "mime_type", "size_bytes", "sha256", "created_at"]
    can_delete = False


@admin.register(TestCaseVersion)
class TestCaseVersionAdmin(admin.ModelAdmin):
    list_display = ["test_case", "version_number", "row_count", "uploaded_at"]
    inlines = [TestCaseRowInline, TestCaseAttachmentInline]


@admin.register(PromptTemplate)
class PromptTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "test_case", "response_format", "is_active", "created_at"]
    list_filter = ["is_active"]


class ModelConfigShareInline(admin.TabularInline):
    model = ModelConfigShare
    extra = 0


@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    form = AdminModelConfigForm
    list_display = [
        "name",
        "provider",
        "auth_type",
        "model_name",
        "is_agent",
        "agent_alias",
        "rate_limit_rpm",
        "is_active",
        "visibility",
        "created_by",
    ]
    list_filter = ["provider", "auth_type", "is_agent", "is_active", "visibility"]
    inlines = [ModelConfigShareInline]


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
    list_display = ["name", "version_number", "is_current", "test_case", "eval_type", "created_at"]
    list_filter = ["eval_type", "is_current"]
    readonly_fields = ["config_group", "version_number", "is_current"]


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
