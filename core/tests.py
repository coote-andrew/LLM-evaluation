"""
Core app tests.

Consolidated here to avoid unittest discovery conflicts with core/tests/ package.
"""

import csv
import io
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase
from django.test import TransactionTestCase
from django.urls import reverse

from core.models import (
    AuthType,
    AssessorType,
    EvalRunStatus,
    EvalType,
    EvaluationConfig,
    EvaluationResult,
    EvaluationRun,
    ModelConfig,
    PromptTemplate,
    Provider,
    ResponseFormat,
    ResultStatus,
    RunStatus,
    ProjectShare,
    ShareRole,
    TestCase,
    TestCaseRow,
    TestCaseVersion,
    TestRun,
    TestRunResult,
    UserProfile,
    Visibility,
)
from core.forms import TestRunCreateForm
from core.services.csv_parser import group_rows, parse_csv, parse_excel, parse_upload
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


# --- Grouped upload tests ---


class GroupRowsTests(DjangoTestCase):
    """Tests for group_rows() and parse_upload() grouped mode."""

    # Helpers

    def _flat_rows(self, data):
        """Build a flat row list from a list of input_fields dicts."""
        return [
            {"row_number": i + 1, "input_fields": fields, "expected_output_fields": {}}
            for i, fields in enumerate(data)
        ]

    def _flat_rows_with_output(self, data):
        return [
            {
                "row_number": i + 1,
                "input_fields": fields["input"],
                "expected_output_fields": fields["output"],
            }
            for i, fields in enumerate(data)
        ]

    # group_rows() unit tests

    def test_groups_two_admissions(self):
        rows = self._flat_rows([
            {"input_csn": "111", "input_note_date": "2026-01-01", "input_note_text": "First"},
            {"input_csn": "111", "input_note_date": "2026-01-02", "input_note_text": "Second"},
            {"input_csn": "222", "input_note_date": "2026-01-10", "input_note_text": "Only"},
        ])
        result = group_rows(rows, ["input_csn"])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["input_fields"]["input_csn"], "111")
        self.assertEqual(len(result[0]["input_fields"]["input_notes"]), 2)
        self.assertEqual(result[1]["input_fields"]["input_csn"], "222")
        self.assertEqual(len(result[1]["input_fields"]["input_notes"]), 1)

    def test_row_numbers_are_sequential(self):
        rows = self._flat_rows([
            {"input_csn": "A", "input_text": "x"},
            {"input_csn": "B", "input_text": "y"},
            {"input_csn": "C", "input_text": "z"},
        ])
        result = group_rows(rows, ["input_csn"])
        self.assertEqual([r["row_number"] for r in result], [1, 2, 3])

    def test_sort_within_group(self):
        rows = self._flat_rows([
            {"input_csn": "111", "input_note_date": "2026-01-03", "input_note_text": "Third"},
            {"input_csn": "111", "input_note_date": "2026-01-01", "input_note_text": "First"},
            {"input_csn": "111", "input_note_date": "2026-01-02", "input_note_text": "Second"},
        ])
        result = group_rows(rows, ["input_csn"], sort_by_col="input_note_date")
        notes = result[0]["input_fields"]["input_notes"]
        self.assertEqual([n["input_note_date"] for n in notes], ["2026-01-01", "2026-01-02", "2026-01-03"])

    def test_single_note_admission(self):
        rows = self._flat_rows([
            {"input_csn": "999", "input_note_text": "Solo note"},
        ])
        result = group_rows(rows, ["input_csn"])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["input_fields"]["input_notes"]), 1)

    def test_composite_group_key(self):
        rows = self._flat_rows([
            {"input_csn": "111", "input_unit": "ICU", "input_note_text": "A"},
            {"input_csn": "111", "input_unit": "ICU", "input_note_text": "B"},
            {"input_csn": "111", "input_unit": "Ward", "input_note_text": "C"},
        ])
        result = group_rows(rows, ["input_csn", "input_unit"])
        self.assertEqual(len(result), 2)

    def test_static_fields_not_in_notes(self):
        rows = self._flat_rows([
            {"input_csn": "111", "input_admission_date": "2026-01-01", "input_note_text": "A"},
            {"input_csn": "111", "input_admission_date": "2026-01-01", "input_note_text": "B"},
        ])
        result = group_rows(rows, ["input_csn", "input_admission_date"])
        fields = result[0]["input_fields"]
        self.assertIn("input_csn", fields)
        self.assertIn("input_admission_date", fields)
        for note in fields["input_notes"]:
            self.assertNotIn("input_csn", note)
            self.assertNotIn("input_admission_date", note)

    def test_missing_sort_column_falls_back_gracefully(self):
        rows = self._flat_rows([
            {"input_csn": "111", "input_note_date": "", "input_note_text": "A"},
            {"input_csn": "111", "input_note_date": "2026-01-01", "input_note_text": "B"},
        ])
        result = group_rows(rows, ["input_csn"], sort_by_col="input_note_date")
        self.assertEqual(len(result[0]["input_fields"]["input_notes"]), 2)

    def test_expected_output_taken_from_first_row(self):
        rows = self._flat_rows_with_output([
            {"input": {"input_csn": "111", "input_note_text": "A"}, "output": {"output_summary": "gold"}},
            {"input": {"input_csn": "111", "input_note_text": "B"}, "output": {"output_summary": "ignored"}},
        ])
        result = group_rows(rows, ["input_csn"])
        self.assertEqual(result[0]["expected_output_fields"], {"output_summary": "gold"})

    def test_insertion_order_preserved(self):
        rows = self._flat_rows([
            {"input_csn": "Z", "input_note_text": "z"},
            {"input_csn": "A", "input_note_text": "a"},
            {"input_csn": "M", "input_note_text": "m"},
        ])
        result = group_rows(rows, ["input_csn"])
        self.assertEqual([r["input_fields"]["input_csn"] for r in result], ["Z", "A", "M"])

    # parse_upload() integration tests for grouped mode

    def test_parse_upload_flat_mode_unchanged(self):
        content = b"input_csn,input_note_text,output_label\n111,Hello,pos\n"
        result = parse_upload(content, "test.csv")
        self.assertEqual(result["input_columns"], ["input_csn", "input_note_text"])
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["input_fields"], {"input_csn": "111", "input_note_text": "Hello"})

    def test_parse_upload_grouped_csv(self):
        content = (
            b"input_csn,input_admission_date,input_note_date,input_note_text\n"
            b"111,2026-01-01,2026-01-01,First\n"
            b"111,2026-01-01,2026-01-02,Second\n"
            b"222,2026-01-10,2026-01-10,Only\n"
        )
        result = parse_upload(content, "test.csv", group_by_columns=["input_csn", "input_admission_date"])
        self.assertEqual(result["row_count"], 2)
        self.assertIn("input_notes", result["input_columns"])
        self.assertEqual(result["rows"][0]["input_fields"]["input_csn"], "111")
        self.assertEqual(len(result["rows"][0]["input_fields"]["input_notes"]), 2)

    def test_parse_upload_grouped_xlsx(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["input_csn", "input_note_text"])
        ws.append(["111", "A"])
        ws.append(["111", "B"])
        ws.append(["222", "C"])
        buf = BytesIO()
        wb.save(buf)
        result = parse_upload(buf.getvalue(), "test.xlsx", group_by_columns=["input_csn"])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(len(result["rows"][0]["input_fields"]["input_notes"]), 2)

    def test_parse_upload_grouped_with_sort(self):
        content = (
            b"input_csn,input_note_date,input_note_text\n"
            b"111,2026-01-03,Third\n"
            b"111,2026-01-01,First\n"
            b"111,2026-01-02,Second\n"
        )
        result = parse_upload(
            content, "test.csv",
            group_by_columns=["input_csn"],
            sort_by_column="input_note_date",
        )
        notes = result["rows"][0]["input_fields"]["input_notes"]
        self.assertEqual([n["input_note_date"] for n in notes], ["2026-01-01", "2026-01-02", "2026-01-03"])

    def test_parse_upload_grouped_column_names_reflect_grouped_schema(self):
        content = b"input_csn,input_note_text,output_summary\n111,A,gold\n"
        result = parse_upload(content, "test.csv", group_by_columns=["input_csn"])
        self.assertIn("input_notes", result["input_columns"])
        self.assertIn("input_notes", result["column_names"])
        self.assertIn("output_summary", result["column_names"])
        self.assertNotIn("input_note_text", result["input_columns"])


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

    def test_list_value_serialised_as_json(self):
        template = "Notes: {input_notes}"
        notes = [{"note_date": "2026-01-01", "note_text": "Stable"}]
        result = build_prompt(template, {"input_notes": notes})
        import json
        self.assertEqual(result, f"Notes: {json.dumps(notes)}")

    def test_dict_value_serialised_as_json(self):
        template = "Data: {input_data}"
        data = {"key": "value"}
        result = build_prompt(template, {"input_data": data})
        import json
        self.assertEqual(result, f"Data: {json.dumps(data)}")

    def test_brace_characters_in_note_text_do_not_crash(self):
        template = "Notes: {input_notes}"
        notes = [{"note_text": "BP {systolic}/{diastolic} mmHg — review {plan}"}]
        result = build_prompt(template, {"input_notes": notes})
        self.assertIn("systolic", result)

    def test_plain_string_fields_unchanged(self):
        template = "{input_question}"
        fields = {"input_question": "What is the diagnosis?"}
        self.assertEqual(build_prompt(template, fields), "What is the diagnosis?")


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
        self.assertEqual(mc.auth_type, AuthType.API_KEY)
        self.assertTrue(mc.is_active)

    def test_create_azure_app_registration_config(self):
        mc = ModelConfig.objects.create(
            name="Azure GPT",
            provider=Provider.AZURE_OPENAI,
            auth_type=AuthType.AZURE_CLIENT_SECRET,
            api_endpoint="https://example.openai.azure.com/openai/deployments/gpt-4",
            model_name="gpt-4",
            azure_tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            azure_client_id="11111111-2222-3333-4444-555555555555",
            azure_client_secret="super-secret",
            created_by=self.user,
        )

        mc.refresh_from_db()
        self.assertEqual(mc.auth_type, AuthType.AZURE_CLIENT_SECRET)
        self.assertEqual(mc.azure_client_secret, "super-secret")


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
        self.superuser = User.objects.create_superuser(
            username="admin",
            password="adminpass123",
            email="admin@example.com",
        )
        self.url = reverse("core:register")

    def test_redirects_to_login_when_anonymous(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_shows_form_when_superuser(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Account")

    def test_rejects_regular_authenticated_user(self):
        self.client.login(username="existing", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_creates_user_without_switching_superuser_session(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "username": "newuser",
            "password1": "strongpass99",
            "password2": "strongpass99",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="newuser").exists())
        self.assertEqual(
            int(self.client.session["_auth_user_id"]),
            self.superuser.pk,
        )

    def test_creates_temporary_password_user_without_logging_in_as_them(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "username": "reviewer",
            "password1": "TempPass123!",
            "password2": "TempPass123!",
            "must_change_password": "on",
        })
        self.assertEqual(response.status_code, 302)
        reviewer = User.objects.get(username="reviewer")
        self.assertTrue(reviewer.profile.must_change_password)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.superuser.pk)

    def test_rejects_duplicate_username(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "username": "existing",
            "password1": "strongpass99",
            "password2": "strongpass99",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already taken")

    def test_rejects_mismatched_passwords(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "username": "brandnew",
            "password1": "strongpass99",
            "password2": "different99",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "do not match")


class ForcedPasswordChangeTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="TempPass123!")
        UserProfile.objects.update_or_create(
            user=self.user,
            defaults={"must_change_password": True},
        )

    def test_login_redirects_flagged_user_to_password_change_with_next(self):
        next_url = reverse("core:dashboard")
        response = self.client.post(reverse("login"), {
            "username": "reviewer",
            "password": "TempPass123!",
            "next": next_url,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("password_change")))
        self.assertIn("next=%2F", response.url)

    def test_flagged_user_is_blocked_from_app_until_password_changed(self):
        self.client.login(username="reviewer", password="TempPass123!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("password_change")))

    def test_password_change_clears_flag_and_redirects_to_shared_page(self):
        shared_url = "/evaluations/00000000-0000-0000-0000-000000000000/review/?row=3"
        self.client.login(username="reviewer", password="TempPass123!")
        response = self.client.post(reverse("password_change"), {
            "old_password": "TempPass123!",
            "new_password1": "NewStrongPass123!",
            "new_password2": "NewStrongPass123!",
            "next": shared_url,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, shared_url)
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.must_change_password)


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
        self.assertContains(response, "Projects")


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


class LLMClientNetworkErrorTests(DjangoTestCase):
    """Network failures should be recorded as model-call errors."""

    def test_azure_connect_error_returns_error_result(self):
        from unittest.mock import patch

        import httpx

        from core.services.llm_client import call_llm

        user = User.objects.create_user(username="network", password="testpass123")
        mc = ModelConfig.objects.create(
            name="Azure Network",
            provider=Provider.AZURE_OPENAI,
            auth_type=AuthType.API_KEY,
            api_endpoint="https://example.openai.azure.com/openai/deployments/gpt-4",
            api_key="azure-api-key",
            model_name="gpt-4",
            created_by=user,
        )

        with patch("httpx.Client") as mock_client_cls:
            client = mock_client_cls.return_value.__enter__.return_value
            client.post.side_effect = httpx.ConnectError("[Errno 113] No route to host")

            result = call_llm(mc, prompt="hi")

        self.assertEqual(result["text"], "")
        self.assertEqual(result["input_tokens"], 0)
        self.assertEqual(result["output_tokens"], 0)
        self.assertIn("Connection error calling", result["error"])
        self.assertIn("[Errno 113] No route to host", result["error"])


class LLMClientAzureClientSecretAuthTests(DjangoTestCase):
    """Azure app registration auth exchanges credentials for a bearer token."""

    def setUp(self):
        self.user = User.objects.create_user(username="azureclient", password="testpass123")
        self.mc = ModelConfig.objects.create(
            name="Azure App Registration",
            provider=Provider.AZURE_OPENAI,
            auth_type=AuthType.AZURE_CLIENT_SECRET,
            api_endpoint="https://example.openai.azure.com/openai/deployments/gpt-4",
            model_name="gpt-4",
            azure_tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            azure_client_id="11111111-2222-3333-4444-555555555555",
            azure_client_secret="client-secret",
            created_by=self.user,
        )

    def test_call_llm_uses_bearer_token_for_azure_app_registration(self):
        from unittest.mock import MagicMock, patch
        from core.services.llm_client import _AZURE_TOKEN_CACHE, call_llm

        _AZURE_TOKEN_CACHE.clear()
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "entra-token",
            "expires_in": 3600,
        }
        token_response.text = ""

        chat_response = MagicMock()
        chat_response.status_code = 200
        chat_response.json.return_value = {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }
        chat_response.headers = {}

        with patch("httpx.Client") as mock_client_cls:
            client = MagicMock()
            client.post.side_effect = [token_response, chat_response]
            mock_client_cls.return_value.__enter__.return_value = client

            result = call_llm(self.mc, prompt="hi")

        self.assertIsNone(result.get("error"))
        self.assertEqual(result["text"], "hello")
        self.assertEqual(client.post.call_count, 2)

        token_call = client.post.call_args_list[0]
        self.assertEqual(
            token_call.args[0],
            "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47/oauth2/v2.0/token",
        )
        self.assertEqual(token_call.kwargs["data"]["grant_type"], "client_credentials")
        self.assertEqual(token_call.kwargs["data"]["client_id"], self.mc.azure_client_id)
        self.assertEqual(token_call.kwargs["data"]["client_secret"], "client-secret")

        chat_call = client.post.call_args_list[1]
        self.assertEqual(chat_call.kwargs["headers"]["Authorization"], "Bearer entra-token")
        self.assertNotIn("api-key", chat_call.kwargs["headers"])

    def test_token_failure_returns_error(self):
        from unittest.mock import MagicMock, patch
        from core.services.llm_client import _AZURE_TOKEN_CACHE, call_llm

        _AZURE_TOKEN_CACHE.clear()
        token_response = MagicMock()
        token_response.status_code = 401
        token_response.text = "bad credentials"

        with patch("httpx.Client") as mock_client_cls:
            client = MagicMock()
            client.post.return_value = token_response
            mock_client_cls.return_value.__enter__.return_value = client

            result = call_llm(self.mc, prompt="hi")

        self.assertIn("Azure token request failed 401", result["error"])
        self.assertEqual(client.post.call_count, 1)


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
        self.assertIn("input_text", header)
        self.assertIn("output_label", header)
        self.assertNotIn("input_input_text", header)
        self.assertNotIn("expected_output_label", header)
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
        self.assertIn("input_text", header)
        self.assertIn("output_label", header)
        self.assertNotIn("input_input_text", header)
        self.assertNotIn("expected_output_label", header)
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

    def test_delete_view_deactivates_version_without_removing_it(self):
        pt = self._make_pt(version=1)
        url = reverse("core:prompttemplate_delete", kwargs={"pk": pt.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        pt.refresh_from_db()
        self.assertFalse(pt.is_active)

    def test_testcase_detail_hides_inactive_prompts_by_default(self):
        active = self._make_pt(name="Active")
        inactive = self._make_pt(name="Inactive")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        response = self.client.get(reverse("core:testcase_detail", kwargs={"pk": self.tc.pk}))

        groups = response.context["prompt_template_groups"]
        self.assertEqual([group["latest"].pk for group in groups], [active.pk])

    def test_testcase_detail_hides_inactive_prompts_for_staff_by_default(self):
        pt = self._make_pt()
        pt.is_active = False
        pt.save(update_fields=["is_active"])
        staff = User.objects.create_superuser(
            username="staff", email="staff@example.com", password="testpass123"
        )
        self.client.force_login(staff)

        response = self.client.get(reverse("core:testcase_detail", kwargs={"pk": self.tc.pk}))

        self.assertEqual(response.context["prompt_template_groups"], [])

    def test_testcase_detail_can_show_inactive_prompts(self):
        inactive = self._make_pt(name="Inactive")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        response = self.client.get(
            reverse("core:testcase_detail", kwargs={"pk": self.tc.pk}),
            {"show_inactive": "1"},
        )

        self.assertTrue(response.context["show_inactive_prompts"])
        self.assertEqual(response.context["prompt_template_groups"][0]["latest"], inactive)

    def test_inactive_prompt_cannot_be_selected_for_new_run(self):
        pt = self._make_pt()
        pt.is_active = False
        pt.save(update_fields=["is_active"])

        form = TestRunCreateForm(user=self.user)

        self.assertNotIn(
            pt.pk,
            form.fields["prompt_template"].queryset.values_list("pk", flat=True),
        )

    def test_activate_view_restores_inactive_version(self):
        pt = self._make_pt()
        pt.is_active = False
        pt.save(update_fields=["is_active"])

        response = self.client.post(
            reverse("core:prompttemplate_activate", kwargs={"pk": pt.pk})
        )

        self.assertEqual(response.status_code, 302)
        pt.refresh_from_db()
        self.assertTrue(pt.is_active)

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


class TestRunCreatePromptFilteringTests(DjangoTestCase):
    """Tests for filtering prompt choices by selected test case version."""

    def setUp(self):
        self.user = User.objects.create_user(username="runcreate", password="testpass123")
        self.tc1 = TestCase.objects.create(name="TC One", created_by=self.user)
        self.tc2 = TestCase.objects.create(name="TC Two", created_by=self.user)
        self.v1 = TestCaseVersion.objects.create(
            test_case=self.tc1,
            version_number=1,
            original_filename="one.csv",
            column_names=[],
            input_columns=[],
            output_columns=[],
            row_count=0,
            uploaded_by=self.user,
        )
        self.v2 = TestCaseVersion.objects.create(
            test_case=self.tc2,
            version_number=1,
            original_filename="two.csv",
            column_names=[],
            input_columns=[],
            output_columns=[],
            row_count=0,
            uploaded_by=self.user,
        )
        self.prompt1 = PromptTemplate.objects.create(
            test_case=self.tc1,
            name="Prompt One",
            template_text="{input_x}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.prompt2 = PromptTemplate.objects.create(
            test_case=self.tc2,
            name="Prompt Two",
            template_text="{input_x}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.client.login(username="runcreate", password="testpass123")

    def test_form_filters_prompts_for_selected_test_case_version(self):
        form = TestRunCreateForm(data={"test_case_version": str(self.v1.pk)})

        prompt_ids = set(form.fields["prompt_template"].queryset.values_list("pk", flat=True))
        self.assertIn(self.prompt1.pk, prompt_ids)
        self.assertNotIn(self.prompt2.pk, prompt_ids)

    def test_prompt_options_endpoint_filters_by_test_case_version(self):
        url = reverse("core:testrun_prompt_template_options")
        response = self.client.get(url, {"test_case_version": str(self.v1.pk)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prompt One")
        self.assertNotContains(response, "Prompt Two")

    def test_create_page_marks_older_prompt_versions(self):
        PromptTemplate.objects.create(
            test_case=self.tc1,
            name="Prompt One",
            template_text="{input_x} v2",
            response_format=ResponseFormat.FREE_TEXT,
            version_number=2,
            created_by=self.user,
        )
        url = reverse("core:testrun_create")
        response = self.client.get(url, {"test_case_version": str(self.v1.pk)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Show previous versions")
        self.assertContains(response, 'data-older')
        self.assertContains(response, "older versions")


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
        self.assertIn("/accounts/login/", response["Location"])
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


class ExecuteTestRunCancellationTests(TransactionTestCase):
    """Verify the task stops making LLM calls when status is set to CANCELLED.

    Uses TransactionTestCase so a CANCELLED write from a pool thread is visible
    to the main task thread's refresh_from_db (Django TestCase wraps each test
    in a transaction that hides cross-thread commits on SQLite).
    """

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
        from unittest.mock import patch

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
        import time
        from core.services.rate_limiter import RateLimiter

        limiter = RateLimiter(requests_per_minute=600, max_concurrency=2)
        inside = []
        blocked = threading.Event()
        both_entered = threading.Barrier(2)

        def worker():
            with limiter:
                inside.append(1)
                both_entered.wait(timeout=2)
                blocked.wait(timeout=2)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        # Give both threads time to acquire slots (includes small RPM sleep).
        deadline = time.monotonic() + 2
        while len(inside) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

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


class EffectiveMaxConcurrencyTests(DjangoTestCase):
    """Hard cap protects the Postgres connection budget."""

    def test_effective_max_concurrency_clamps_to_settings(self):
        from django.test import override_settings

        from core.tasks import _effective_max_concurrency

        with override_settings(MAX_MODEL_CONCURRENCY=4):
            self.assertEqual(_effective_max_concurrency(1), 1)
            self.assertEqual(_effective_max_concurrency(4), 4)
            self.assertEqual(_effective_max_concurrency(99), 4)
            self.assertEqual(_effective_max_concurrency(None), 1)
            self.assertEqual(_effective_max_concurrency(0), 1)


class CallRowNoOrmTests(DjangoTestCase):
    """Pool workers must not open Django DB connections during LLM waits."""

    def test_call_row_skips_when_cancel_event_set(self):
        import threading
        from unittest.mock import MagicMock, patch

        from core.tasks import _call_row

        cancel_event = threading.Event()
        cancel_event.set()
        limiter = MagicMock()
        row = MagicMock()
        row.input_fields = {"input_text": "hi"}

        with patch("core.tasks.call_llm") as mock_llm:
            with patch("core.tasks.TestRun.objects") as mock_qs:
                result = _call_row(
                    cancel_event, limiter, MagicMock(), "{input_text}", 0.0, False, row
                )

        self.assertEqual(result[1], None)
        mock_llm.assert_not_called()
        mock_qs.assert_not_called()
        limiter.wait_if_needed.assert_not_called()

    def test_call_row_does_not_query_orm(self):
        import threading
        from unittest.mock import MagicMock, patch

        from core.tasks import _call_row

        cancel_event = threading.Event()
        limiter = MagicMock()
        row = MagicMock()
        row.input_fields = {"input_text": "hi"}
        model_config = MagicMock()

        with patch("core.tasks.call_llm", return_value={"text": "ok", "input_tokens": 1, "output_tokens": 1, "latency_ms": 1, "parsed": None}) as mock_llm:
            with patch("core.tasks.TestRun.objects") as mock_qs:
                row_out, result = _call_row(
                    cancel_event, limiter, model_config, "{input_text}", 0.0, False, row
                )

        self.assertIs(row_out, row)
        self.assertEqual(result["text"], "ok")
        mock_llm.assert_called_once()
        mock_qs.assert_not_called()


class KeywordEvalTaskTests(DjangoTestCase):
    """Keyword evaluation runs as a Celery task (not a web-process thread)."""

    def setUp(self):
        self.user = User.objects.create_user(username="keywordeval", password="testpass123")
        self.tc = TestCase.objects.create(name="KW TC", created_by=self.user)
        self.version = TestCaseVersion.objects.create(
            test_case=self.tc,
            version_number=1,
            original_filename="kw.csv",
            column_names=["input_text", "output_answer"],
            input_columns=["input_text"],
            output_columns=["output_answer"],
            row_count=1,
            uploaded_by=self.user,
        )
        self.row = TestCaseRow.objects.create(
            version=self.version,
            row_number=1,
            input_fields={"input_text": "hello"},
            expected_output_fields={"output_answer": "world"},
        )
        self.mc = ModelConfig.objects.create(
            name="KW MC",
            provider=Provider.LOCAL,
            model_name="llama",
            created_by=self.user,
        )
        self.pt = PromptTemplate.objects.create(
            test_case=self.tc,
            name="KW PT",
            version_number=1,
            template_text="{input_text}",
            response_format=ResponseFormat.FREE_TEXT,
            created_by=self.user,
        )
        self.test_run = TestRun.objects.create(
            test_case_version=self.version,
            prompt_template=self.pt,
            model_config=self.mc,
            prompt_snapshot=self.pt.template_text,
            status=RunStatus.COMPLETED,
            created_by=self.user,
        )
        TestRunResult.objects.create(
            test_run=self.test_run,
            test_case_row=self.row,
            prompt_sent="hello",
            raw_response="world",
            status=ResultStatus.SUCCESS,
        )
        self.config = EvaluationConfig.objects.create(
            test_case=self.tc,
            name="KW Config",
            eval_type=EvalType.KEYWORD_MATCH,
            scoring_criteria={
                "checks": [
                    {"name": "has_world", "type": "contains_phrase", "phrase": "world"}
                ]
            },
            created_by=self.user,
        )

    def test_execute_keyword_eval_completes(self):
        from core.tasks import execute_keyword_eval

        eval_run = EvaluationRun.objects.create(
            evaluation_config=self.config,
            test_run=self.test_run,
            created_by=self.user,
        )
        execute_keyword_eval(eval_run.pk)
        eval_run.refresh_from_db()
        self.assertEqual(eval_run.status, EvalRunStatus.COMPLETED)
        self.assertEqual(
            EvaluationResult.objects.filter(evaluation_run=eval_run).count(), 1
        )

    def test_create_view_dispatches_keyword_eval_task(self):
        from unittest.mock import patch

        self.client.login(username="keywordeval", password="testpass123")
        with patch("core.views.evaluations.dispatch_task") as mock_dispatch:
            response = self.client.post(
                reverse("core:evaluationrun_create", args=[self.test_run.pk]),
                {"evaluation_config": str(self.config.pk)},
            )
        self.assertEqual(response.status_code, 302)
        mock_dispatch.assert_called_once()
        from core.tasks import execute_keyword_eval

        self.assertIs(mock_dispatch.call_args.args[0], execute_keyword_eval)


# ---------------------------------------------------------------------------
# Phase A — ModelConfig agent fields
# ---------------------------------------------------------------------------


class ModelConfigAgentFieldsTests(DjangoTestCase):
    """is_agent + agent_alias fields on ModelConfig."""

    def setUp(self):
        self.user = User.objects.create_user(username="agentuser", password="testpass123")

    def test_is_agent_defaults_to_false(self):
        mc = ModelConfig.objects.create(
            name="Plain", provider=Provider.OPENAI, model_name="gpt-4",
            created_by=self.user,
        )
        self.assertFalse(mc.is_agent)
        self.assertIsNone(mc.agent_alias)

    def test_agent_config_roundtrip(self):
        mc = ModelConfig.objects.create(
            name="Agent Clinical",
            provider=Provider.CUSTOM,
            api_endpoint="http://agents:8080",
            model_name="clinical_note_analysis",
            is_agent=True,
            agent_alias="clinical-notes",
            created_by=self.user,
        )
        mc.refresh_from_db()
        self.assertTrue(mc.is_agent)
        self.assertEqual(mc.agent_alias, "clinical-notes")

    def test_agent_alias_unique(self):
        ModelConfig.objects.create(
            name="A", provider=Provider.CUSTOM, model_name="p1",
            is_agent=True, agent_alias="shared", created_by=self.user,
        )
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ModelConfig.objects.create(
                    name="B", provider=Provider.CUSTOM, model_name="p2",
                    is_agent=True, agent_alias="shared", created_by=self.user,
                )

    def test_multiple_non_agent_configs_allowed_with_null_alias(self):
        # unique=True on a nullable field — multiple NULLs must still be allowed.
        ModelConfig.objects.create(
            name="One", provider=Provider.OPENAI, model_name="gpt-4", created_by=self.user
        )
        ModelConfig.objects.create(
            name="Two", provider=Provider.OPENAI, model_name="gpt-4", created_by=self.user
        )
        self.assertEqual(ModelConfig.objects.filter(agent_alias__isnull=True).count(), 2)


class ModelConfigFormAgentValidationTests(DjangoTestCase):
    """ModelConfigForm enforces agent_alias when is_agent=True."""

    def setUp(self):
        self.user = User.objects.create_user(username="formuser", password="testpass123")

    def _payload(self, **overrides):
        base = {
            "name": "Test",
            "provider": Provider.CUSTOM,
            "auth_type": AuthType.API_KEY,
            "api_endpoint": "http://agents.example.com:8080",
            "api_key": "",
            "azure_tenant_id": "",
            "azure_client_id": "",
            "azure_client_secret": "",
            "azure_token_scope": "https://cognitiveservices.azure.com/.default",
            "model_name": "clinical_note_analysis",
            "default_temperature": "0.0",
            "default_max_tokens": "4096",
            "default_timeout": "120.0",
            "rate_limit_rpm": "60",
            "max_concurrency": "1",
            "is_agent": "",
            "agent_alias": "",
            "is_active": "on",
        }
        base.update(overrides)
        return base

    def test_agent_without_alias_is_invalid(self):
        from core.forms import ModelConfigForm
        form = ModelConfigForm(data=self._payload(is_agent="on", agent_alias=""))
        self.assertFalse(form.is_valid())
        self.assertIn("agent_alias", form.errors)

    def test_agent_with_alias_is_valid(self):
        from core.forms import ModelConfigForm
        form = ModelConfigForm(data=self._payload(is_agent="on", agent_alias="clinical"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_create_saves_new_uuid_instance_after_created_by_is_assigned(self):
        from core.forms import ModelConfigForm

        form = ModelConfigForm(data=self._payload(is_agent="on", agent_alias="clinical"))
        self.assertTrue(form.is_valid(), form.errors)

        form.instance.created_by = self.user
        saved = form.save()

        self.assertIsNotNone(saved.pk)
        self.assertTrue(ModelConfig.objects.filter(pk=saved.pk).exists())

    def test_alias_cleared_when_is_agent_false(self):
        from core.forms import ModelConfigForm
        form = ModelConfigForm(data=self._payload(is_agent="", agent_alias="stray"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["agent_alias"])


class ModelConfigFormAzureAuthTests(DjangoTestCase):
    """ModelConfigForm validates Azure app-registration credentials safely."""

    def setUp(self):
        self.user = User.objects.create_user(username="azureform", password="testpass123")

    def _payload(self, **overrides):
        base = {
            "name": "Azure Model",
            "provider": Provider.AZURE_OPENAI,
            "auth_type": AuthType.AZURE_CLIENT_SECRET,
            "api_endpoint": "https://example.openai.azure.com/openai/deployments/gpt-4",
            "api_key": "",
            "azure_tenant_id": "72f988bf-86f1-41af-91ab-2d7cd011db47",
            "azure_client_id": "11111111-2222-3333-4444-555555555555",
            "azure_client_secret": "new-secret",
            "azure_token_scope": "https://cognitiveservices.azure.com/.default",
            "model_name": "gpt-4",
            "default_temperature": "0.0",
            "default_max_tokens": "4096",
            "default_timeout": "120.0",
            "rate_limit_rpm": "60",
            "max_concurrency": "1",
            "is_agent": "",
            "agent_alias": "",
            "is_active": "on",
        }
        base.update(overrides)
        return base

    def test_azure_app_registration_requires_secret_on_create(self):
        from core.forms import ModelConfigForm

        form = ModelConfigForm(data=self._payload(azure_client_secret=""))

        self.assertFalse(form.is_valid())
        self.assertIn("azure_client_secret", form.errors)

    def test_azure_app_registration_validates_uuid_fields(self):
        from core.forms import ModelConfigForm

        form = ModelConfigForm(
            data=self._payload(
                azure_tenant_id="not-a-uuid",
                azure_client_id="also-not-a-uuid",
            )
        )

        self.assertFalse(form.is_valid())
        self.assertIn("azure_tenant_id", form.errors)
        self.assertIn("azure_client_id", form.errors)

    def test_azure_app_registration_is_limited_to_azure_providers(self):
        from core.forms import ModelConfigForm

        form = ModelConfigForm(data=self._payload(provider=Provider.OPENAI))

        self.assertFalse(form.is_valid())
        self.assertIn("auth_type", form.errors)

    def test_blank_secret_on_edit_preserves_existing_secret(self):
        from core.forms import ModelConfigForm

        mc = ModelConfig.objects.create(
            name="Azure Existing",
            provider=Provider.AZURE_OPENAI,
            auth_type=AuthType.AZURE_CLIENT_SECRET,
            api_endpoint="https://example.openai.azure.com/openai/deployments/gpt-4",
            model_name="gpt-4",
            azure_tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            azure_client_id="11111111-2222-3333-4444-555555555555",
            azure_client_secret="existing-secret",
            created_by=self.user,
        )

        form = ModelConfigForm(
            data=self._payload(name="Azure Existing Updated", azure_client_secret=""),
            instance=mc,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.azure_client_secret, "existing-secret")

    def test_api_key_mode_clears_azure_credentials(self):
        from core.forms import ModelConfigForm

        mc = ModelConfig.objects.create(
            name="Azure Existing",
            provider=Provider.AZURE_OPENAI,
            auth_type=AuthType.AZURE_CLIENT_SECRET,
            api_endpoint="https://example.openai.azure.com/openai/deployments/gpt-4",
            model_name="gpt-4",
            azure_tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47",
            azure_client_id="11111111-2222-3333-4444-555555555555",
            azure_client_secret="existing-secret",
            created_by=self.user,
        )

        form = ModelConfigForm(
            data=self._payload(
                auth_type=AuthType.API_KEY,
                api_key="azure-api-key",
                azure_tenant_id="",
                azure_client_id="",
                azure_client_secret="",
            ),
            instance=mc,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.api_key, "azure-api-key")
        self.assertEqual(saved.azure_tenant_id, "")
        self.assertEqual(saved.azure_client_id, "")
        self.assertEqual(saved.azure_client_secret, "")


# ---------------------------------------------------------------------------
# Phase A — llm_client dispatches agent calls
# ---------------------------------------------------------------------------


class LLMClientAgentDispatchTests(DjangoTestCase):
    """When ModelConfig.is_agent is True, call_llm treats the response as a
    clinical_graphs agent output and surfaces the graph state."""

    def setUp(self):
        self.user = User.objects.create_user(username="dispatch", password="testpass123")
        self.mc = ModelConfig.objects.create(
            name="Agents",
            provider=Provider.CUSTOM,
            api_endpoint="http://agents:8080",
            model_name="clinical_note_analysis",
            is_agent=True,
            agent_alias="clinical",
            created_by=self.user,
        )

    def test_call_llm_surfaces_agent_state_as_parsed(self):
        """If the agents service returns message.parsed, call_llm exposes it
        as the top-level `parsed` field (no JSON-parsing of `content`)."""
        from unittest.mock import patch, MagicMock
        from core.services.llm_client import call_llm

        agent_state = {"summary": "s", "problems": ["p1", "p2"]}
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": "clinical_note_analysis",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"summary": "s", "problems": ["p1", "p2"]}',
                        "parsed": agent_state,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        fake_response.headers = {"X-Query-Id": "q-123"}

        with patch("httpx.Client") as mock_client_cls:
            client = MagicMock()
            client.post.return_value = fake_response
            mock_client_cls.return_value.__enter__.return_value = client

            result = call_llm(self.mc, prompt="hello")

        self.assertIsNone(result.get("error"))
        self.assertEqual(result["parsed"], agent_state)
        self.assertEqual(result["agent_state"], agent_state)
        self.assertEqual(result["query_id"], "q-123")
        self.assertEqual(result["input_tokens"], 10)
        self.assertEqual(result["output_tokens"], 20)

        # Agent requests must NOT send temperature/max_tokens to the agents
        # service — those are no-ops there and some versions 422 on them.
        _, kwargs = client.post.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "clinical_note_analysis")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_tokens", payload)

    def test_call_llm_skips_think_tag_stripping_for_agents(self):
        """Agent responses are structured JSON, not chain-of-thought text."""
        from unittest.mock import patch, MagicMock
        from core.services.llm_client import call_llm

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "<think>internal</think>{\"ok\": true}"}}],
            "usage": {},
        }
        fake_response.headers = {}
        with patch("httpx.Client") as mock_client_cls:
            client = MagicMock()
            client.post.return_value = fake_response
            mock_client_cls.return_value.__enter__.return_value = client
            result = call_llm(self.mc, prompt="x")

        self.assertIn("<think>", result["text"])


# ---------------------------------------------------------------------------
# Phase A — llm_providers.yaml generator
# ---------------------------------------------------------------------------


class LLMProvidersYAMLBuilderTests(DjangoTestCase):
    """Pure-function tests for build_providers_document / dump_yaml."""

    def setUp(self):
        self.user = User.objects.create_user(username="yaml", password="testpass123")

    def test_agent_configs_are_excluded(self):
        from core.services.llm_providers_yaml import build_providers_document
        ModelConfig.objects.create(
            name="Real", provider=Provider.OPENAI, model_name="gpt-4",
            created_by=self.user,
        )
        ModelConfig.objects.create(
            name="Agent", provider=Provider.CUSTOM, model_name="ptrn",
            is_agent=True, agent_alias="a", created_by=self.user,
        )
        doc = build_providers_document()
        self.assertEqual(len(doc["providers"]), 1)
        self.assertEqual(doc["providers"][0]["models"][0]["name"], "gpt-4")

    def test_inactive_configs_are_excluded(self):
        from core.services.llm_providers_yaml import build_providers_document
        ModelConfig.objects.create(
            name="Off", provider=Provider.OPENAI, model_name="x",
            is_active=False, created_by=self.user,
        )
        doc = build_providers_document()
        self.assertEqual(doc["providers"], [])

    def test_provider_type_mapping(self):
        from core.services.llm_providers_yaml import build_providers_document
        cases = [
            (Provider.ANTHROPIC, "anthropic"),
            (Provider.OPENAI, "openai"),
            (Provider.VLLM, "openai_compatible"),
            (Provider.AZURE_OPENAI, "openai_compatible"),
            (Provider.AZURE_AI_FOUNDRY, "openai_compatible"),
            (Provider.LOCAL, "openai_compatible"),
            (Provider.CUSTOM, "openai_compatible"),
        ]
        for prov, expected_type in cases:
            with self.subTest(prov=prov):
                ModelConfig.objects.all().delete()
                ModelConfig.objects.create(
                    name="X", provider=prov, model_name="m",
                    api_endpoint="http://host",
                    created_by=self.user,
                )
                doc = build_providers_document()
                self.assertEqual(doc["providers"][0]["type"], expected_type)

    def test_secrets_are_not_inlined(self):
        from core.services.llm_providers_yaml import build_providers_document, dump_yaml
        ModelConfig.objects.create(
            name="With Key", provider=Provider.OPENAI, model_name="gpt-4",
            api_key="sk-super-secret-abc123", created_by=self.user,
        )
        rendered = dump_yaml(build_providers_document())
        self.assertNotIn("sk-super-secret-abc123", rendered)
        self.assertIn("api_key_env", rendered)

    def test_endpoint_becomes_base_url_default(self):
        from core.services.llm_providers_yaml import build_providers_document
        ModelConfig.objects.create(
            name="vllm", provider=Provider.VLLM, model_name="qwen",
            api_endpoint="http://vllm:8000/v1", created_by=self.user,
        )
        doc = build_providers_document()
        self.assertEqual(doc["providers"][0]["base_url_default"], "http://vllm:8000/v1")
        self.assertIn("base_url_env", doc["providers"][0])

    def test_anthropic_without_endpoint_omits_base_url(self):
        from core.services.llm_providers_yaml import build_providers_document
        ModelConfig.objects.create(
            name="claude", provider=Provider.ANTHROPIC, model_name="claude-sonnet-4-5",
            created_by=self.user,
        )
        doc = build_providers_document()
        entry = doc["providers"][0]
        self.assertNotIn("base_url_env", entry)
        self.assertNotIn("base_url_default", entry)


class GenerateLLMProvidersYAMLCommandTests(DjangoTestCase):
    """The management command writes a valid YAML document to disk."""

    def setUp(self):
        self.user = User.objects.create_user(username="cmd", password="testpass123")

    def test_writes_file_at_given_path(self):
        import tempfile
        from io import StringIO
        from pathlib import Path
        from django.core.management import call_command

        ModelConfig.objects.create(
            name="M", provider=Provider.OPENAI, model_name="gpt-4", created_by=self.user,
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "llm_providers.yaml"
            call_command("generate_llm_providers_yaml", output=out, stdout=StringIO())
            self.assertTrue(out.exists())
            content = out.read_text()
            self.assertIn("AUTO-GENERATED", content)
            self.assertIn("providers", content)
            self.assertIn("gpt-4", content)

    def test_dry_run_writes_nothing(self):
        import tempfile
        from io import StringIO
        from pathlib import Path
        from django.core.management import call_command

        ModelConfig.objects.create(
            name="M", provider=Provider.OPENAI, model_name="gpt-4", created_by=self.user,
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "llm_providers.yaml"
            call_command("generate_llm_providers_yaml", output=out, dry_run=True, stdout=StringIO())
            self.assertFalse(out.exists())

    def test_print_flag_emits_yaml_to_stdout(self):
        from io import StringIO
        from django.core.management import call_command

        ModelConfig.objects.create(
            name="M", provider=Provider.OPENAI, model_name="gpt-4", created_by=self.user,
        )
        buf = StringIO()
        call_command("generate_llm_providers_yaml", "--print", stdout=buf)
        output = buf.getvalue()
        self.assertIn("providers", output)
        self.assertIn("gpt-4", output)


class AutoGenerateYAMLSignalTests(DjangoTestCase):
    """Post-save signal regenerates the YAML only when the setting is enabled."""

    def setUp(self):
        self.user = User.objects.create_user(username="sig", password="testpass123")

    def test_signal_noop_when_setting_disabled(self):
        from unittest.mock import patch
        with patch("core.signals._regenerate_yaml") as mock_regen:
            ModelConfig.objects.create(
                name="X", provider=Provider.OPENAI, model_name="gpt-4",
                created_by=self.user,
            )
            mock_regen.assert_not_called()

    def test_signal_runs_when_setting_enabled(self):
        from unittest.mock import patch
        from django.test import override_settings
        with override_settings(AUTO_GENERATE_LLM_PROVIDERS_YAML=True):
            with patch("core.signals._regenerate_yaml") as mock_regen:
                ModelConfig.objects.create(
                    name="Y", provider=Provider.OPENAI, model_name="gpt-4",
                    created_by=self.user,
                )
                mock_regen.assert_called()

    def test_signal_swallows_errors(self):
        """Failures in YAML regeneration must never block a ModelConfig save."""
        from unittest.mock import patch
        from django.test import override_settings
        with override_settings(AUTO_GENERATE_LLM_PROVIDERS_YAML=True):
            with patch(
                "core.signals._regenerate_yaml",
                side_effect=RuntimeError("disk full"),
            ):
                mc = ModelConfig.objects.create(
                    name="Z", provider=Provider.OPENAI, model_name="gpt-4",
                    created_by=self.user,
                )
                # Save succeeded despite regeneration exception
                self.assertIsNotNone(mc.pk)


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------

class TestRunDetailPaginationTests(DjangoTestCase):
    """Pagination on the test-run detail page."""

    def setUp(self):
        self.user = User.objects.create_user("pag_user", password="pw")
        self.client.force_login(self.user)
        model_config = ModelConfig.objects.create(
            name="pag_model", model_name="gpt-4o", provider=Provider.OPENAI,
            created_by=self.user,
        )
        test_case = TestCase.objects.create(name="TC Pag", created_by=self.user)
        version = TestCaseVersion.objects.create(
            test_case=test_case,
            version_number=1,
            original_filename="pag.csv",
            uploaded_by=self.user,
        )
        prompt = PromptTemplate.objects.create(
            test_case=test_case, name="P Pag", template_text="x", created_by=self.user,
        )
        self.run = TestRun.objects.create(
            test_case_version=version,
            prompt_template=prompt,
            model_config=model_config,
            status=RunStatus.COMPLETED,
            rows_total=120,
            rows_completed=120,
            created_by=self.user,
        )
        for i in range(1, 121):
            row = TestCaseRow.objects.create(
                version=version, row_number=i, input_fields={"q": f"q{i}"},
            )
            TestRunResult.objects.create(
                test_run=self.run,
                test_case_row=row,
                status=ResultStatus.SUCCESS,
                raw_response="ok",
            )

    def _url(self, **params):
        url = reverse("core:testrun_detail", kwargs={"pk": self.run.pk})
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return url

    def test_default_page_size_is_50(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_paginated"])
        self.assertEqual(len(response.context["page_results"]), 50)
        self.assertEqual(response.context["page_size"], 50)

    def test_page_2_returns_correct_rows(self):
        response = self.client.get(self._url(page=2))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_results"]), 50)
        self.assertEqual(response.context["page_obj"].number, 2)

    def test_page_3_returns_final_20_rows(self):
        response = self.client.get(self._url(page=3))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_results"]), 20)

    def test_page_size_100(self):
        response = self.client.get(self._url(page_size=100))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_results"]), 100)
        self.assertEqual(response.context["page_size"], 100)

    def test_page_size_clamped_above_100(self):
        response = self.client.get(self._url(page_size=9999))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_size"], 100)

    def test_is_tail_page_false_for_completed_run(self):
        response = self.client.get(self._url())
        self.assertFalse(response.context["is_tail_page"])

    def test_is_tail_page_true_on_last_page_of_active_run(self):
        self.run.status = RunStatus.RUNNING
        self.run.save(update_fields=["status"])
        # 120 rows / 50 per page = 3 pages; page 3 is the tail
        response = self.client.get(self._url(page=3))
        self.assertTrue(response.context["is_tail_page"])

    def test_is_tail_page_false_on_sealed_page_of_active_run(self):
        self.run.status = RunStatus.RUNNING
        self.run.save(update_fields=["status"])
        response = self.client.get(self._url(page=1))
        self.assertFalse(response.context["is_tail_page"])


class TestRunResultsPartialPaginationTests(DjangoTestCase):
    """Paginated HTMX partial view."""

    def setUp(self):
        self.user = User.objects.create_user("partial_pag_user", password="pw")
        self.client.force_login(self.user)
        model_config = ModelConfig.objects.create(
            name="partial_model", model_name="gpt-4o", provider=Provider.OPENAI,
            created_by=self.user,
        )
        test_case = TestCase.objects.create(name="TC Partial", created_by=self.user)
        version = TestCaseVersion.objects.create(
            test_case=test_case,
            version_number=1,
            original_filename="partial.csv",
            uploaded_by=self.user,
        )
        prompt = PromptTemplate.objects.create(
            test_case=test_case, name="P Partial", template_text="z", created_by=self.user,
        )
        self.run = TestRun.objects.create(
            test_case_version=version,
            prompt_template=prompt,
            model_config=model_config,
            status=RunStatus.RUNNING,
            rows_total=60,
            rows_completed=60,
            created_by=self.user,
        )
        for i in range(1, 61):
            row = TestCaseRow.objects.create(
                version=version, row_number=i, input_fields={"q": f"q{i}"},
            )
            TestRunResult.objects.create(
                test_run=self.run,
                test_case_row=row,
                status=ResultStatus.SUCCESS,
                raw_response="ok",
            )

    def _url(self, **params):
        url = reverse("core:testrun_results_partial", kwargs={"pk": self.run.pk})
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return url

    def test_partial_returns_page_1_default(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_results"]), 50)
        self.assertEqual(response.context["page_obj"].number, 1)

    def test_partial_page_2_returns_remainder(self):
        response = self.client.get(self._url(page=2))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_results"]), 10)

    def test_is_tail_page_true_on_last_page(self):
        response = self.client.get(self._url(page=2))
        self.assertTrue(response.context["is_tail_page"])

    def test_is_tail_page_false_on_sealed_page(self):
        response = self.client.get(self._url(page=1))
        self.assertFalse(response.context["is_tail_page"])


class EvaluationRunDetailPaginationTests(DjangoTestCase):
    """Pagination on the evaluation-run detail page."""

    def setUp(self):
        self.user = User.objects.create_user("eval_pag_user", password="pw")
        self.client.force_login(self.user)
        model_config = ModelConfig.objects.create(
            name="eval_pag_model", model_name="gpt-4o", provider=Provider.OPENAI,
            created_by=self.user,
        )
        test_case = TestCase.objects.create(name="TC EvalPag", created_by=self.user)
        version = TestCaseVersion.objects.create(
            test_case=test_case,
            version_number=1,
            original_filename="evalpag.csv",
            uploaded_by=self.user,
        )
        prompt = PromptTemplate.objects.create(
            test_case=test_case, name="P EvalPag", template_text="e", created_by=self.user,
        )
        test_run = TestRun.objects.create(
            test_case_version=version,
            prompt_template=prompt,
            model_config=model_config,
            status=RunStatus.COMPLETED,
            rows_total=75,
            rows_completed=75,
            created_by=self.user,
        )
        eval_config = EvaluationConfig.objects.create(
            test_case=test_case,
            name="EC Pag",
            eval_type=EvalType.KEYWORD_MATCH,
            scoring_criteria={},
            created_by=self.user,
        )
        self.eval_run = EvaluationRun.objects.create(
            evaluation_config=eval_config,
            test_run=test_run,
            status=EvalRunStatus.COMPLETED,
            created_by=self.user,
        )
        for i in range(1, 76):
            row = TestCaseRow.objects.create(
                version=version, row_number=i, input_fields={"q": f"q{i}"},
            )
            trr = TestRunResult.objects.create(
                test_run=test_run,
                test_case_row=row,
                status=ResultStatus.SUCCESS,
                raw_response="ok",
            )
            EvaluationResult.objects.create(
                evaluation_run=self.eval_run,
                test_run_result=trr,
                assessor_type=AssessorType.AI,
                assessor_id="keyword_match",
                assessment={},
            )

    def _url(self, **params):
        url = reverse("core:evaluationrun_detail", kwargs={"pk": self.eval_run.pk})
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return url

    def test_default_page_size_is_50(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_paginated"])
        self.assertEqual(len(response.context["page_results"]), 50)

    def test_page_2_returns_remaining_rows(self):
        response = self.client.get(self._url(page=2))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_results"]), 25)

    def test_page_size_100_fits_all_on_one_page(self):
        response = self.client.get(self._url(page_size=100))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_paginated"])
        self.assertEqual(len(response.context["page_results"]), 75)


# ---------------------------------------------------------------------------
# File bundle upload tests
# ---------------------------------------------------------------------------

class FileBundleParserTests(DjangoTestCase):
    def _bundle(self, entries):
        import zipfile

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, content in entries.items():
                archive.writestr(path, content)
        return buffer.getvalue()

    def test_parses_referenced_pdf_and_ignores_unreferenced_file(self):
        from core.services.bundle_parser import parse_attachment_bundle

        content = self._bundle(
            {
                "reports/a.pdf": b"%PDF-1.7\nexample",
                "unused/document.docx": b"not supported",
            },
        )
        parsed = parse_upload(
            b"input_question,file_report,output_answer\nSummarise,reports/a.pdf,ok\n",
            "cases.csv",
        )

        attachments = parse_attachment_bundle(content, parsed)

        self.assertEqual(parsed["file_columns"], ["file_report"])
        self.assertEqual(
            parsed["rows"][0]["file_fields"], {"file_report": "reports/a.pdf"}
        )
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].relative_path, "reports/a.pdf")
        self.assertEqual(attachments[0].mime_type, "application/pdf")

    def test_referenced_unsupported_file_rejects_bundle(self):
        from core.services.bundle_parser import BundleValidationError, parse_attachment_bundle

        content = self._bundle(
            {"source.docx": b"not supported"},
        )
        parsed = parse_upload(
            b"input_question,file_source\nRead,source.docx\n",
            "cases.csv",
        )

        with self.assertRaises(BundleValidationError) as exc:
            parse_attachment_bundle(content, parsed)
        self.assertIn("source.docx", str(exc.exception))

    def test_rejects_path_traversal_reference(self):
        from core.services.bundle_parser import BundleValidationError, parse_attachment_bundle

        content = self._bundle(
            {"source.pdf": b"%PDF-1.7\nexample"},
        )
        parsed = parse_upload(
            b"input_question,file_source\nRead,../source.pdf\n",
            "cases.csv",
        )

        with self.assertRaises(BundleValidationError) as exc:
            parse_attachment_bundle(content, parsed)
        self.assertIn("Row 1", str(exc.exception))


class FileBundleUploadTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bundle-user", password="testpass123")
        self.client.force_login(self.user)

    def test_upload_persists_one_attachment_and_row_reference(self):
        import zipfile
        from django.core.files.uploadedfile import SimpleUploadedFile

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("reports/a.pdf", b"%PDF-1.7\nexample")

        response = self.client.post(
            reverse("core:testcase_upload"),
            {
                "file": SimpleUploadedFile(
                    "cases.csv",
                    b"input_question,file_report\nSummarise,reports/a.pdf\n",
                    "text/csv",
                ),
                "bundle": SimpleUploadedFile(
                    "attachments.zip", buffer.getvalue(), "application/zip"
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        version = TestCaseVersion.objects.get()
        self.assertEqual(version.file_columns, ["file_report"])
        self.assertEqual(version.attachments.count(), 1)
        self.assertEqual(
            version.rows.get().file_fields, {"file_report": "reports/a.pdf"}
        )

    def test_manifest_file_reference_requires_separate_zip(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            reverse("core:testcase_upload"),
            {
                "file": SimpleUploadedFile(
                    "cases.csv",
                    b"input_question,file_report\nSummarise,reports/a.pdf\n",
                    "text/csv",
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload an attachment ZIP")
        self.assertEqual(TestCaseVersion.objects.count(), 0)


class AttachmentCapabilityTests(DjangoTestCase):
    def test_openai_chat_rejects_pdf_even_when_model_enables_it(self):
        from core.services.llm_client import validate_attachments

        model = ModelConfig(
            name="OpenAI chat",
            provider=Provider.OPENAI,
            model_name="gpt-test",
            attachment_types=["application/pdf"],
        )
        errors = validate_attachments(
            model,
            [{"name": "report.pdf", "mime_type": "application/pdf"}],
        )
        self.assertTrue(errors)
        self.assertIn("Anthropic document adapter", errors[0])

    def test_anthropic_accepts_configured_pdf(self):
        from core.services.llm_client import validate_attachments

        model = ModelConfig(
            name="Claude",
            provider=Provider.ANTHROPIC,
            model_name="claude-test",
            attachment_types=["application/pdf"],
        )
        self.assertEqual(
            validate_attachments(
                model,
                [{"name": "report.pdf", "mime_type": "application/pdf"}],
            ),
            [],
        )

    def test_vllm_accepts_pdf_when_jpeg_is_enabled(self):
        from core.services.llm_client import validate_attachments

        model = ModelConfig(
            name="Qwen vision",
            provider=Provider.VLLM,
            model_name="qwen3.5-9b",
            attachment_types=["image/jpeg"],
        )

        self.assertEqual(
            validate_attachments(
                model,
                [{"name": "report.pdf", "mime_type": "application/pdf"}],
            ),
            [],
        )

    def test_vllm_rasterizes_pdf_into_ordered_jpeg_parts(self):
        from unittest.mock import MagicMock, patch

        from core.services.llm_client import call_llm
        from core.services.pdf_renderer import RenderedPDFPage

        model = ModelConfig(
            name="Qwen vision",
            provider=Provider.VLLM,
            model_name="qwen3.5-9b",
            api_endpoint="http://vllm.test",
            attachment_types=["image/jpeg"],
        )
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "done"}}],
            "usage": {},
        }
        response.headers = {}
        from core.services.pdf_renderer import PDFRenderResult

        pages = PDFRenderResult(
            pages=[
                RenderedPDFPage(b"jpeg-page-one", 1, 2, 2.0),
                RenderedPDFPage(b"jpeg-page-two", 2, 2, 2.0),
            ]
        )

        with patch("core.services.llm_client.render_pdf_pages", return_value=pages):
            with patch("httpx.Client") as mock_client_cls:
                client = MagicMock()
                client.post.return_value = response
                mock_client_cls.return_value.__enter__.return_value = client

                result = call_llm(
                    model,
                    "Read this report",
                    attachments=[{
                        "name": "reports/a.pdf",
                        "mime_type": "application/pdf",
                        "content": b"%PDF-1.7",
                        "sha256": "a" * 64,
                    }],
                )

        content = client.post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Read this report"})
        self.assertEqual(len(content), 3)
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(result["attachment_metadata"][0]["delivery_strategy"], "rasterized_pdf_pages")
        self.assertEqual(result["attachment_metadata"][0]["source_name"], "reports/a.pdf")
        self.assertEqual(result["attachment_metadata"][1]["page_number"], 2)

    def test_vllm_skips_llm_when_pdf_render_fails(self):
        from unittest.mock import MagicMock, patch

        from core.services.llm_client import call_llm
        from core.services.pdf_renderer import PDFRenderError

        model = ModelConfig(
            name="Qwen vision",
            provider=Provider.VLLM,
            model_name="qwen3.5-9b",
            api_endpoint="http://vllm.test",
            attachment_types=["image/jpeg"],
        )

        with patch(
            "core.services.llm_client.render_pdf_pages",
            side_effect=PDFRenderError("PDF could not be opened or is encrypted."),
        ):
            with patch("httpx.Client") as mock_client_cls:
                result = call_llm(
                    model,
                    "Read this report",
                    attachments=[{
                        "name": "reports/a.pdf",
                        "mime_type": "application/pdf",
                        "content": b"%PDF-1.7",
                        "sha256": "a" * 64,
                    }],
                )

        mock_client_cls.assert_not_called()
        self.assertIn("Cannot render PDF attachment", result["error"])
        self.assertEqual(result["text"], "")

    def test_vllm_truncates_pdf_pages_with_warning(self):
        from unittest.mock import MagicMock, patch

        from core.services.llm_client import call_llm
        from core.services.pdf_renderer import PDFRenderResult, RenderedPDFPage

        model = ModelConfig(
            name="Qwen vision",
            provider=Provider.VLLM,
            model_name="qwen3.5-9b",
            api_endpoint="http://vllm.test",
            attachment_types=["image/jpeg"],
        )
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "done"}}],
            "usage": {},
        }
        response.headers = {}
        rendered = PDFRenderResult(
            pages=[RenderedPDFPage(b"jpeg-page-one", 1, 21, 2.0)],
            warnings=(
                "PDF has 21 pages; only the first 20 were sent (model/page limit).",
            ),
        )

        with patch("core.services.llm_client.render_pdf_pages", return_value=rendered):
            with patch("httpx.Client") as mock_client_cls:
                client = MagicMock()
                client.post.return_value = response
                mock_client_cls.return_value.__enter__.return_value = client

                result = call_llm(
                    model,
                    "Read this report",
                    attachments=[{
                        "name": "reports/a.pdf",
                        "mime_type": "application/pdf",
                        "content": b"%PDF-1.7",
                        "sha256": "a" * 64,
                    }],
                )

        self.assertIsNone(result.get("error"))
        self.assertEqual(
            result["warnings"],
            [
                "reports/a.pdf: PDF has 21 pages; only the first 20 were sent "
                "(model/page limit)."
            ],
        )
        content = client.post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(len(content), 2)


class PDFRendererTests(DjangoTestCase):
    @staticmethod
    def _minimal_pdf(page_count: int = 1) -> bytes:
        page_refs = " ".join(f"{3 + index} 0 R" for index in range(page_count))
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{page_refs}] /Count {page_count} >>".encode(),
        ]
        for index in range(page_count):
            content_obj = 3 + page_count + index
            objects.append(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
                f"/Resources << >> /Contents {content_obj} 0 R >>".encode()
            )
        for _ in range(page_count):
            objects.append(b"<< /Length 0 >>\nstream\n\nendstream")
        output = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for index, body in enumerate(objects, start=1):
            offsets.append(len(output))
            output.extend(f"{index} 0 obj\n".encode())
            output.extend(body)
            output.extend(b"\nendobj\n")
        xref_offset = len(output)
        output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
        output.extend(b"0000000000 65535 f \n")
        output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
        output.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n".encode()
        )
        return bytes(output)

    def test_renders_pdf_page_to_jpeg(self):
        from core.services.pdf_renderer import render_pdf_pages

        result = render_pdf_pages(self._minimal_pdf())

        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].page_number, 1)
        self.assertEqual(result.pages[0].page_count, 1)
        self.assertEqual(result.warnings, ())
        self.assertTrue(result.pages[0].content.startswith(b"\xff\xd8\xff"))

    def test_truncates_pages_over_limit_with_warning(self):
        from django.test import override_settings

        from core.services.pdf_renderer import render_pdf_pages

        with override_settings(PDF_MAX_PAGES=1):
            result = render_pdf_pages(self._minimal_pdf(page_count=3))

        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].page_number, 1)
        self.assertEqual(result.pages[0].page_count, 3)
        self.assertEqual(
            result.warnings,
            (
                "PDF has 3 pages; only the first 1 were sent (model/page limit).",
            ),
        )

    def test_rejects_non_positive_rendering_limit(self):
        from django.test import override_settings

        from core.services.pdf_renderer import PDFRenderError, render_pdf_pages

        with override_settings(PDF_MAX_PAGES=0):
            with self.assertRaises(PDFRenderError):
                render_pdf_pages(self._minimal_pdf())

    def test_logs_pdfium_open_failure(self):
        from unittest.mock import patch

        from core.services.pdf_renderer import PDFRenderError, render_pdf_pages

        with patch(
            "core.services.pdf_renderer.pdfium.PdfDocument",
            side_effect=RuntimeError("bad xref"),
        ):
            with patch("core.services.pdf_renderer.logger") as logger:
                with self.assertRaises(PDFRenderError):
                    render_pdf_pages(b"%PDF-1.7\nbroken")
        logger.exception.assert_called_once_with(
            "PDFium could not open an uploaded PDF for rendering."
        )

    def test_pdfium_calls_are_serialised_across_threads(self):
        import threading
        import time
        from unittest.mock import MagicMock, patch

        from core.services.pdf_renderer import PDFRenderError, render_pdf_pages

        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def fake_document(_content):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with counter_lock:
                active -= 1
            document = MagicMock()
            document.__len__.return_value = 0
            return document

        with patch(
            "core.services.pdf_renderer.pdfium.PdfDocument",
            side_effect=fake_document,
        ):
            errors = []

            def worker():
                try:
                    render_pdf_pages(self._minimal_pdf())
                except PDFRenderError:
                    pass
                except Exception as exc:  # noqa: BLE001 - unexpected failures
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(max_active, 1)


