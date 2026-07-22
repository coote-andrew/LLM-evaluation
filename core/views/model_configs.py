"""Model configuration views."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from core.access import (
    editable_model_configs,
    manageable_model_configs,
    visible_model_configs,
)
from core.forms import ModelConfigForm, ShareForm
from core.models import ModelConfig, ModelConfigShare, Visibility


class ModelConfigListView(LoginRequiredMixin, ListView):
    """List model configurations."""

    model = ModelConfig
    template_name = "core/modelconfig_list.html"
    context_object_name = "model_configs"

    def get_queryset(self):
        return visible_model_configs(self.request.user).select_related("created_by")


class ModelConfigCreateView(LoginRequiredMixin, CreateView):
    """Create model configuration."""

    model = ModelConfig
    form_class = ModelConfigForm
    template_name = "core/modelconfig_form.html"
    success_url = reverse_lazy("core:modelconfig_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        if not self.request.user.is_staff:
            form.instance.visibility = Visibility.PRIVATE
        return super().form_valid(form)


class ModelConfigUpdateView(LoginRequiredMixin, UpdateView):
    """Edit model configuration."""

    model = ModelConfig
    form_class = ModelConfigForm
    template_name = "core/modelconfig_form.html"
    context_object_name = "model_config"
    success_url = reverse_lazy("core:modelconfig_list")

    def get_queryset(self):
        return editable_model_configs(self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if manageable_model_configs(self.request.user).filter(pk=self.object.pk).exists():
            context["share_form"] = ShareForm(owner=self.object.created_by)
        return context


class ModelConfigShareView(LoginRequiredMixin, View):
    """Add or update an explicit model configuration share."""

    def post(self, request, pk):
        model_config = get_object_or_404(manageable_model_configs(request.user), pk=pk)
        form = ShareForm(request.POST, owner=model_config.created_by)
        if form.is_valid():
            for user in form.cleaned_data["users"]:
                ModelConfigShare.objects.update_or_create(
                    model_config=model_config,
                    user=user,
                    defaults={"role": form.cleaned_data["role"]},
                )
            if model_config.visibility == Visibility.PRIVATE:
                model_config.visibility = Visibility.SHARED
                model_config.save(update_fields=["visibility"])
            messages.success(request, "Model sharing updated.")
        else:
            messages.error(request, "Unable to update model sharing.")
        return redirect("core:modelconfig_edit", pk=model_config.pk)
