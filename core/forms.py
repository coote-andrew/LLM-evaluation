"""Forms for the evaluation workbench."""

from django import forms
from django.urls import reverse_lazy

from core.models import TestCase, ModelConfig, PromptTemplate, TestCaseVersion


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
            "api_endpoint",
            "api_key",
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
            "api_key": forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        }

    def clean(self):
        cleaned = super().clean()
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