class ResourceAccessScopingTests(DjangoTestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.viewer = User.objects.create_user(username="viewer", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.project = TestCase.objects.create(
            name="Private project",
            created_by=self.owner,
            visibility=Visibility.PRIVATE,
        )
        self.private_model = ModelConfig.objects.create(
            name="Private model",
            provider=Provider.LOCAL,
            model_name="local",
            created_by=self.owner,
            visibility=Visibility.PRIVATE,
        )

    def test_private_resources_are_hidden_from_unrelated_users(self):
        from core.access import visible_model_configs, visible_projects

        self.assertFalse(visible_projects(self.other).filter(pk=self.project.pk).exists())
        self.assertFalse(
            visible_model_configs(self.other).filter(pk=self.private_model.pk).exists()
        )

    def test_shared_resources_are_visible_to_granted_user(self):
        from core.access import editable_projects, visible_model_configs, visible_projects
        from core.models import ModelConfigShare

        self.project.visibility = Visibility.SHARED
        self.project.save(update_fields=["visibility"])
        ProjectShare.objects.create(
            project=self.project,
            user=self.viewer,
            role=ShareRole.EDITOR,
        )
        self.private_model.visibility = Visibility.SHARED
        self.private_model.save(update_fields=["visibility"])
        ModelConfigShare.objects.create(
            model_config=self.private_model,
            user=self.viewer,
            role=ShareRole.VIEWER,
        )

        self.assertTrue(visible_projects(self.viewer).filter(pk=self.project.pk).exists())
        self.assertTrue(editable_projects(self.viewer).filter(pk=self.project.pk).exists())
        self.assertTrue(
            visible_model_configs(self.viewer).filter(pk=self.private_model.pk).exists()
        )

    def test_private_project_detail_returns_not_found(self):
        self.client.force_login(self.other)
        response = self.client.get(
            reverse("core:testcase_detail", kwargs={"pk": self.project.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_staff_can_access_private_resources(self):
        from core.access import visible_model_configs, visible_projects

        self.assertTrue(visible_projects(self.staff).filter(pk=self.project.pk).exists())
        self.assertTrue(
            visible_model_configs(self.staff).filter(pk=self.private_model.pk).exists()
        )

    def test_owner_can_share_a_project_with_multiple_users(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("core:testcase_share", kwargs={"pk": self.project.pk}),
            {
                "users": [str(self.viewer.pk), str(self.other.pk)],
                "role": ShareRole.VIEWER,
            },
        )

        self.assertRedirects(
            response,
            reverse("core:testcase_detail", kwargs={"pk": self.project.pk}),
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Visibility.SHARED)
        self.assertEqual(self.project.shares.count(), 2)


class FieldMatchBuilderTests(DjangoTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fieldmatch", password="testpass123")
        self.test_case = TestCase.objects.create(
            name="Structured output",
            created_by=self.user,
        )
        TestCaseVersion.objects.create(
            test_case=self.test_case,
            version_number=1,
            original_filename="expected-values.xlsx",
            column_names=["input_note", "output_code", "output_summary"],
            input_columns=["input_note"],
            output_columns=["output_code", "output_summary"],
            row_count=1,
            uploaded_by=self.user,
        )

    def test_create_config_shows_output_column_multi_select_for_field_match(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse(
                "core:evaluationconfig_create",
                kwargs={"test_case_id": self.test_case.pk},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="fm-column-picker"')
        self.assertContains(response, 'id="fm-select-all-columns"')
        self.assertContains(response, "Add selected fields")
        self.assertContains(response, "output_code")
        self.assertContains(response, "output_summary")

    def test_create_form_error_preserves_field_match_builder_data(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse(
                "core:evaluationconfig_create",
                kwargs={"test_case_id": self.test_case.pk},
            ),
            {
                "name": "",
                "eval_type": EvalType.FIELD_MATCH,
                "scoring_criteria": (
                    '{"fields": [{"name": "output_code", "match_type": "exact"}]}'
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Name is required.")
        self.assertEqual(
            response.context["form_data"]["scoring_criteria_json"],
            '{"fields": [{"name": "output_code", "match_type": "exact"}]}',
        )

    def test_edit_form_error_preserves_unsaved_field_match_builder_data(self):
        config = EvaluationConfig.objects.create(
            test_case=self.test_case,
            name="Existing config",
            eval_type=EvalType.FIELD_MATCH,
            scoring_criteria={"fields": [{"name": "output_summary"}]},
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("core:evaluationconfig_edit", kwargs={"pk": config.pk}),
            {
                "name": "",
                "eval_type": EvalType.FIELD_MATCH,
                "scoring_criteria": (
                    '{"fields": [{"name": "output_code", "match_type": "llm_judge"}]}'
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Name is required.")
        self.assertEqual(
            response.context["form_data"]["scoring_criteria_json"],
            '{"fields": [{"name": "output_code", "match_type": "llm_judge"}]}',
        )


class FieldMatchScorerTests(DjangoTestCase):
    def test_strip_edge_punctuation_option_normalizes_exact_values(self):
        from types import SimpleNamespace

        from core.services.scorer import score_field_match

        result = SimpleNamespace(
            response_parsed={"output_code": '  "A12!"  '},
            raw_response="",
        )
        fields = [{"name": "output_code", "match_type": "exact"}]

        self.assertEqual(
            score_field_match(
                result,
                {"output_code": "A12"},
                fields,
                case_sensitive=True,
            ),
            {"output_code": False},
        )
        self.assertEqual(
            score_field_match(
                result,
                {"output_code": "A12"},
                fields,
                case_sensitive=True,
                strip_edge_punctuation=True,
            ),
            {"output_code": True},
        )
