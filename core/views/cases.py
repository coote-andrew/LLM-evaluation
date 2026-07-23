"""Test case views: list, detail, upload CSV."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from core.access import editable_projects, manageable_projects, visible_projects
from core.forms import ProjectForm, ShareForm, TestCaseUploadForm
from core.models import (
    EvaluationConfig,
    PromptTemplate,
    TestCase,
    TestCaseAttachment,
    TestCaseRow,
    ProjectShare,
    TestCaseVersion,
    Visibility,
)
from core.services.bundle_parser import BundleValidationError, parse_attachment_bundle
from core.services.csv_parser import parse_upload


def _group_prompt_templates(test_case, include_inactive=False):
    """Return a list of dicts, one per unique template name, newest version first.

    Each dict has:
      - ``latest``: the PromptTemplate with the highest version_number for this name
      - ``older``: list of older versions ordered newest-first (may be empty)
    """
    all_pts = PromptTemplate.objects.filter(test_case=test_case)
    if not include_inactive:
        all_pts = all_pts.filter(is_active=True)
    all_pts = list(all_pts.order_by("name", "-version_number"))
    groups = {}
    for pt in all_pts:
        if pt.name not in groups:
            groups[pt.name] = {"latest": pt, "older": []}
        else:
            groups[pt.name]["older"].append(pt)
    return list(groups.values())


class TestCaseListView(LoginRequiredMixin, ListView):
    """List all test cases."""

    model = TestCase
    template_name = "core/testcase_list.html"
    context_object_name = "test_cases"

    def get_queryset(self):
        return visible_projects(self.request.user)


class TestCaseDetailView(LoginRequiredMixin, DetailView):
    """Detail view: versions, rows, linked templates."""

    model = TestCase
    template_name = "core/testcase_detail.html"
    context_object_name = "test_case"

    def get_queryset(self):
        return visible_projects(self.request.user).prefetch_related("versions__attachments")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        show_inactive = self.request.GET.get("show_inactive") == "1"
        context["prompt_template_groups"] = _group_prompt_templates(
            self.object, include_inactive=show_inactive
        )
        context["show_inactive_prompts"] = show_inactive
        context["eval_configs"] = EvaluationConfig.objects.filter(
            test_case=self.object,
            is_current=True,
        ).order_by("name")
        if manageable_projects(self.request.user).filter(pk=self.object.pk).exists():
            context["share_form"] = ShareForm(owner=self.object.created_by)
        return context


class TestCaseCreateView(LoginRequiredMixin, CreateView):
    """Create test case (without upload)."""

    model = TestCase
    template_name = "core/testcase_form.html"
    form_class = ProjectForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        if not self.request.user.is_staff:
            form.instance.visibility = Visibility.PRIVATE
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("core:testcase_detail", kwargs={"pk": self.object.pk})


class TestCaseUpdateView(LoginRequiredMixin, UpdateView):
    """Rename or update a project when the user has editor access."""

    model = TestCase
    form_class = ProjectForm
    template_name = "core/testcase_form.html"

    def get_queryset(self):
        return editable_projects(self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self):
        return reverse("core:testcase_detail", kwargs={"pk": self.object.pk})


class TestCaseDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a test case and all its versions/rows."""

    model = TestCase
    success_url = reverse_lazy("core:testcase_list")

    def get_queryset(self):
        return editable_projects(self.request.user)

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)

    def form_valid(self, form):
        tc = self.get_object()
        messages.success(self.request, f"Test case \"{tc.name}\" deleted.")
        return super().form_valid(form)


class TestCaseShareView(LoginRequiredMixin, View):
    """Add or update an explicit project share."""

    def post(self, request, pk):
        project = get_object_or_404(manageable_projects(request.user), pk=pk)
        form = ShareForm(request.POST, owner=project.created_by)
        if form.is_valid():
            for user in form.cleaned_data["users"]:
                ProjectShare.objects.update_or_create(
                    project=project,
                    user=user,
                    defaults={"role": form.cleaned_data["role"]},
                )
            if project.visibility == Visibility.PRIVATE:
                project.visibility = Visibility.SHARED
                project.save(update_fields=["visibility"])
            messages.success(request, "Project sharing updated.")
        else:
            messages.error(request, "Unable to update project sharing.")
        return redirect("core:testcase_detail", pk=project.pk)


