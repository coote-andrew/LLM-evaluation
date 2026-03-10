"""Prompt template views."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import CreateView, DetailView, UpdateView

from core.forms import PromptTemplateForm
from core.models import PromptTemplate, TestCase


class PromptTemplateCreateView(LoginRequiredMixin, CreateView):
    """Create prompt template for a test case."""

    model = PromptTemplate
    form_class = PromptTemplateForm
    template_name = "core/prompttemplate_form.html"

    def get_test_case(self):
        return get_object_or_404(TestCase, pk=self.kwargs["test_case_id"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["test_case"] = self.get_test_case()
        return context

    def form_valid(self, form):
        form.instance.test_case = self.get_test_case()
        form.instance.created_by = self.request.user
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("core:testcase_detail", kwargs={"pk": self.object.test_case_id})


class PromptTemplateUpdateView(LoginRequiredMixin, UpdateView):
    """Edit prompt template."""

    model = PromptTemplate
    form_class = PromptTemplateForm
    template_name = "core/prompttemplate_form.html"
    context_object_name = "prompt_template"

    def get_success_url(self):
        return reverse("core:testcase_detail", kwargs={"pk": self.object.test_case_id})
