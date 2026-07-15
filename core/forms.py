"""Forms for the evaluation workbench."""

import uuid

from django import forms
from django.urls import reverse_lazy

from core.models import AuthType, ModelConfig, PromptTemplate, Provider, TestCase, TestCaseVersion


class TestCaseUploadForm(forms.Form):
    """Form for uploading CSV/Excel to a test case."""

    test_case = forms.ModelChoiceField(
        queryset=TestCase.objects.all(),
        required=False,
        empty_label="Create new test case",
        help_text="Leave empty to create a new test case",
    )
    file = forms.FileField(
        label="CSV or Excel file",
        help_text="Columns must be prefixed with input_ or output_",
    )
    group_by_columns = forms.CharField(
        required=False,
        label="Group by columns (optional)",
        help_text=(
            "Comma-separated list of input_ columns that are static per group "
            "(e.g. input_csn, input_admission_date). Each unique combination "
            "becomes one test case row. All other input_ columns are collected "
            "into an input_notes array. Leave blank for normal flat upload."
        ),
    )
    sort_by_column = forms.CharField(
        required=False,
        label="Sort notes by column (optional)",
        help_text=(
            "Column name to sort notes within each group (e.g. input_note_date). "
            "Only used when group by columns are set."
        ),
    )


class PromptTemplateForm(forms.ModelForm):
    """Create/edit prompt template."""

    class Meta:
        model = PromptTemplate
        fields = ["name", "template_text", "response_format"]