@login_required
def upload_csv_view(request):
    """Upload a CSV/Excel manifest and optional attachment ZIP to a test case."""
    if request.method == "GET":
        initial = {}
        if tc_id := request.GET.get("test_case"):
            try:
                tc = editable_projects(request.user).get(pk=tc_id)
                initial["test_case"] = tc
            except (TestCase.DoesNotExist, ValueError):
                pass
        return render(request, "core/testcase_upload.html", {
            "form": TestCaseUploadForm(initial=initial, user=request.user),
            "pos_values": ["true", "yes", "1", "positive", "present"],
            "neg_values": ["false", "no", "0", "negative", "absent"],
        })

    form = TestCaseUploadForm(request.POST, request.FILES, user=request.user)
    if not form.is_valid():
        return render(request, "core/testcase_upload.html", {
            "form": form,
            "pos_values": ["true", "yes", "1", "positive", "present"],
            "neg_values": ["false", "no", "0", "negative", "absent"],
        })

    test_case = form.cleaned_data.get("test_case")
    file_obj = request.FILES["file"]
    content = file_obj.read()
    filename = file_obj.name
    bundle_obj = form.cleaned_data.get("bundle")

    raw_group_by = form.cleaned_data.get("group_by_columns") or ""
    group_by_columns = [c.strip() for c in raw_group_by.split(",") if c.strip()] or None
    sort_by_column = form.cleaned_data.get("sort_by_column") or None

    try:
        flat = parse_upload(content, filename)
        if group_by_columns:
            missing = [c for c in group_by_columns if c not in flat["input_columns"]]
            if missing:
                form.add_error(
                    "group_by_columns",
                    "Group by column(s) not found in file: "
                    f"{', '.join(missing)}. Check spelling and ensure the column "
                    "is prefixed with input_.",
                )
                return render(request, "core/testcase_upload.html", {
                    "form": form,
                    "pos_values": ["true", "yes", "1", "positive", "present"],
                    "neg_values": ["false", "no", "0", "negative", "absent"],
                })
        parsed = parse_upload(
            content,
            filename,
            group_by_columns=group_by_columns,
            sort_by_column=sort_by_column,
        )
        has_file_references = any(
            bool(row.get("file_fields")) for row in parsed["rows"]
        )
        if has_file_references and not bundle_obj:
            form.add_error(
                "bundle",
                "Upload an attachment ZIP because this manifest contains file_ references.",
            )
            return render(request, "core/testcase_upload.html", {
                "form": form,
                "pos_values": ["true", "yes", "1", "positive", "present"],
                "neg_values": ["false", "no", "0", "negative", "absent"],
            })
        if bundle_obj:
            attachments = parse_attachment_bundle(
                bundle_obj.read(),
                parsed,
            )
        else:
            attachments = []
    except BundleValidationError as exc:
        for error in exc.errors:
            form.add_error("bundle", error)
        return render(request, "core/testcase_upload.html", {
            "form": form,
            "pos_values": ["true", "yes", "1", "positive", "present"],
            "neg_values": ["false", "no", "0", "negative", "absent"],
        })

    if not parsed["input_columns"]:
        form.add_error("file", "No columns prefixed with 'input_' found. Check your file format.")
        return render(request, "core/testcase_upload.html", {
            "form": form,
            "pos_values": ["true", "yes", "1", "positive", "present"],
            "neg_values": ["false", "no", "0", "negative", "absent"],
        })

    with transaction.atomic():
        if not test_case:
            test_case = TestCase.objects.create(
                name=parsed["original_filename"].rsplit(".", 1)[0],
                description="",
                created_by=request.user,
                visibility=Visibility.PRIVATE,
            )
        agg = test_case.versions.aggregate(max_v=Max("version_number"))
        next_version = (agg.get("max_v") or 0) + 1

        version = TestCaseVersion.objects.create(
            test_case=test_case,
            version_number=next_version,
            original_filename=parsed["original_filename"],
            column_names=parsed["column_names"],
            input_columns=parsed["input_columns"],
            output_columns=parsed["output_columns"],
            file_columns=parsed.get("file_columns", []),
            row_count=parsed["row_count"],
            uploaded_by=request.user,
        )
        for row_data in parsed["rows"]:
            TestCaseRow.objects.create(
                version=version,
                row_number=row_data["row_number"],
                input_fields=row_data["input_fields"],
                expected_output_fields=row_data["expected_output_fields"],
                file_fields=row_data.get("file_fields", {}),
            )
        for attachment in attachments:
            TestCaseAttachment.objects.create(
                version=version,
                relative_path=attachment.relative_path,
                file=ContentFile(attachment.content, name=attachment.relative_path),
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                sha256=attachment.sha256,
            )

    messages.success(
        request,
        f"Uploaded {parsed['row_count']} rows as version {next_version}."
        + (f" Stored {len(attachments)} referenced attachment(s)." if attachments else ""),
    )
    return redirect("core:testcase_detail", pk=test_case.pk)
