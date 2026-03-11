"""
Core app tests.

Consolidated here to avoid unittest discovery conflicts with core/tests/ package.
"""

from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase
from django.urls import reverse

from core.models import (
    ModelConfig,
    PromptTemplate,
    Provider,
    ResponseFormat,
    RunStatus,
    TestCase,
    TestCaseRow,
    TestCaseVersion,
    TestRun,
)
from core.services.csv_parser import parse_csv, parse_excel, parse_upload
from core.services.prompt_builder import build_prompt, get_placeholder_names, validate_template

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
