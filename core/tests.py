"""
Core app tests.

Consolidated here to avoid unittest discovery conflicts with core/tests/ package.
"""

import csv
import io
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase
from django.urls import reverse

from core.models import (
    AssessorType,
    EvalType,
    EvaluationConfig,
    EvaluationResult,
    EvaluationRun,
    ModelConfig,
    PromptTemplate,
    Provider,
    ResponseFormat,
    RunStatus,
    TestCase,
    TestCaseRow,
    TestCaseVersion,
    TestRun,
    TestRunResult,
)
from core.services.csv_parser import parse_csv, parse_excel, parse_upload
from core.services.llm_client import (
    _build_auth_headers,
    _build_openai_compatible_url,
    _strip_think_tags,
)
from core.services.prompt_builder import build_prompt, get_placeholder_names, validate_template
from core.views.evaluations import compute_sens_spec, _ground_truth_positive

User = get_user_model()


# --- CSV Parser tests ---


class ParseCSVTests(DjangoTestCase):
    def test_parses_input_output_columns(self):
        content = "input_text,output_label\nhello,positive\ngoodbye,negative\n"
        result = parse_csv(content)
        self.assertEqual(result["column_names"], ["input_text", "output_label"])
        self.assertEqual(result["input_columns"], ["input_text"])
        self.assertEqual(result["output_columns"], ["output_label"])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["input_fields"], {"input_text": "hello"})
        self.assertEqual(result["rows"][0]["expected_output_fields"], {"output_label": "positive"})

    def test_accepts_bytes(self):
        content = b"input_x\nval1\n"
        result = parse_csv(content)
        self.assertEqual(result["input_columns"], ["input_x"])
        self.assertEqual(result["rows"][0]["input_fields"], {"input_x": "val1"})

    def test_handles_empty_file(self):
        content = "input_x\n"
        result = parse_csv(content)
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["rows"], [])

    def test_strips_whitespace_from_column_names(self):
        # CSV exported with spaces after commas, e.g. "input_unit, input_csn, ..."
        content = "input_unit, input_csn, input_notetext\nGastro,97333199,Some note\n"
        result = parse_csv(content)
        self.assertEqual(
            sorted(result["input_columns"]),
            ["input_csn", "input_notetext", "input_unit"],
        )
        self.assertEqual(result["rows"][0]["input_fields"]["input_csn"], "97333199")
        self.assertEqual(result["rows"][0]["input_fields"]["input_notetext"], "Some note")


class ParseUploadTests(DjangoTestCase):
    def test_csv_extension(self):
        content = b"input_a,output_b\na,b\n"
        result = parse_upload(content, "test.csv")
        self.assertEqual(result["input_columns"], ["input_a"])
        self.assertEqual(result["row_count"], 1)

    def test_xlsx_extension(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["input_x", "output_y"])
        ws.append(["data1", "label1"])
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        result = parse_upload(buf.getvalue(), "test.xlsx")
        self.assertEqual(result["input_columns"], ["input_x"])
        self.assertGreaterEqual(result["row_count"], 1)


# --- Prompt builder tests ---


class BuildPromptTests(DjangoTestCase):
    def test_replaces_placeholders(self):
        template = "Extract from: {input_text}"
        fields = {"input_text": "hello world"}
        self.assertEqual(build_prompt(template, fields), "Extract from: hello world")

    def test_multiple_placeholders(self):
        template = "{input_a} and {input_b}"
        fields = {"input_a": "A", "input_b": "B"}
        self.assertEqual(build_prompt(template, fields), "A and B")


class ValidateTemplateTests(DjangoTestCase):
    def test_valid_when_all_placeholders_in_columns(self):
        valid, missing = validate_template("Hello {input_x}", ["input_x"])
        self.assertTrue(valid)
        self.assertEqual(missing, [])

    def test_invalid_when_missing_column(self):
        valid, missing = validate_template("Hello {input_x} {input_y}", ["input_x"])
        self.assertFalse(valid)
        self.assertIn("input_y", missing)


class GetPlaceholderNamesTests(DjangoTestCase):
    def test_extracts_names(self):
        names = get_placeholder_names("A {x} B {y} C")
        self.assertEqual(names, {"x", "y"})


# --- Model tests ---


class TestCaseModelTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")

    def test_create_test_case(self):
        tc = TestCase.objects.create(name="ED Extraction", description="Test", created_by=self.user)
        self.assertEqual(tc.name, "ED Extraction")
        self.assertIsNotNone(tc.id)

    def test_create_version_with_rows(self):
        tc = TestCase.objects.create(name="TC1", created_by=self.user)
        v = TestCaseVersion.objects.create(
            test_case=tc,
            version_number=1,
            original_filename="data.csv",
            column_names=["input_text", "output_label"],
            input_columns=["input_text"],
            output_columns=["output_label"],
            row_count=2,
            uploaded_by=self.user,
        )
        TestCaseRow.objects.create(
            version=v,
            row_number=1,
            input_fields={"input_text": "hello"},
            expected_output_fields={"output_label": "pos"},
        )
        self.assertEqual(v.rows.count(), 1)


class ModelConfigTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")

    def test_create_model_config(self):
        mc = ModelConfig.objects.create(
            name="GPT-4",
            provider=Provider.OPENAI,
            model_name="gpt-4",
            api_key="sk-test",
            created_by=self.user,
        )
        self.assertEqual(mc.provider, Provider.OPENAI)
        self.assertTrue(mc.is_active)


class TestRunModelTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.tc = TestCase.objects.create(name="TC", created_by=self.user)
        self.v = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="x.csv",
            column_names=[],
            input_columns=[],
            output_columns=[],
            row_count=0,
            uploaded_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="P1",
            template_text="{input_x}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.mc = ModelConfig.objects.create(
            name="M1",
            provider=Provider.LOCAL,
            model_name="llama",
            created_by=self.user,
        )

    def test_create_test_run(self):
        run = TestRun.objects.create(
            test_case_version=self.v,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            created_by=self.user,
        )
        self.assertEqual(run.status, RunStatus.PENDING)
        self.assertIsNotNone(run.id)


# --- View tests ---


class DashboardViewTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")

    def test_redirects_to_login_when_anonymous(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_shows_dashboard_when_authenticated(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LLM Evaluation Workbench")


class RegisterViewTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="existing", password="testpass123")
        self.url = reverse("core:register")

    def test_redirects_to_login_when_anonymous(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_shows_form_when_authenticated(self):
        self.client.login(username="existing", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Account")

    def test_creates_user_and_logs_in(self):
        self.client.login(username="existing", password="testpass123")
        response = self.client.post(self.url, {
            "username": "newuser",
            "password1": "strongpass99",
            "password2": "strongpass99",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="newuser").exists())

    def test_rejects_duplicate_username(self):
        self.client.login(username="existing", password="testpass123")
        response = self.client.post(self.url, {
            "username": "existing",
            "password1": "strongpass99",
            "password2": "strongpass99",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already taken")

    def test_rejects_mismatched_passwords(self):
        self.client.login(username="existing", password="testpass123")
        response = self.client.post(self.url, {
            "username": "brandnew",
            "password1": "strongpass99",
            "password2": "different99",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "do not match")


class TestCaseListViewTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")

    def test_requires_login(self):
        response = self.client.get(reverse("core:testcase_list"))
        self.assertEqual(response.status_code, 302)

    def test_shows_list_when_authenticated(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(reverse("core:testcase_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Cases")


class PromptTemplateFormViewTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.tc = TestCase.objects.create(name="My TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="data.csv",
            column_names=["input_question", "input_context", "output_answer"],
            input_columns=["input_question", "input_context"],
            output_columns=["output_answer"],
            row_count=2,
            uploaded_by=self.user,
        )
        self.client.login(username="testuser", password="testpass123")

    def test_create_view_shows_input_columns(self):
        url = reverse("core:prompttemplate_create", kwargs={"test_case_id": self.tc.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("input_columns", response.context)
        self.assertEqual(response.context["input_columns"], ["input_question", "input_context"])
        self.assertContains(response, "{input_question}")
        self.assertContains(response, "{input_context}")

    def test_create_view_no_columns_when_no_version(self):
        tc2 = TestCase.objects.create(name="Empty TC", created_by=self.user)
        url = reverse("core:prompttemplate_create", kwargs={"test_case_id": tc2.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["input_columns"], [])

    def test_edit_view_shows_input_columns(self):
        pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="My Prompt",
            template_text="{input_question}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        url = reverse("core:prompttemplate_edit", kwargs={"pk": pt.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("input_columns", response.context)
        self.assertEqual(response.context["input_columns"], ["input_question", "input_context"])
        self.assertContains(response, "{input_question}")
        self.assertContains(response, "{input_context}")


# --- LLM client URL / auth header tests ---


class LLMClientURLTests(DjangoTestCase):
    """Unit tests for _build_openai_compatible_url — no network calls needed."""

    # --- OpenAI ---

    def test_openai_no_base(self):
        url = _build_openai_compatible_url(Provider.OPENAI, "")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    def test_openai_base_without_v1(self):
        url = _build_openai_compatible_url(Provider.OPENAI, "https://api.openai.com")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    def test_openai_base_with_v1(self):
        url = _build_openai_compatible_url(Provider.OPENAI, "https://api.openai.com/v1")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    def test_openai_base_already_full(self):
        url = _build_openai_compatible_url(Provider.OPENAI, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    def test_openai_trailing_slash_stripped(self):
        url = _build_openai_compatible_url(Provider.OPENAI, "https://api.openai.com/v1/")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    # --- Azure OpenAI (classic deployment) ---

    def test_azure_openai_deployment_url(self):
        base = "https://myresource.openai.azure.com/openai/deployments/my-gpt4"
        url = _build_openai_compatible_url(Provider.AZURE_OPENAI, base)
        self.assertEqual(url, f"{base}/chat/completions")

    def test_azure_openai_already_full(self):
        full = "https://myresource.openai.azure.com/openai/deployments/my-gpt4/chat/completions"
        url = _build_openai_compatible_url(Provider.AZURE_OPENAI, full)
        self.assertEqual(url, full)

    def test_azure_openai_trailing_slash_stripped(self):
        base = "https://myresource.openai.azure.com/openai/deployments/my-gpt4/"
        url = _build_openai_compatible_url(Provider.AZURE_OPENAI, base)
        self.assertEqual(url, "https://myresource.openai.azure.com/openai/deployments/my-gpt4/chat/completions")

    # --- Azure AI Foundry ---

    def test_azure_foundry_resource_root(self):
        base = "https://myresource.openai.azure.com"
        url = _build_openai_compatible_url(Provider.AZURE_AI_FOUNDRY, base)
        self.assertEqual(url, "https://myresource.openai.azure.com/openai/v1/chat/completions")

    def test_azure_foundry_cognitive_services_root(self):
        base = "https://westus.api.cognitive.microsoft.com"
        url = _build_openai_compatible_url(Provider.AZURE_AI_FOUNDRY, base)
        self.assertEqual(url, "https://westus.api.cognitive.microsoft.com/openai/v1/chat/completions")

    def test_azure_foundry_with_openai_v1_suffix(self):
        base = "https://myresource.openai.azure.com/openai/v1"
        url = _build_openai_compatible_url(Provider.AZURE_AI_FOUNDRY, base)
        self.assertEqual(url, "https://myresource.openai.azure.com/openai/v1/chat/completions")

    def test_azure_foundry_with_openai_suffix(self):
        base = "https://myresource.openai.azure.com/openai"
        url = _build_openai_compatible_url(Provider.AZURE_AI_FOUNDRY, base)
        self.assertEqual(url, "https://myresource.openai.azure.com/openai/v1/chat/completions")

    def test_azure_foundry_already_full(self):
        full = "https://myresource.openai.azure.com/openai/v1/chat/completions"
        url = _build_openai_compatible_url(Provider.AZURE_AI_FOUNDRY, full)
        self.assertEqual(url, full)

    # --- vLLM ---

    def test_vllm_host_only(self):
        url = _build_openai_compatible_url(Provider.VLLM, "http://localhost:8000")
        self.assertEqual(url, "http://localhost:8000/v1/chat/completions")

    def test_vllm_with_v1_base(self):
        url = _build_openai_compatible_url(Provider.VLLM, "http://localhost:8000/v1")
        self.assertEqual(url, "http://localhost:8000/v1/chat/completions")

    def test_vllm_remote_host(self):
        url = _build_openai_compatible_url(Provider.VLLM, "https://vllm.internal:8080")
        self.assertEqual(url, "https://vllm.internal:8080/v1/chat/completions")

    def test_vllm_trailing_slash(self):
        url = _build_openai_compatible_url(Provider.VLLM, "http://localhost:8000/")
        self.assertEqual(url, "http://localhost:8000/v1/chat/completions")

    # --- Local / Custom ---

    def test_local_ollama_style(self):
        url = _build_openai_compatible_url(Provider.LOCAL, "http://localhost:11434")
        self.assertEqual(url, "http://localhost:11434/v1/chat/completions")

    def test_custom_with_v1(self):
        url = _build_openai_compatible_url(Provider.CUSTOM, "http://myserver/v1")
        self.assertEqual(url, "http://myserver/v1/chat/completions")


class LLMClientAuthHeaderTests(DjangoTestCase):
    """Unit tests for _build_auth_headers."""

    def test_openai_uses_bearer(self):
        headers = _build_auth_headers(Provider.OPENAI, "sk-abc")
        self.assertEqual(headers["Authorization"], "Bearer sk-abc")
        self.assertNotIn("api-key", headers)

    def test_azure_openai_uses_api_key_header(self):
        headers = _build_auth_headers(Provider.AZURE_OPENAI, "my-azure-key")
        self.assertEqual(headers["api-key"], "my-azure-key")
        self.assertNotIn("Authorization", headers)

    def test_azure_foundry_uses_api_key_header(self):
        headers = _build_auth_headers(Provider.AZURE_AI_FOUNDRY, "my-foundry-key")
        self.assertEqual(headers["api-key"], "my-foundry-key")
        self.assertNotIn("Authorization", headers)

    def test_vllm_uses_bearer(self):
        headers = _build_auth_headers(Provider.VLLM, "token-123")
        self.assertEqual(headers["Authorization"], "Bearer token-123")
        self.assertNotIn("api-key", headers)

    def test_local_uses_bearer(self):
        headers = _build_auth_headers(Provider.LOCAL, "ollama-key")
        self.assertEqual(headers["Authorization"], "Bearer ollama-key")

    def test_no_key_omits_auth(self):
        headers = _build_auth_headers(Provider.OPENAI, "")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("api-key", headers)

    def test_content_type_always_set(self):
        for provider in Provider.values:
            with self.subTest(provider=provider):
                headers = _build_auth_headers(provider, "key")
                self.assertEqual(headers["Content-Type"], "application/json")


# --- Export view tests ---


class _ExportFixtureMixin:
    """Shared setup for export view tests."""

    def _make_fixtures(self):
        self.user = User.objects.create_user(username="exportuser", password="testpass123")
        self.tc = TestCase.objects.create(name="Export TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="data.csv",
            column_names=["input_text", "output_label"],
            input_columns=["input_text"],
            output_columns=["output_label"],
            row_count=2,
            uploaded_by=self.user,
        )
        self.row1 = TestCaseRow.objects.create(
            version=self.version,
            row_number=1,
            input_fields={"input_text": "hello"},
            expected_output_fields={"output_label": "pos"},
        )
        self.row2 = TestCaseRow.objects.create(
            version=self.version,
            row_number=2,
            input_fields={"input_text": "goodbye"},
            expected_output_fields={"output_label": "neg"},
        )
        self.mc = ModelConfig.objects.create(
            name="M1", provider=Provider.LOCAL, model_name="llama", created_by=self.user
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="P1",
            template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.run = TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            rows_total=2,
            created_by=self.user,
        )
        self.result1 = TestRunResult.objects.create(
            test_run=self.run,
            test_case_row=self.row1,
            prompt_sent="hello",
            raw_response="positive",
            status="success",
            latency_ms=100,
            input_tokens=5,
            output_tokens=3,
        )
        self.result2 = TestRunResult.objects.create(
            test_run=self.run,
            test_case_row=self.row2,
            prompt_sent="goodbye",
            raw_response="negative",
            status="success",
            latency_ms=120,
            input_tokens=5,
            output_tokens=3,
        )


class ExportTestRunViewTests(_ExportFixtureMixin, DjangoTestCase):
    def setUp(self):
        self._make_fixtures()
        self.client.login(username="exportuser", password="testpass123")

    def test_requires_login(self):
        self.client.logout()
        url = reverse("core:testrun_export", kwargs={"pk": self.run.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_returns_csv_attachment(self):
        url = reverse("core:testrun_export", kwargs={"pk": self.run.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])

    def test_csv_header_contains_required_columns(self):
        url = reverse("core:testrun_export", kwargs={"pk": self.run.pk})
        response = self.client.get(url)
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        self.assertIn("test_run_id", header)
        self.assertIn("model", header)
        self.assertIn("prompt_template", header)
        self.assertIn("input_input_text", header)
        self.assertIn("expected_output_label", header)
        self.assertIn("prompt_sent", header)
        self.assertIn("raw_response", header)
        self.assertIn("status", header)

    def test_csv_row_count_matches_results(self):
        url = reverse("core:testrun_export", kwargs={"pk": self.run.pk})
        response = self.client.get(url)
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        # 1 header + 2 data rows
        self.assertEqual(len(rows), 3)

    def test_csv_data_values_present(self):
        url = reverse("core:testrun_export", kwargs={"pk": self.run.pk})
        response = self.client.get(url)
        content = response.content.decode("utf-8")
        self.assertIn("hello", content)
        self.assertIn("positive", content)
        self.assertIn("M1", content)


class ExportEvaluationRunViewTests(_ExportFixtureMixin, DjangoTestCase):
    def setUp(self):
        self._make_fixtures()
        self.client.login(username="exportuser", password="testpass123")
        self.eval_config = EvaluationConfig.objects.create(
            test_case=self.tc,
            name="KW Check",
            eval_type=EvalType.KEYWORD_MATCH,
            scoring_criteria={
                "checks": [{"name": "has_pos", "type": "contains_phrase", "phrase": "pos"}]
            },
            created_by=self.user,
        )
        self.eval_run = EvaluationRun.objects.create(
            evaluation_config=self.eval_config,
            test_run=self.run,
            created_by=self.user,
        )
        EvaluationResult.objects.create(
            evaluation_run=self.eval_run,
            test_run_result=self.result1,
            assessor_type=AssessorType.AI,
            assessor_id="keyword_match",
            assessment={"has_pos": True},
        )
        EvaluationResult.objects.create(
            evaluation_run=self.eval_run,
            test_run_result=self.result2,
            assessor_type=AssessorType.AI,
            assessor_id="keyword_match",
            assessment={"has_pos": False},
        )

    def test_requires_login(self):
        self.client.logout()
        url = reverse("core:evaluationrun_export", kwargs={"pk": self.eval_run.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_returns_csv_attachment(self):
        url = reverse("core:evaluationrun_export", kwargs={"pk": self.eval_run.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment", response["Content-Disposition"])

    def test_csv_header_contains_eval_columns(self):
        url = reverse("core:evaluationrun_export", kwargs={"pk": self.eval_run.pk})
        response = self.client.get(url)
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        self.assertIn("eval_config", header)
        self.assertIn("eval_type", header)
        self.assertIn("eval_has_pos", header)
        self.assertIn("eval_notes", header)
        self.assertIn("judge_prompt_sent", header)
        self.assertIn("raw_judge_response", header)

    def test_csv_row_count_matches_eval_results(self):
        url = reverse("core:evaluationrun_export", kwargs={"pk": self.eval_run.pk})
        response = self.client.get(url)
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        self.assertEqual(len(rows), 3)

    def test_csv_assessment_values_present(self):
        url = reverse("core:evaluationrun_export", kwargs={"pk": self.eval_run.pk})
        response = self.client.get(url)
        content = response.content.decode("utf-8")
        self.assertIn("True", content)
        self.assertIn("False", content)
        self.assertIn("KW Check", content)


# --- Sensitivity / Specificity tests ---


class GroundTruthPositiveTests(DjangoTestCase):
    """Unit tests for the _ground_truth_positive helper."""

    def test_bool_true(self):
        self.assertTrue(_ground_truth_positive(True))

    def test_bool_false(self):
        self.assertFalse(_ground_truth_positive(False))

    def test_int_one(self):
        self.assertTrue(_ground_truth_positive(1))

    def test_int_zero(self):
        self.assertFalse(_ground_truth_positive(0))

    def test_string_true(self):
        self.assertTrue(_ground_truth_positive("true"))
        self.assertTrue(_ground_truth_positive("True"))
        self.assertTrue(_ground_truth_positive("yes"))
        self.assertTrue(_ground_truth_positive("positive"))
        self.assertTrue(_ground_truth_positive("present"))

    def test_string_false(self):
        self.assertFalse(_ground_truth_positive("false"))
        self.assertFalse(_ground_truth_positive("no"))
        self.assertFalse(_ground_truth_positive("0"))
        self.assertFalse(_ground_truth_positive("negative"))
        self.assertFalse(_ground_truth_positive("absent"))
        self.assertFalse(_ground_truth_positive(""))

    def test_none_is_false(self):
        self.assertFalse(_ground_truth_positive(None))

    def test_nonempty_string_is_positive(self):
        self.assertTrue(_ground_truth_positive("diabetes"))
        self.assertTrue(_ground_truth_positive("E11.9"))

    def test_nonzero_numbers_are_positive(self):
        self.assertTrue(_ground_truth_positive(2))
        self.assertTrue(_ground_truth_positive(3))
        self.assertTrue(_ground_truth_positive(0.5))
        self.assertFalse(_ground_truth_positive(0))
        self.assertFalse(_ground_truth_positive(0.0))

    def test_numeric_strings_are_positive(self):
        self.assertTrue(_ground_truth_positive("2"))
        self.assertTrue(_ground_truth_positive("3"))


class ComputeSensSpecTests(DjangoTestCase):
    """Tests for compute_sens_spec()."""

    def setUp(self):
        self.user = User.objects.create_user(username="ssuser", password="testpass123")
        self.tc = TestCase.objects.create(name="SS TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="data.csv",
            column_names=["input_text", "output_has_condition"],
            input_columns=["input_text"],
            output_columns=["output_has_condition"],
            row_count=4,
            uploaded_by=self.user,
        )
        # 4 rows: TP, FP, TN, FN
        self.row_tp = TestCaseRow.objects.create(
            version=self.version, row_number=1,
            input_fields={"input_text": "patient has diabetes"},
            expected_output_fields={"output_has_condition": "true"},
        )
        self.row_fp = TestCaseRow.objects.create(
            version=self.version, row_number=2,
            input_fields={"input_text": "patient is healthy"},
            expected_output_fields={"output_has_condition": "false"},
        )
        self.row_tn = TestCaseRow.objects.create(
            version=self.version, row_number=3,
            input_fields={"input_text": "no conditions"},
            expected_output_fields={"output_has_condition": "false"},
        )
        self.row_fn = TestCaseRow.objects.create(
            version=self.version, row_number=4,
            input_fields={"input_text": "patient has hypertension"},
            expected_output_fields={"output_has_condition": "true"},
        )
        self.mc = ModelConfig.objects.create(
            name="M1", provider=Provider.LOCAL, model_name="llama", created_by=self.user
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc, name="P1", template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT, created_by=self.user,
        )
        self.run = TestRun.objects.create(
            test_case_version=self.version, prompt_template=self.pt,
            model_config=self.mc, prompt_snapshot=self.pt.template_text,
            rows_total=4, created_by=self.user,
        )
        self.result_tp = TestRunResult.objects.create(
            test_run=self.run, test_case_row=self.row_tp,
            prompt_sent="p", raw_response="yes", status="success",
        )
        self.result_fp = TestRunResult.objects.create(
            test_run=self.run, test_case_row=self.row_fp,
            prompt_sent="p", raw_response="yes", status="success",
        )
        self.result_tn = TestRunResult.objects.create(
            test_run=self.run, test_case_row=self.row_tn,
            prompt_sent="p", raw_response="no", status="success",
        )
        self.result_fn = TestRunResult.objects.create(
            test_run=self.run, test_case_row=self.row_fn,
            prompt_sent="p", raw_response="no", status="success",
        )

        self.eval_config = EvaluationConfig.objects.create(
            test_case=self.tc,
            name="Condition Check",
            eval_type=EvalType.KEYWORD_MATCH,
            scoring_criteria={
                "checks": [{
                    "name": "has_condition",
                    "type": "contains_phrase",
                    "phrase": "yes",
                    "sens_spec": True,
                    "expected_output_column": "output_has_condition",
                }]
            },
            created_by=self.user,
        )
        self.eval_run = EvaluationRun.objects.create(
            evaluation_config=self.eval_config,
            test_run=self.run,
            created_by=self.user,
        )
        # TP: ground_truth=positive, eval passed=True (LLM correctly identified positive)
        EvaluationResult.objects.create(
            evaluation_run=self.eval_run, test_run_result=self.result_tp,
            assessor_type=AssessorType.AI, assessor_id="keyword_match",
            assessment={"has_condition": True},
        )
        # TN: ground_truth=negative, eval passed=True (LLM correctly identified negative)
        EvaluationResult.objects.create(
            evaluation_run=self.eval_run, test_run_result=self.result_fp,
            assessor_type=AssessorType.AI, assessor_id="keyword_match",
            assessment={"has_condition": True},
        )
        # FP: ground_truth=negative, eval passed=False (LLM got the negative wrong)
        EvaluationResult.objects.create(
            evaluation_run=self.eval_run, test_run_result=self.result_tn,
            assessor_type=AssessorType.AI, assessor_id="keyword_match",
            assessment={"has_condition": False},
        )
        # FN: ground_truth=positive, eval passed=False (LLM missed the positive)
        EvaluationResult.objects.create(
            evaluation_run=self.eval_run, test_run_result=self.result_fn,
            assessor_type=AssessorType.AI, assessor_id="keyword_match",
            assessment={"has_condition": False},
        )

    def test_returns_list_with_one_entry(self):
        result = compute_sens_spec(self.eval_run)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)

    def test_check_name_and_column(self):
        result = compute_sens_spec(self.eval_run)
        entry = result[0]
        self.assertEqual(entry["name"], "has_condition")
        self.assertEqual(entry["expected_output_column"], "output_has_condition")

    def test_confusion_matrix_counts(self):
        result = compute_sens_spec(self.eval_run)
        entry = result[0]
        # result_tp: ground=positive, eval=True  → TP
        # result_fp: ground=negative, eval=True  → TN
        # result_tn: ground=negative, eval=False → FP
        # result_fn: ground=positive, eval=False → FN
        self.assertEqual(entry["tp"], 1)
        self.assertEqual(entry["fp"], 1)
        self.assertEqual(entry["tn"], 1)
        self.assertEqual(entry["fn"], 1)
        self.assertEqual(entry["total"], 4)

    def test_sensitivity(self):
        # TP=1, FN=1 → sensitivity = 0.5
        result = compute_sens_spec(self.eval_run)
        self.assertEqual(result[0]["sensitivity"], 0.5)

    def test_specificity(self):
        # TN=1, FP=1 → specificity = 0.5
        result = compute_sens_spec(self.eval_run)
        self.assertEqual(result[0]["specificity"], 0.5)

    def test_ppv(self):
        # TP=1, FP=1 → PPV = 0.5
        result = compute_sens_spec(self.eval_run)
        self.assertEqual(result[0]["ppv"], 0.5)

    def test_npv(self):
        # TN=1, FN=1 → NPV = 0.5
        result = compute_sens_spec(self.eval_run)
        self.assertEqual(result[0]["npv"], 0.5)

    def test_returns_none_when_no_flagged_checks(self):
        config_no_flag = EvaluationConfig.objects.create(
            test_case=self.tc,
            name="No Flag",
            eval_type=EvalType.KEYWORD_MATCH,
            scoring_criteria={"checks": [{"name": "x", "type": "contains_phrase", "phrase": "y"}]},
            created_by=self.user,
        )
        run_no_flag = EvaluationRun.objects.create(
            evaluation_config=config_no_flag, test_run=self.run, created_by=self.user
        )
        EvaluationResult.objects.create(
            evaluation_run=run_no_flag, test_run_result=self.result_tp,
            assessor_type=AssessorType.AI, assessor_id="keyword_match",
            assessment={"x": True},
        )
        self.assertIsNone(compute_sens_spec(run_no_flag))

    def test_perfect_sensitivity(self):
        """All positive cases are correctly identified → sensitivity = 1.0."""
        config = EvaluationConfig.objects.create(
            test_case=self.tc,
            name="Perfect Sens",
            eval_type=EvalType.KEYWORD_MATCH,
            scoring_criteria={"checks": [{
                "name": "c", "type": "contains_phrase", "phrase": "yes",
                "sens_spec": True, "expected_output_column": "output_has_condition",
            }]},
            created_by=self.user,
        )
        eval_run = EvaluationRun.objects.create(
            evaluation_config=config, test_run=self.run, created_by=self.user
        )
        # Both positives pass, both negatives fail
        EvaluationResult.objects.create(
            evaluation_run=eval_run, test_run_result=self.result_tp,
            assessor_type=AssessorType.AI, assessor_id="kw",
            assessment={"c": True},
        )
        # result_fp: ground=negative, eval=True → TN (correctly identified negative)
        EvaluationResult.objects.create(
            evaluation_run=eval_run, test_run_result=self.result_fp,
            assessor_type=AssessorType.AI, assessor_id="kw",
            assessment={"c": True},
        )
        # result_tn: ground=negative, eval=True → TN (correctly identified negative)
        EvaluationResult.objects.create(
            evaluation_run=eval_run, test_run_result=self.result_tn,
            assessor_type=AssessorType.AI, assessor_id="kw",
            assessment={"c": True},
        )
        # result_fn: ground=positive, eval=True → TP (correctly identified positive)
        EvaluationResult.objects.create(
            evaluation_run=eval_run, test_run_result=self.result_fn,
            assessor_type=AssessorType.AI, assessor_id="kw",
            assessment={"c": True},
        )
        result = compute_sens_spec(eval_run)
        self.assertEqual(result[0]["sensitivity"], 1.0)
        self.assertEqual(result[0]["specificity"], 1.0)
class StripThinkTagsTests(DjangoTestCase):
    """Unit tests for _strip_think_tags (thinking model output)."""

    def test_passthrough_when_no_think_tags(self):
        text = "Here is my answer."
        self.assertEqual(_strip_think_tags(text), "Here is my answer.")

    def test_strips_single_think_block(self):
        text = "<think>Let me reason step by step...</think>\n\nHere is my answer."
        self.assertEqual(_strip_think_tags(text), "Here is my answer.")

    def test_strips_think_block_at_start(self):
        text = "<think>thinking content</think>\n\nActual response."
        self.assertEqual(_strip_think_tags(text), "Actual response.")

    def test_strips_think_block_at_end(self):
        text = "Actual response.\n\n<think>more thinking</think>"
        self.assertEqual(_strip_think_tags(text), "Actual response.")

    def test_strips_multiline_think_block(self):
        text = "<think>line1\nline2\nline3</think>\n\nAnswer"
        self.assertEqual(_strip_think_tags(text), "Answer")

    def test_strips_multiple_think_blocks(self):
        text = "<think>first</think>\n\nMiddle\n\n<think>second</think>\n\nEnd"
        result = _strip_think_tags(text)
        self.assertIn("Middle", result)
        self.assertIn("End", result)
        self.assertNotIn("<think>", result)

    def test_empty_after_strip_returns_empty(self):
        text = "<think>only thinking</think>"
        self.assertEqual(_strip_think_tags(text), "")

    def test_empty_input_unchanged(self):
        self.assertEqual(_strip_think_tags(""), "")


# --- Prompt template versioning tests ---


class PromptTemplateVersioningTests(DjangoTestCase):
    """Tests for PromptTemplate version history and the create-new-version view."""

    def setUp(self):
        self.user = User.objects.create_user(username="ptuser", password="testpass123")
        self.tc = TestCase.objects.create(name="Version TC", created_by=self.user)
        self.client.login(username="ptuser", password="testpass123")

    def _make_pt(self, name="My Prompt", version=1, parent=None):
        return PromptTemplate.objects.create(
            test_case=self.tc,
            name=name,
            version_number=version,
            parent_template=parent,
            template_text=f"v{version} template",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )

    # --- model property ---

    def test_is_latest_single_version(self):
        pt = self._make_pt(version=1)
        self.assertTrue(pt.is_latest)

    def test_is_latest_highest_version(self):
        v1 = self._make_pt(version=1)
        v2 = self._make_pt(version=2, parent=v1)
        self.assertFalse(v1.is_latest)
        self.assertTrue(v2.is_latest)

    def test_str_includes_version(self):
        pt = self._make_pt(version=3)
        self.assertIn("v3", str(pt))

    # --- edit view creates new version ---

    def test_edit_view_get_returns_200(self):
        pt = self._make_pt(version=1)
        url = reverse("core:prompttemplate_edit", kwargs={"pk": pt.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_edit_post_creates_new_version(self):
        pt = self._make_pt(version=1)
        url = reverse("core:prompttemplate_edit", kwargs={"pk": pt.pk})
        response = self.client.post(url, {
            "name": "My Prompt",
            "template_text": "updated template text",
            "response_format": ResponseFormat.FREE_TEXT,
        })
        self.assertEqual(response.status_code, 302)
        versions = PromptTemplate.objects.filter(test_case=self.tc, name="My Prompt").order_by("version_number")
        self.assertEqual(versions.count(), 2)
        self.assertEqual(versions.last().version_number, 2)
        self.assertEqual(versions.last().template_text, "updated template text")

    def test_edit_post_preserves_original(self):
        pt = self._make_pt(version=1)
        url = reverse("core:prompttemplate_edit", kwargs={"pk": pt.pk})
        self.client.post(url, {
            "name": "My Prompt",
            "template_text": "updated",
            "response_format": ResponseFormat.FREE_TEXT,
        })
        pt.refresh_from_db()
        self.assertEqual(pt.template_text, "v1 template")

    def test_edit_post_sets_parent_template(self):
        pt = self._make_pt(version=1)
        url = reverse("core:prompttemplate_edit", kwargs={"pk": pt.pk})
        self.client.post(url, {
            "name": "My Prompt",
            "template_text": "v2 text",
            "response_format": ResponseFormat.FREE_TEXT,
        })
        v2 = PromptTemplate.objects.get(test_case=self.tc, name="My Prompt", version_number=2)
        self.assertEqual(v2.parent_template, pt)

    def test_edit_post_increments_version_on_repeated_edits(self):
        pt = self._make_pt(version=1)
        edit_url = reverse("core:prompttemplate_edit", kwargs={"pk": pt.pk})
        self.client.post(edit_url, {"name": "My Prompt", "template_text": "v2", "response_format": ResponseFormat.FREE_TEXT})
        v2 = PromptTemplate.objects.get(test_case=self.tc, name="My Prompt", version_number=2)
        edit_url2 = reverse("core:prompttemplate_edit", kwargs={"pk": v2.pk})
        self.client.post(edit_url2, {"name": "My Prompt", "template_text": "v3", "response_format": ResponseFormat.FREE_TEXT})
        self.assertEqual(PromptTemplate.objects.filter(test_case=self.tc, name="My Prompt").count(), 3)
        self.assertTrue(PromptTemplate.objects.filter(test_case=self.tc, name="My Prompt", version_number=3).exists())

    def test_delete_view_removes_version(self):
        pt = self._make_pt(version=1)
        url = reverse("core:prompttemplate_delete", kwargs={"pk": pt.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PromptTemplate.objects.filter(pk=pt.pk).exists())

    # --- testcase_detail shows grouped templates ---

    def test_testcase_detail_context_has_groups(self):
        self._make_pt(name="Prompt A", version=1)
        self._make_pt(name="Prompt A", version=2)
        self._make_pt(name="Prompt B", version=1)
        url = reverse("core:testcase_detail", kwargs={"pk": self.tc.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        groups = response.context["prompt_template_groups"]
        self.assertEqual(len(groups), 2)
        group_a = next(g for g in groups if g["latest"].name == "Prompt A")
        self.assertEqual(group_a["latest"].version_number, 2)
        self.assertEqual(len(group_a["older"]), 1)
        self.assertEqual(group_a["older"][0].version_number, 1)

    def test_testcase_detail_single_version_no_older(self):
        self._make_pt(name="Prompt A", version=1)
        url = reverse("core:testcase_detail", kwargs={"pk": self.tc.pk})
        response = self.client.get(url)
        groups = response.context["prompt_template_groups"]
        self.assertEqual(len(groups[0]["older"]), 0)


# --- Run list grouping tests ---


class TestRunListGroupingTests(DjangoTestCase):
    """Tests for the run list view grouping logic."""

    def setUp(self):
        self.user = User.objects.create_user(username="runuser", password="testpass123")
        self.tc = TestCase.objects.create(name="Group TC", created_by=self.user)
        self.v1 = TestCaseVersion.objects.create(
            test_case=self.tc, version_number=1, original_filename="v1.csv",
            column_names=[], input_columns=[], output_columns=[], row_count=0,
            uploaded_by=self.user,
        )
        self.v2 = TestCaseVersion.objects.create(
            test_case=self.tc, version_number=2, original_filename="v2.csv",
            column_names=[], input_columns=[], output_columns=[], row_count=0,
            uploaded_by=self.user,
        )
        self.mc = ModelConfig.objects.create(
            name="M1", provider=Provider.LOCAL, model_name="llama", created_by=self.user
        )
        self.pt1 = PromptTemplate.objects.create(
            test_case=self.tc, name="Phrase 1", version_number=1,
            template_text="t", response_format=ResponseFormat.FREE_TEXT, created_by=self.user,
        )
        self.pt2 = PromptTemplate.objects.create(
            test_case=self.tc, name="Phrase 2", version_number=1,
            template_text="t", response_format=ResponseFormat.FREE_TEXT, created_by=self.user,
        )
        self.client.login(username="runuser", password="testpass123")

    def _make_run(self, version, prompt_template):
        return TestRun.objects.create(
            test_case_version=version,
            prompt_template=prompt_template,
            model_config=self.mc,
            prompt_snapshot="t",
            created_by=self.user,
        )

    def test_run_list_has_run_groups_context(self):
        self._make_run(self.v1, self.pt1)
        response = self.client.get(reverse("core:testrun_list"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("run_groups", response.context)

    def test_latest_version_in_latest_bucket(self):
        from core.views.runs import _group_test_runs
        run_v1 = self._make_run(self.v1, self.pt1)
        run_v2 = self._make_run(self.v2, self.pt1)
        runs = TestRun.objects.select_related(
            "prompt_template", "model_config", "test_case_version__test_case"
        ).order_by("-created_at")
        groups = _group_test_runs(runs)
        self.assertEqual(len(groups), 1)
        self.assertIn(run_v2, groups[0]["latest"])
        self.assertIn(run_v1, groups[0]["older"])

    def test_multiple_prompts_produce_separate_groups(self):
        from core.views.runs import _group_test_runs
        self._make_run(self.v2, self.pt1)
        self._make_run(self.v2, self.pt2)
        runs = TestRun.objects.select_related(
            "prompt_template", "model_config", "test_case_version__test_case"
        ).order_by("-created_at")
        groups = _group_test_runs(runs)
        self.assertEqual(len(groups), 2)

    def test_only_latest_version_no_older(self):
        from core.views.runs import _group_test_runs
        self._make_run(self.v2, self.pt1)
        runs = TestRun.objects.select_related(
            "prompt_template", "model_config", "test_case_version__test_case"
        ).order_by("-created_at")
        groups = _group_test_runs(runs)
        self.assertEqual(len(groups[0]["older"]), 0)

    def test_run_list_template_renders_group_header(self):
        self._make_run(self.v2, self.pt1)
        response = self.client.get(reverse("core:testrun_list"))
        self.assertContains(response, "Group TC")
        self.assertContains(response, "Phrase 1")


# ---------------------------------------------------------------------------
# Python eval — tool_runner unit tests
# ---------------------------------------------------------------------------

from core.services.tool_runner import run_python_eval


class RunPythonEvalTests(DjangoTestCase):
    """Unit tests for the run_python_eval helper."""

    BASE_LOCALS = {
        "input_fields": {"input_text": "hello"},
        "expected_output_fields": {"output_label": "positive"},
        "raw_response": "positive",
        "response_parsed": None,
    }

    def test_simple_boolean_result(self):
        script = "result = {'correct': True}"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertEqual(outcome, {"correct": True})

    def test_uses_row_locals(self):
        script = (
            "expected = expected_output_fields.get('output_label', '')\n"
            "result = {'correct': raw_response.strip() == expected}"
        )
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertEqual(outcome, {"correct": True})

    def test_json_module_available(self):
        script = "result = {'parsed': isinstance(json.loads('{\"k\": 1}'), dict)}"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertEqual(outcome, {"parsed": True})

    def test_math_module_available(self):
        script = "result = {'pi_floor': int(math.floor(math.pi))}"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertEqual(outcome, {"pi_floor": 3})

    def test_re_module_available(self):
        script = "result = {'has_digit': bool(re.search(r'\\d', raw_response))}"
        locals_ = {**self.BASE_LOCALS, "raw_response": "answer42"}
        outcome = run_python_eval(script, locals_)
        self.assertEqual(outcome, {"has_digit": True})

    def test_missing_result_variable_returns_error_string(self):
        script = "x = 1"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertIsInstance(outcome, str)
        self.assertIn("result", outcome)

    def test_result_not_dict_returns_error_string(self):
        script = "result = 'oops'"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertIsInstance(outcome, str)
        self.assertIn("dict", outcome)

    def test_exception_in_script_returns_error_string(self):
        script = "raise ValueError('something went wrong')"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertIsInstance(outcome, str)
        self.assertIn("something went wrong", outcome)

    def test_forbidden_import_raises_error(self):
        script = "import os; result = {'bad': True}"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertIsInstance(outcome, str)

    def test_response_parsed_passed_through(self):
        script = "result = {'got_parsed': response_parsed is not None and response_parsed.get('k') == 1}"
        locals_ = {**self.BASE_LOCALS, "response_parsed": {"k": 1}}
        outcome = run_python_eval(script, locals_)
        self.assertEqual(outcome, {"got_parsed": True})

    def test_mixed_value_types(self):
        script = "result = {'flag': True, 'score': 7, 'note': 'ok'}"
        outcome = run_python_eval(script, dict(self.BASE_LOCALS))
        self.assertEqual(outcome, {"flag": True, "score": 7, "note": "ok"})


# ---------------------------------------------------------------------------
# Python eval — compute_accuracy integration tests
# ---------------------------------------------------------------------------

from core.views.evaluations import compute_accuracy


class PythonEvalComputeAccuracyTests(DjangoTestCase):
    """compute_accuracy should work for python_eval runs."""

    def setUp(self):
        self.user = User.objects.create_user(username="pyaccuser", password="testpass123")
        self.tc = TestCase.objects.create(name="PyAcc TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc, version_number=1, original_filename="d.csv",
            column_names=["input_text"], input_columns=["input_text"],
            output_columns=[], row_count=2, uploaded_by=self.user,
        )
        self.row1 = TestCaseRow.objects.create(
            version=self.version, row_number=1,
            input_fields={"input_text": "a"}, expected_output_fields={},
        )
        self.row2 = TestCaseRow.objects.create(
            version=self.version, row_number=2,
            input_fields={"input_text": "b"}, expected_output_fields={},
        )
        self.mc = ModelConfig.objects.create(
            name="m", provider=Provider.LOCAL, model_name="m", created_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc, name="p", version_number=1,
            template_text="t", response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.run = TestRun.objects.create(
            test_case_version=self.version, prompt_template=self.pt,
            model_config=self.mc, prompt_snapshot="t", created_by=self.user,
        )
        self.rr1 = TestRunResult.objects.create(
            test_run=self.run, test_case_row=self.row1,
            prompt_sent="p", raw_response="r",
        )
        self.rr2 = TestRunResult.objects.create(
            test_run=self.run, test_case_row=self.row2,
            prompt_sent="p", raw_response="r",
        )
        self.eval_config = EvaluationConfig.objects.create(
            test_case=self.tc, name="py cfg",
            eval_type=EvalType.PYTHON_EVAL,
            scoring_criteria={
                "script": "result = {'correct': True}",
                "output_fields": [{"name": "correct", "type": "boolean"}],
            },
            created_by=self.user,
        )
        self.eval_run = EvaluationRun.objects.create(
            evaluation_config=self.eval_config, test_run=self.run,
            created_by=self.user,
        )

    def _add_result(self, run_result, assessment):
        EvaluationResult.objects.create(
            evaluation_run=self.eval_run,
            test_run_result=run_result,
            assessor_type=AssessorType.AI,
            assessor_id="python_eval",
            assessment=assessment,
        )

    def test_all_correct(self):
        self._add_result(self.rr1, {"correct": True})
        self._add_result(self.rr2, {"correct": True})
        acc = compute_accuracy(self.eval_run)
        self.assertIsNotNone(acc)
        self.assertEqual(acc["correct"], 2)
        self.assertEqual(acc["total"], 2)
        self.assertEqual(acc["pct"], 100.0)

    def test_partial_correct(self):
        self._add_result(self.rr1, {"correct": True})
        self._add_result(self.rr2, {"correct": False})
        acc = compute_accuracy(self.eval_run)
        self.assertEqual(acc["correct"], 1)
        self.assertEqual(acc["total"], 2)

    def test_no_declared_fields_falls_back_to_inferred(self):
        self.eval_config.scoring_criteria = {"script": "result = {'ok': True}"}
        self.eval_config.save()
        self._add_result(self.rr1, {"ok": True})
        self._add_result(self.rr2, {"ok": False})
        acc = compute_accuracy(self.eval_run)
        self.assertIsNotNone(acc)
        self.assertEqual(acc["total"], 2)


# ---------------------------------------------------------------------------
# Cancel test run view
# ---------------------------------------------------------------------------


class CancelTestRunViewTests(DjangoTestCase):
    """Tests for the cancel endpoint and task-level cancellation check."""

    def setUp(self):
        self.user = User.objects.create_user(username="canceluser", password="testpass123")
        self.tc = TestCase.objects.create(name="Cancel TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="cancel.csv",
            column_names=["input_text"],
            input_columns=["input_text"],
            output_columns=[],
            row_count=1,
            uploaded_by=self.user,
        )
        self.mc = ModelConfig.objects.create(
            name="Cancel MC",
            provider=Provider.LOCAL,
            model_name="llama",
            created_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="Cancel PT",
            version_number=1,
            template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.client.login(username="canceluser", password="testpass123")

    def _make_run(self, status=RunStatus.RUNNING):
        return TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            status=status,
            created_by=self.user,
        )

    def test_cancel_running_run_sets_status_cancelled(self):
        run = self._make_run(RunStatus.RUNNING)
        response = self.client.post(reverse("core:testrun_cancel", kwargs={"pk": run.pk}))
        self.assertRedirects(response, reverse("core:testrun_detail", kwargs={"pk": run.pk}))
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)

    def test_cancel_pending_run_sets_status_cancelled(self):
        run = self._make_run(RunStatus.PENDING)
        self.client.post(reverse("core:testrun_cancel", kwargs={"pk": run.pk}))
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)

    def test_cancel_already_completed_run_does_not_change_status(self):
        run = self._make_run(RunStatus.COMPLETED)
        self.client.post(reverse("core:testrun_cancel", kwargs={"pk": run.pk}))
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)

    def test_cancel_requires_login(self):
        self.client.logout()
        run = self._make_run(RunStatus.RUNNING)
        response = self.client.post(reverse("core:testrun_cancel", kwargs={"pk": run.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("cancel", response["Location"])
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.RUNNING)

    def test_cancel_get_not_allowed(self):
        run = self._make_run(RunStatus.RUNNING)
        response = self.client.get(reverse("core:testrun_cancel", kwargs={"pk": run.pk}))
        self.assertEqual(response.status_code, 405)


# ---------------------------------------------------------------------------
# TestRunStatusView — JSON polling endpoint
# ---------------------------------------------------------------------------


class TestRunStatusViewTests(DjangoTestCase):
    """Tests for the /runs/<pk>/status/ JSON endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username="statususer", password="testpass123")
        self.tc = TestCase.objects.create(name="Status TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="s.csv",
            column_names=["input_text"],
            input_columns=["input_text"],
            output_columns=[],
            row_count=3,
            uploaded_by=self.user,
        )
        self.mc = ModelConfig.objects.create(
            name="Status MC",
            provider=Provider.LOCAL,
            model_name="llama",
            created_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="Status PT",
            version_number=1,
            template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.client.login(username="statususer", password="testpass123")

    def _make_run(self, status=RunStatus.RUNNING, rows_completed=1, rows_total=3, rows_failed=0):
        return TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            status=status,
            rows_completed=rows_completed,
            rows_total=rows_total,
            rows_failed=rows_failed,
            created_by=self.user,
        )

    def test_requires_login(self):
        self.client.logout()
        run = self._make_run()
        response = self.client.get(reverse("core:testrun_status", kwargs={"pk": run.pk}))
        self.assertEqual(response.status_code, 302)

    def test_returns_json(self):
        run = self._make_run()
        response = self.client.get(reverse("core:testrun_status", kwargs={"pk": run.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_json_contains_expected_fields(self):
        run = self._make_run(status=RunStatus.RUNNING, rows_completed=2, rows_total=3)
        response = self.client.get(reverse("core:testrun_status", kwargs={"pk": run.pk}))
        data = response.json()
        self.assertIn("status", data)
        self.assertIn("status_display", data)
        self.assertIn("rows_completed", data)
        self.assertIn("rows_total", data)
        self.assertIn("rows_failed", data)
        self.assertIn("result_count", data)

    def test_json_reflects_current_status(self):
        run = self._make_run(status=RunStatus.RUNNING, rows_completed=2, rows_total=3)
        response = self.client.get(reverse("core:testrun_status", kwargs={"pk": run.pk}))
        data = response.json()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["rows_completed"], 2)
        self.assertEqual(data["rows_total"], 3)

    def test_result_count_reflects_results(self):
        run = self._make_run()
        row = TestCaseRow.objects.create(
            version=self.version,
            row_number=1,
            input_fields={"input_text": "hello"},
            expected_output_fields={},
        )
        TestRunResult.objects.create(
            test_run=run,
            test_case_row=row,
            prompt_sent="hello",
            raw_response="ok",
            status="success",
        )
        response = self.client.get(reverse("core:testrun_status", kwargs={"pk": run.pk}))
        data = response.json()
        self.assertEqual(data["result_count"], 1)

    def test_404_for_nonexistent_run(self):
        import uuid
        response = self.client.get(
            reverse("core:testrun_status", kwargs={"pk": uuid.uuid4()})
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# TestRunResultsPartialView — HTML partial endpoint
# ---------------------------------------------------------------------------


class TestRunResultsPartialViewTests(DjangoTestCase):
    """Tests for the /runs/<pk>/results-partial/ HTML fragment endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username="partialuser", password="testpass123")
        self.tc = TestCase.objects.create(name="Partial TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="p.csv",
            column_names=["input_text"],
            input_columns=["input_text"],
            output_columns=[],
            row_count=1,
            uploaded_by=self.user,
        )
        self.mc = ModelConfig.objects.create(
            name="Partial MC",
            provider=Provider.LOCAL,
            model_name="llama",
            created_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="Partial PT",
            version_number=1,
            template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.row = TestCaseRow.objects.create(
            version=self.version,
            row_number=1,
            input_fields={"input_text": "hello"},
            expected_output_fields={},
        )
        self.run = TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            rows_total=1,
            created_by=self.user,
        )
        self.client.login(username="partialuser", password="testpass123")

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(
            reverse("core:testrun_results_partial", kwargs={"pk": self.run.pk})
        )
        self.assertEqual(response.status_code, 302)

    def test_returns_html_fragment(self):
        response = self.client.get(
            reverse("core:testrun_results_partial", kwargs={"pk": self.run.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])

    def test_empty_when_no_results(self):
        response = self.client.get(
            reverse("core:testrun_results_partial", kwargs={"pk": self.run.pk})
        )
        self.assertEqual(response.status_code, 200)
        # No <tr> elements when there are no results
        self.assertNotContains(response, "<tr")

    def test_contains_result_row_when_results_exist(self):
        TestRunResult.objects.create(
            test_run=self.run,
            test_case_row=self.row,
            prompt_sent="hello",
            raw_response="world",
            status="success",
        )
        response = self.client.get(
            reverse("core:testrun_results_partial", kwargs={"pk": self.run.pk})
        )
        self.assertContains(response, "<tr")
        self.assertContains(response, "world")

    def test_404_for_nonexistent_run(self):
        import uuid
        response = self.client.get(
            reverse("core:testrun_results_partial", kwargs={"pk": uuid.uuid4()})
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Task cancellation — execute_test_run respects CANCELLED status mid-loop
# ---------------------------------------------------------------------------


class ExecuteTestRunCancellationTests(DjangoTestCase):
    """Verify the task stops making LLM calls when status is set to CANCELLED."""

    def setUp(self):
        self.user = User.objects.create_user(username="taskcancel", password="testpass123")
        self.tc = TestCase.objects.create(name="Task Cancel TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="tc.csv",
            column_names=["input_text"],
            input_columns=["input_text"],
            output_columns=[],
            row_count=3,
            uploaded_by=self.user,
        )
        for i in range(1, 4):
            TestCaseRow.objects.create(
                version=self.version,
                row_number=i,
                input_fields={"input_text": f"row{i}"},
                expected_output_fields={},
            )
        self.mc = ModelConfig.objects.create(
            name="Task MC",
            provider=Provider.LOCAL,
            model_name="llama",
            rate_limit_rpm=0,
            created_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="Task PT",
            version_number=1,
            template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )

    def test_task_stops_processing_after_cancel(self):
        """After the first row, set status to CANCELLED; only one LLM call should be made."""
        from unittest.mock import patch, call as mock_call

        run = TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            status=RunStatus.PENDING,
            created_by=self.user,
        )

        call_count = {"n": 0}

        def fake_llm(model_config, prompt, **kwargs):
            call_count["n"] += 1
            # After the first call, cancel the run so the loop exits next iteration
            if call_count["n"] == 1:
                TestRun.objects.filter(pk=run.pk).update(status=RunStatus.CANCELLED)
            return {"text": "ok", "input_tokens": 1, "output_tokens": 1, "latency_ms": 10}

        with patch("core.tasks.call_llm", side_effect=fake_llm):
            with patch("core.tasks.get_limiter") as mock_limiter:
                mock_limiter.return_value.wait_if_needed = lambda: None
                from core.tasks import execute_test_run
                execute_test_run(str(run.pk))

        self.assertEqual(call_count["n"], 1, "Should have stopped after 1 LLM call")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)

    def test_task_marks_completed_when_not_cancelled(self):
        """All rows processed → status should be COMPLETED."""
        run = TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            status=RunStatus.PENDING,
            created_by=self.user,
        )

        def fake_llm(model_config, prompt, **kwargs):
            return {"text": "ok", "input_tokens": 1, "output_tokens": 1, "latency_ms": 10}

        with patch("core.tasks.call_llm", side_effect=fake_llm):
            with patch("core.tasks.get_limiter") as mock_limiter:
                mock_limiter.return_value.wait_if_needed = lambda: None
                from core.tasks import execute_test_run
                execute_test_run(str(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------


class RateLimiterTests(DjangoTestCase):
    """Unit tests for the RateLimiter concurrency semaphore and RPM throttle."""

    def test_context_manager_allows_up_to_max_concurrency(self):
        """Semaphore should allow exactly max_concurrency threads inside at once."""
        import threading
        from core.services.rate_limiter import RateLimiter

        limiter = RateLimiter(requests_per_minute=600, max_concurrency=2)
        inside = []
        blocked = threading.Event()
        entered = threading.Event()

        def worker():
            with limiter:
                inside.append(1)
                entered.set()
                blocked.wait(timeout=2)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        entered.wait(timeout=2)

        # Both threads should be inside at the same time
        self.assertEqual(len(inside), 2)
        blocked.set()
        t1.join(timeout=2)
        t2.join(timeout=2)

    def test_wait_if_needed_respects_rpm(self):
        """Sequential calls should be spaced by at least min_interval."""
        import time
        from core.services.rate_limiter import RateLimiter

        limiter = RateLimiter(requests_per_minute=120)  # 0.5s interval
        limiter.wait_if_needed()
        t0 = time.monotonic()
        limiter.wait_if_needed()
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.4)  # allow small timing jitter

    def test_get_limiter_returns_same_instance(self):
        """Same model config id should return the same limiter object."""
        from core.services.rate_limiter import get_limiter, _limiters
        _limiters.clear()
        l1 = get_limiter("model-abc", 60, 1)
        l2 = get_limiter("model-abc", 60, 1)
        self.assertIs(l1, l2)

    def test_get_limiter_recreates_on_setting_change(self):
        """If concurrency changes, a new limiter should be created."""
        from core.services.rate_limiter import get_limiter, _limiters
        _limiters.clear()
        l1 = get_limiter("model-xyz", 60, 1)
        l2 = get_limiter("model-xyz", 60, 3)
        self.assertIsNot(l1, l2)


# ---------------------------------------------------------------------------
# Concurrent execute_test_run tests
# ---------------------------------------------------------------------------


class ExecuteTestRunConcurrencyTests(DjangoTestCase):
    """Verify that max_concurrency > 1 dispatches rows in parallel."""

    def setUp(self):
        self.user = User.objects.create_user(username="concuser", password="testpass123")
        self.tc = TestCase.objects.create(name="Conc TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="c.csv",
            column_names=["input_text"],
            input_columns=["input_text"],
            output_columns=[],
            row_count=4,
            uploaded_by=self.user,
        )
        for i in range(1, 5):
            TestCaseRow.objects.create(
                version=self.version,
                row_number=i,
                input_fields={"input_text": f"row{i}"},
                expected_output_fields={},
            )
        self.mc = ModelConfig.objects.create(
            name="Conc MC",
            provider=Provider.LOCAL,
            model_name="llama",
            rate_limit_rpm=600,
            max_concurrency=3,
            created_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="Conc PT",
            version_number=1,
            template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )

    def test_all_rows_processed_with_concurrency(self):
        """All rows should be saved even when max_concurrency > 1."""
        from unittest.mock import patch
        from core.tasks import execute_test_run

        run = TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            status=RunStatus.PENDING,
            created_by=self.user,
        )

        def fake_llm(model_config, prompt, **kwargs):
            return {"text": "ok", "input_tokens": 1, "output_tokens": 1, "latency_ms": 5}

        with patch("core.tasks.call_llm", side_effect=fake_llm):
            with patch("core.tasks.get_limiter") as mock_limiter:
                mock_limiter.return_value.wait_if_needed = lambda: None
                execute_test_run(str(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.rows_completed, 4)
        self.assertEqual(TestRunResult.objects.filter(test_run=run).count(), 4)

    def test_model_config_default_max_concurrency_is_one(self):
        """New ModelConfig instances should default to sequential (max_concurrency=1)."""
        mc = ModelConfig.objects.create(
            name="Default Conc MC",
            provider=Provider.LOCAL,
            model_name="llama",
            created_by=self.user,
        )
        self.assertEqual(mc.max_concurrency, 1)