class ModelConfigForm(forms.ModelForm):
    """Create/edit model configuration."""

    class Meta:
        model = ModelConfig
        fields = [
            "name",
            "provider",
            "auth_type",
            "api_endpoint",
            "api_key",
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_token_scope",
            "model_name",
            "default_temperature",
            "default_max_tokens",
            "default_timeout",
            "rate_limit_rpm",
            "max_concurrency",
            "is_agent",
            "agent_alias",
            "is_active",
        ]
        widgets = {
            "api_key": forms.PasswordInput(
                attrs={"autocomplete": "new-password"},
                render_value=False,
            ),
            "azure_client_secret": forms.PasswordInput(
                attrs={"autocomplete": "new-password"},
                render_value=False,
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["api_key"].required = False
        self.fields["api_key"].help_text = "Leave blank to keep the existing API key."
        self.fields["azure_tenant_id"].label = "Azure tenant ID"
        self.fields["azure_tenant_id"].help_text = "Directory (tenant) ID from Microsoft Entra ID."
        self.fields["azure_client_id"].label = "Azure client ID"
        self.fields["azure_client_id"].help_text = "Application (client) ID from the app registration."
        self.fields["azure_client_secret"].required = False
        self.fields["azure_client_secret"].label = "Azure client secret"
        self.fields["azure_client_secret"].help_text = "Leave blank to keep the existing client secret."
        self.fields["azure_token_scope"].label = "Azure token scope"
        self.fields["azure_token_scope"].help_text = (
            "Usually https://cognitiveservices.azure.com/.default"
        )
        self.fields["model_name"].help_text = (
            "For Azure OpenAI classic, use the deployment name. For Azure AI Foundry "
            "and OpenAI-compatible endpoints, use the model name."
        )
        from django.conf import settings as django_settings
        self.fields["max_concurrency"].help_text = (
            f"Maximum concurrent LLM requests (1 = sequential). "
            f"Hard-capped at {django_settings.MAX_MODEL_CONCURRENCY}."
        )

    def clean_max_concurrency(self):
        from django.conf import settings as django_settings

        value = self.cleaned_data.get("max_concurrency") or 1
        cap = max(1, int(django_settings.MAX_MODEL_CONCURRENCY))
        if value > cap:
            raise forms.ValidationError(
                f"max_concurrency cannot exceed MAX_MODEL_CONCURRENCY ({cap})."
            )
        return max(1, value)

    def _has_saved_secret(self, field_name):
        if not self.instance or not self.instance.pk:
            return False
        return bool(getattr(self.instance, field_name, ""))

    def _validate_uuid_field(self, cleaned, field_name):
        value = cleaned.get(field_name)
        if not value:
            return
        try:
            uuid.UUID(value)
        except (TypeError, ValueError):
            self.add_error(field_name, "Enter a valid UUID.")

    def clean(self):
        cleaned = super().clean()
        auth_type = cleaned.get("auth_type")
        provider = cleaned.get("provider")
        api_key = cleaned.get("api_key")
        azure_tenant_id = cleaned.get("azure_tenant_id")
        azure_client_id = cleaned.get("azure_client_id")
        azure_client_secret = cleaned.get("azure_client_secret")

        self._validate_uuid_field(cleaned, "azure_tenant_id")
        self._validate_uuid_field(cleaned, "azure_client_id")

        if auth_type == AuthType.AZURE_CLIENT_SECRET:
            if provider not in (Provider.AZURE_OPENAI, Provider.AZURE_AI_FOUNDRY):
                self.add_error(
                    "auth_type",
                    "Azure app registration authentication is only available for Azure providers.",
                )
            if not azure_tenant_id:
                self.add_error("azure_tenant_id", "Azure tenant ID is required.")
            if not azure_client_id:
                self.add_error("azure_client_id", "Azure client ID is required.")
            if not azure_client_secret and not self._has_saved_secret("azure_client_secret"):
                self.add_error("azure_client_secret", "Azure client secret is required.")
        else:
            cleaned["azure_tenant_id"] = ""
            cleaned["azure_client_id"] = ""
            cleaned["azure_client_secret"] = ""

        api_key_required_providers = {
            Provider.AZURE_OPENAI,
            Provider.AZURE_AI_FOUNDRY,
            Provider.OPENAI,
            Provider.ANTHROPIC,
        }
        if (
            auth_type == AuthType.API_KEY
            and provider in api_key_required_providers
            and not api_key
            and not self._has_saved_secret("api_key")
        ):
            self.add_error("api_key", "API key is required for this provider.")

        is_agent = cleaned.get("is_agent")
        agent_alias = cleaned.get("agent_alias")
        if is_agent and not agent_alias:
            self.add_error(
                "agent_alias",
                "Agent alias is required when 'Is agent' is enabled.",
            )
        if not is_agent and agent_alias:
            # Keep uniqueness meaningful: only agent configs own an alias.
            cleaned["agent_alias"] = None
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if not instance._state.adding:
            saved = type(self.instance).objects.get(pk=self.instance.pk)
            if (
                self.cleaned_data.get("auth_type") == AuthType.API_KEY
                and not self.cleaned_data.get("api_key")
            ):
                instance.api_key = saved.api_key
            elif self.cleaned_data.get("auth_type") != AuthType.API_KEY:
                instance.api_key = ""
            if (
                self.cleaned_data.get("auth_type") == AuthType.AZURE_CLIENT_SECRET
                and not self.cleaned_data.get("azure_client_secret")
            ):
                instance.azure_client_secret = saved.azure_client_secret
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class TestRunCreateForm(forms.Form):
    """Create a new test run."""

    test_case_version = forms.ModelChoiceField(
        queryset=TestCaseVersion.objects.select_related("test_case").all(),
        label="Test case version",
    )
    prompt_template = forms.ModelChoiceField(
        queryset=PromptTemplate.objects.select_related("test_case").all(),
        label="Prompt template",
    )
    model_config = forms.ModelChoiceField(
        queryset=ModelConfig.objects.filter(is_active=True),
        label="Model",
    )
    row_limit = forms.IntegerField(
        required=False,
        min_value=1,
        label="Row limit (optional)",
        help_text="Leave empty to process all rows",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["test_case_version"].widget.attrs.update({
            "hx-get": reverse_lazy("core:testrun_prompt_template_options"),
            "hx-trigger": "change",
            "hx-target": "#id_prompt_template",
            "hx-swap": "innerHTML",
            "hx-include": "#id_prompt_template",
        })

        test_case_version = self._selected_test_case_version()
        if test_case_version:
            self.fields["prompt_template"].queryset = (
                PromptTemplate.objects.filter(test_case=test_case_version.test_case)
                .select_related("test_case")
            )

    def _selected_test_case_version(self):
        value = self.data.get("test_case_version") if self.is_bound else None
        if not value:
            value = self.initial.get("test_case_version")
        if isinstance(value, TestCaseVersion):
            return value
        if not value:
            return None
        try:
            return TestCaseVersion.objects.select_related("test_case").get(pk=value)
        except (TestCaseVersion.DoesNotExist, ValueError, TypeError):
            return None
