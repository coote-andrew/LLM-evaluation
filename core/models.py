"""
LLM Evaluation Workbench - Core models.

Data model per proposal.md section 4.
"""

import uuid

from django.conf import settings
from django.db import models

from encrypted_model_fields.fields import EncryptedCharField


class TestCase(models.Model):
    """A named container for a particular evaluation task."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_test_cases',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class TestCaseVersion(models.Model):
    """A specific CSV/Excel upload for a test case."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_case = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name='versions',
    )
    version_number = models.PositiveIntegerField()
    original_filename = models.CharField(max_length=255)
    column_names = models.JSONField(default=list)  # List of all column names
    input_columns = models.JSONField(default=list)  # Columns starting with input_
    output_columns = models.JSONField(default=list)  # Columns starting with output_
    row_count = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='uploaded_versions',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['test_case', '-version_number']
        unique_together = [['test_case', 'version_number']]

    def __str__(self):
        return f"{self.test_case.name} v{self.version_number}"


class TestCaseRow(models.Model):
    """One row from a test case version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        TestCaseVersion,
        on_delete=models.CASCADE,
        related_name='rows',
    )
    row_number = models.PositiveIntegerField()  # 1-indexed
    input_fields = models.JSONField(default=dict)  # input_ column values
    expected_output_fields = models.JSONField(default=dict)  # output_ column values

    class Meta:
        ordering = ['version', 'row_number']
        unique_together = [['version', 'row_number']]

    def __str__(self):
        return f"Row {self.row_number} of {self.version}"


class ResponseFormat(models.TextChoices):
    JSON = 'json', 'JSON'
    FREE_TEXT = 'free_text', 'Free text'


class PromptTemplate(models.Model):
    """Reusable text template with {column_name} placeholders.

    Templates are versioned: each edit creates a new PromptTemplate row with an
    incremented version_number.  All versions share the same ``name`` and
    ``test_case``; ``parent_template`` points to the immediately preceding
    version (null for v1).  The highest version_number for a given
    (test_case, name) pair is considered the *current* version.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_case = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name='prompt_templates',
    )
    name = models.CharField(max_length=255)
    version_number = models.PositiveIntegerField(default=1)
    parent_template = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='child_versions',
    )
    template_text = models.TextField()
    response_format = models.CharField(
        max_length=20,
        choices=ResponseFormat.choices,
        default=ResponseFormat.FREE_TEXT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_prompt_templates',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['test_case', 'name', '-version_number']
        unique_together = [['test_case', 'name', 'version_number']]

    def __str__(self):
        return f"{self.name} v{self.version_number} ({self.test_case.name})"

    @property
    def is_latest(self):
        """True if this is the highest-versioned template for this (test_case, name)."""
        return not PromptTemplate.objects.filter(
            test_case=self.test_case,
            name=self.name,
            version_number__gt=self.version_number,
        ).exists()


class Provider(models.TextChoices):
    AZURE_OPENAI = 'azure_openai', 'Azure OpenAI (classic deployment)'
    AZURE_AI_FOUNDRY = 'azure_ai_foundry', 'Azure AI Foundry'
    OPENAI = 'openai', 'OpenAI'
    ANTHROPIC = 'anthropic', 'Anthropic'
    VLLM = 'vllm', 'vLLM'
    LOCAL = 'local', 'Local (e.g. Ollama)'
    CUSTOM = 'custom', 'Custom'


class ModelConfig(models.Model):
    """Saved record of how to reach a particular LLM."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.OPENAI,
    )
    api_endpoint = models.URLField(max_length=500, blank=True)
    api_key = EncryptedCharField(max_length=500, blank=True)
    model_name = models.CharField(max_length=255)
    default_temperature = models.FloatField(default=0.0)
    default_max_tokens = models.PositiveIntegerField(default=4096)
    default_timeout = models.FloatField(
        default=120.0,
        help_text="HTTP timeout in seconds for LLM API requests",
    )
    rate_limit_rpm = models.PositiveIntegerField(
        default=60,
        help_text="Requests per minute for throttling",
    )
    max_concurrency = models.PositiveIntegerField(
        default=1,
        help_text="Maximum number of concurrent requests to this model (1 = sequential)",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_model_configs',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Model configuration'
        verbose_name_plural = 'Model configurations'

    def __str__(self):
        return self.name


class RunStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    RUNNING = 'running', 'Running'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'
    CANCELLED = 'cancelled', 'Cancelled'


class TestRun(models.Model):
    """A single execution of one prompt template against one model."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_case_version = models.ForeignKey(
        TestCaseVersion,
        on_delete=models.CASCADE,
        related_name='test_runs',
    )
    prompt_template = models.ForeignKey(
        PromptTemplate,
        on_delete=models.CASCADE,
        related_name='test_runs',
    )
    model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.CASCADE,
        related_name='test_runs',
    )
    parent_run = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='continuation_runs',
    )
    status = models.CharField(
        max_length=20,
        choices=RunStatus.choices,
        default=RunStatus.PENDING,
    )
    row_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="If set, only process first N rows",
    )
    skip_rows_from_parent = models.BooleanField(
        default=False,
        help_text="If true, skip rows already processed in parent run",
    )
    rows_total = models.PositiveIntegerField(default=0)
    rows_completed = models.PositiveIntegerField(default=0)
    rows_failed = models.PositiveIntegerField(default=0)
    temperature_override = models.FloatField(null=True, blank=True)
    prompt_snapshot = models.TextField(
        blank=True,
        help_text="Full template text at time of run",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_test_runs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    total_duration_seconds = models.FloatField(null=True, blank=True)
    total_input_tokens = models.PositiveIntegerField(default=0)
    total_output_tokens = models.PositiveIntegerField(default=0)
    error_message = models.TextField(
        blank=True,
        default="",
        help_text="Populated when the run fails unexpectedly",
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Run {str(self.id)[:8]} - {self.prompt_template.name} @ {self.model_config.name}"


class EvalType(models.TextChoices):
    KEYWORD_MATCH = 'keyword_match', 'Keyword / phrase match'
    AI_JUDGE = 'ai_judge', 'AI judge'
    HUMAN = 'human', 'Human review'
    FIELD_MATCH = 'field_match', 'Field match (JSON output vs expected)'
    PYTHON_EVAL = 'python_eval', 'Python script'


class EvaluationConfig(models.Model):
    """Defines how to evaluate a test run — keyword, AI judge, or human."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_case = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name='evaluation_configs',
    )
    name = models.CharField(max_length=255)
    eval_type = models.CharField(max_length=20, choices=EvalType.choices)
    judge_prompt_template = models.TextField(
        blank=True,
        help_text="For AI judge: template with {input}, {output}, {expected} placeholders",
    )
    judge_model_config = models.ForeignKey(
        'ModelConfig',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='judge_configs',
        help_text="Which model to use as the judge",
    )
    scoring_criteria = models.JSONField(
        default=dict,
        help_text="Defines checks (keyword) or output fields (AI/human)",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_eval_configs',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['test_case', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_eval_type_display()})"


class EvalRunStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    IN_PROGRESS = 'in_progress', 'In progress'
    COMPLETED = 'completed', 'Completed'


class EvaluationRun(models.Model):
    """One execution of an evaluation config against a test run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evaluation_config = models.ForeignKey(
        EvaluationConfig,
        on_delete=models.CASCADE,
        related_name='evaluation_runs',
    )
    test_run = models.ForeignKey(
        TestRun,
        on_delete=models.CASCADE,
        related_name='evaluation_runs',
    )
    is_gold_standard = models.BooleanField(
        default=False,
        help_text="True if this is the authoritative human review",
    )
    status = models.CharField(
        max_length=20,
        choices=EvalRunStatus.choices,
        default=EvalRunStatus.PENDING,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_eval_runs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Eval {str(self.id)[:8]} — {self.evaluation_config.name}"

    @property
    def results_count(self):
        return self.results.count()

    @property
    def completed_count(self):
        return self.results.count()


class AssessorType(models.TextChoices):
    AI = 'ai', 'AI'
    HUMAN = 'human', 'Human'


class EvaluationResult(models.Model):
    """Assessment of one test run result by a human or AI judge."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evaluation_run = models.ForeignKey(
        EvaluationRun,
        on_delete=models.CASCADE,
        related_name='results',
    )
    test_run_result = models.ForeignKey(
        'TestRunResult',
        on_delete=models.CASCADE,
        related_name='evaluation_results',
    )
    assessor_type = models.CharField(max_length=10, choices=AssessorType.choices)
    assessor_id = models.CharField(max_length=255, help_text="User ID or model name")
    assessment = models.JSONField(
        default=dict,
        help_text="Scores/flags/notes — structure matches scoring_criteria",
    )
    notes = models.TextField(blank=True)
    judge_prompt_sent = models.TextField(
        blank=True,
        help_text="Exact prompt sent to the AI judge",
    )
    raw_judge_response = models.TextField(
        blank=True,
        help_text="Raw text response from the AI judge (for debugging)",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['evaluation_run', 'test_run_result__test_case_row__row_number']
        unique_together = [['evaluation_run', 'test_run_result']]

    def __str__(self):
        return f"EvalResult row {self.test_run_result.test_case_row.row_number}"


class ResultStatus(models.TextChoices):
    SUCCESS = 'success', 'Success'
    ERROR = 'error', 'Error'
    TIMEOUT = 'timeout', 'Timeout'


class TestRunResult(models.Model):
    """Result for one row in a test run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_run = models.ForeignKey(
        TestRun,
        on_delete=models.CASCADE,
        related_name='results',
    )
    test_case_row = models.ForeignKey(
        TestCaseRow,
        on_delete=models.CASCADE,
        related_name='run_results',
    )
    prompt_sent = models.TextField()
    raw_response = models.TextField(blank=True)
    response_parsed = models.JSONField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=ResultStatus.choices,
        default=ResultStatus.SUCCESS,
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['test_run', 'test_case_row__row_number']
        unique_together = [['test_run', 'test_case_row']]

    def __str__(self):
        return f"Result row {self.test_case_row.row_number} - {self.status}"
