"""Prompt template views."""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import CreateView, View

from core.access import editable_projects
from core.forms import PromptTemplateForm
from core.models import PromptTemplate, TestCase


def _get_input_columns_for_test_case(test_case):
    """Return input columns from the latest version of a test case, or empty list."""
    latest = test_case.versions.first()
    return latest.input_columns if latest else []


class PromptTemplateCreateView(LoginRequiredMixin, CreateView):
    """Create prompt template for a test case (always version 1)."""

    model = PromptTemplate
    form_class = PromptTemplateForm
    template_name = "core/prompttemplate_form.html"

    def get_test_case(self):
        return get_object_or_404(
            editable_projects(self.request.user),
            pk=self.kwargs["test_case_id"],
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        test_case = self.get_test_case()
        context["test_case"] = test_case
        context["input_columns"] = _get_input_columns_for_test_case(test_case)
        return context

    def form_valid(self, form):
        test_case = self.get_test_case()
        form.instance.test_case = test_case
        form.instance.created_by = self.request.user
        form.instance.version_number = 1
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("core:testcase_detail", kwargs={"pk": self.object.test_case_id})


class PromptTemplateUpdateView(LoginRequiredMixin, View):
    """Edit a prompt template by creating a new version (immutable history).

    GET  → render the form pre-filled from the existing template.
    POST → validate and save a *new* PromptTemplate row with version_number+1.
    """

    template_name = "core/prompttemplate_form.html"

    def _get_template(self):
        return get_object_or_404(
            PromptTemplate.objects.filter(
                test_case__in=editable_projects(self.request.user)
            ),
            pk=self.kwargs["pk"],
        )

    def get(self, request, *args, **kwargs):
        from django.shortcuts import render
        existing = self._get_template()
        form = PromptTemplateForm(instance=existing)
        return render(request, self.template_name, {
            "form": form,
            "prompt_template": existing,
            "test_case": existing.test_case,
            "input_columns": _get_input_columns_for_test_case(existing.test_case),
            "is_update": True,
        })

    def post(self, request, *args, **kwargs):
        from django.shortcuts import render
        existing = self._get_template()
        form = PromptTemplateForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {
                "form": form,
                "prompt_template": existing,
                "test_case": existing.test_case,
                "input_columns": _get_input_columns_for_test_case(existing.test_case),
                "is_update": True,
            })

        # Determine the highest existing version for this (test_case, name).
        # The name in the form may differ from the existing one if the user renamed it;
        # in that case we treat it as a new template series starting at v1.
        new_name = form.cleaned_data["name"]
        existing_max = (
            PromptTemplate.objects
            .filter(test_case=existing.test_case, name=new_name)
            .order_by("-version_number")
            .values_list("version_number", flat=True)
            .first()
        )
        next_version = (existing_max or 0) + 1

        new_template = PromptTemplate.objects.create(
            test_case=existing.test_case,
            name=new_name,
            version_number=next_version,
            parent_template=existing if next_version > 1 else None,
            template_text=form.cleaned_data["template_text"],
            response_format=form.cleaned_data["response_format"],
            created_by=request.user,
        )
        messages.success(
            request,
            f"Saved as \"{new_name}\" v{next_version}. Previous version is preserved.",
        )
        return redirect(
            reverse("core:testcase_detail", kwargs={"pk": new_template.test_case_id})
        )


class PromptTemplateDeleteView(LoginRequiredMixin, View):
    """Delete a specific prompt template version."""

    def post(self, request, *args, **kwargs):
        pt = get_object_or_404(
            PromptTemplate.objects.filter(
                test_case__in=editable_projects(request.user)
            ),
            pk=self.kwargs["pk"],
        )
        test_case_id = pt.test_case_id
        version = pt.version_number
        name = pt.name
        pt.delete()
        messages.success(request, f'Deleted "{name}" v{version}.')
        return redirect(reverse("core:testcase_detail", kwargs={"pk": test_case_id}))
