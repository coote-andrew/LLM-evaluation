"""Model configuration views."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, UpdateView

from core.forms import ModelConfigForm
from core.models import ModelConfig


class ModelConfigListView(LoginRequiredMixin, ListView):
    """List model configurations."""

    model = ModelConfig
    template_name = "core/modelconfig_list.html"
    context_object_name = "model_configs"


class ModelConfigCreateView(LoginRequiredMixin, CreateView):
    """Create model configuration."""

    model = ModelConfig
    form_class = ModelConfigForm
    template_name = "core/modelconfig_form.html"
    success_url = reverse_lazy("core:modelconfig_list")

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        return super().form_valid(form)


class ModelConfigUpdateView(LoginRequiredMixin, UpdateView):
    """Edit model configuration."""

    model = ModelConfig
    form_class = ModelConfigForm
    template_name = "core/modelconfig_form.html"
    context_object_name = "model_config"
    success_url = reverse_lazy("core:modelconfig_list")
