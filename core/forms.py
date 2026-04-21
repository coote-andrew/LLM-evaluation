"""Forms for the evaluation workbench."""

from django import forms

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
            "is_active",
        ]
        widgets = {
            "api_key": forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        }


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
