"""
LLM Evaluation Workbench - Core models.

Data model per proposal.md section 4.
"""

import uuid

from django.conf import settings
from django.db import models

from encrypted_model_fields.fields import EncryptedCharField


class UserProfile(models.Model):
    """Per-user account settings that extend Django's built-in user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    must_change_password = models.BooleanField(default=False)

    def __str__(self):
        return f"Profile for {self.user}"


class Visibility(models.TextChoices):
    PRIVATE = "private", "Private"
    SHARED = "shared", "Shared"
    PUBLIC = "public", "Public"


class ShareRole(models.TextChoices):
    VIEWER = "viewer", "Viewer"
    EDITOR = "editor", "Editor"


class TestCase(models.Model):
    """A named container for a particular evaluation task and its data."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )
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


class ProjectShare(models.Model):
    """An explicit user's access to a shared project."""

    project = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name="shares",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_shares",
    )
    role = models.CharField(
        max_length=10,
        choices=ShareRole.choices,
        default=ShareRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["project", "user"]]

    def __str__(self):
        return f"{self.project}: {self.user} ({self.role})"


# Public domain terminology while the persisted Django model keeps its
# established name for migration compatibility.
Project = TestCase


class TestCaseVersion(models.Model):
    """A specific CSV/Excel manifest upload for a project."""

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
    file_columns = models.JSONField(default=list)  # Columns starting with file_
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
    """One row from a project version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        TestCaseVersion,
        on_delete=models.CASCADE,
        related_name='rows',
    )
    row_number = models.PositiveIntegerField()  # 1-indexed
    input_fields = models.JSONField(default=dict)  # input_ column values
    expected_output_fields = models.JSONField(default=dict)  # output_ column values
    file_fields = models.JSONField(default=dict)  # file_ column paths in the bundle

    class Meta:
        ordering = ['version', 'row_number']
        unique_together = [['version', 'row_number']]

    def __str__(self):
        return f"Row {self.row_number} of {self.version}"


def project_attachment_upload_to(instance, filename):
    """Place attachments under the owning immutable project version."""
    return f"project_versions/{instance.version_id}/{filename}"


def testcase_attachment_upload_to(instance, filename):
    """Legacy upload path retained so historical migrations remain importable."""
    return f"testcase_versions/{instance.version_id}/{filename}"


class TestCaseAttachment(models.Model):
    """One referenced attachment stored once for a project version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        TestCaseVersion,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    relative_path = models.CharField(max_length=500)
    file = models.FileField(upload_to=testcase_attachment_upload_to, max_length=700)
    mime_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["version", "relative_path"]
        unique_together = [["version", "relative_path"]]

    def __str__(self):
        return self.relative_path


# Public domain terminology while the persisted Django model keeps its
# established name for migration compatibility.
ProjectVersion = TestCaseVersion
ProjectRow = TestCaseRow
ProjectAttachment = TestCaseAttachment


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


class AuthType(models.TextChoices):
    API_KEY = 'api_key', 'API key'
    AZURE_CLIENT_SECRET = 'azure_client_secret', 'Azure app registration'


class ModelConfig(models.Model):
    """Saved record of how to reach a particular LLM."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.OPENAI,
    )
    auth_type = models.CharField(
        max_length=30,
        choices=AuthType.choices,
        default=AuthType.API_KEY,
    )
    api_endpoint = models.URLField(max_length=500, blank=True)
    api_key = EncryptedCharField(max_length=500, blank=True)
    azure_tenant_id = models.CharField(max_length=36, blank=True)
    azure_client_id = models.CharField(max_length=36, blank=True)
    azure_client_secret = EncryptedCharField(max_length=500, blank=True)
    azure_token_scope = models.CharField(
        max_length=255,
        blank=True,
        default="https://cognitiveservices.azure.com/.default",
    )
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
        help_text=(
            "Maximum number of concurrent requests to this model (1 = sequential). "
            "Clamped at runtime by MAX_MODEL_CONCURRENCY to protect the database "
            "connection budget."
        ),
    )
    attachment_types = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Attachment MIME types this specific model accepts, for example "
            "image/png or application/pdf. Leave empty for text-only."
        ),
    )
    is_agent = models.BooleanField(
        default=False,
        help_text=(
            "If true, this endpoint is a clinical_graphs agent service exposing "
            "pattern workflows via OpenAI-compatible /v1/chat/completions. "
            "`model_name` should be the pattern alias (e.g. 'clinical_note_analysis')."
        ),
    )
    agent_alias = models.SlugField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
        help_text=(
            "Unique short name for this agent endpoint as referenced by Django UI "
            "and by the generated llm_providers.yaml. Only meaningful when "
            "is_agent=True. Leave blank for regular LLM configs."
        ),
    )
    is_active = models.BooleanField(default=True)
    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
    )
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


class ModelConfigShare(models.Model):
    """An explicit user's access to a shared model configuration."""

    model_config = models.ForeignKey(
        ModelConfig,
        on_delete=models.CASCADE,
        related_name="shares",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="model_config_shares",
    )
    role = models.CharField(
        max_length=10,
        choices=ShareRole.choices,
        default=ShareRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["model_config", "user"]]

    def __str__(self):
        return f"{self.model_config}: {self.user} ({self.role})"


class AgentAssetKind(models.TextChoices):
    """Kinds of asset exposed by the agents service registry.

    Mirrors the ``kind`` field on ``GET /admin/registry`` responses.
    """
    TOOL = 'tool', 'Tool'
    NODE = 'node', 'Node'
    PATTERN = 'pattern', 'Pattern'
    SYSTEM_PROMPT = 'system_prompt', 'System prompt'


class AgentAsset(models.Model):
    """Metadata-only cache of an asset published by the agents service.

    One row per (kind, name). The agent service owns the source files and is
    always authoritative. This table exists so Django can show pickers and
    listings without round-tripping the agents admin API on every request.

    See ``docs/AGENTS_SERVICE_GUIDE.md`` for the full contract and invariants
    we rely on from the agent service.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=AgentAssetKind.choices)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    # Populated by the most recent sync; a missing asset on sync marks it
    # inactive rather than deleting (TestRuns may FK versions of it).
    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['kind', 'name']
        unique_together = [['kind', 'name']]

    def __str__(self):
        return f"{self.get_kind_display()} / {self.name}"


class AgentAssetVersion(models.Model):
    """Metadata for one cut (or @latest) version of an agent asset.

    No source is ever stored here — the agents service streams source and
    diffs on demand via its admin API. ``content_hash`` is what lets Django
    detect drift and deduplicate entries across syncs.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(
        AgentAsset,
        on_delete=models.CASCADE,
        related_name='versions',
    )
    label = models.CharField(
        max_length=40,
        help_text="E.g. '1.2' for a cut snapshot or '@latest' for the working copy.",
    )
    file_path = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Path within the agents repo, for display only.",
    )
    content_hash = models.CharField(
        max_length=80,
        help_text="sha256:<hex> of the file bytes as reported by the agents service.",
    )
    git_sha = models.CharField(max_length=80, blank=True, default="")
    declared_params = models.JSONField(default=dict, blank=True)
    # Dotted keys like "tool.snomed_lookup" → version label string.
    pinned_deps = models.JSONField(default=dict, blank=True)
    is_working_copy = models.BooleanField(
        default=False,
        help_text="True for the synthetic @latest row tracking the working copy.",
    )
    is_deprecated = models.BooleanField(default=False)
    ready = models.BooleanField(
        default=True,
        help_text="False if the agents service reported a sandbox-import failure.",
    )
    import_error = models.TextField(blank=True, default="")
    created_at_agent = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp reported by the agents service (source-of-truth).",
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['asset', '-label']
        unique_together = [['asset', 'label']]

    def __str__(self):
        return f"{self.asset} @ {self.label}"


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
    attachment_metadata = models.JSONField(
        default=list,
        blank=True,
        help_text="Attachment paths, checksums, MIME types, and delivery strategy.",
    )
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
