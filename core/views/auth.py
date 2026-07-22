"""Authentication-related views."""

from django import forms
from django.contrib import messages
from django.contrib.auth.views import LoginView, PasswordChangeView, redirect_to_login
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic.edit import FormView
from urllib.parse import urlencode

from core.models import UserProfile


def _safe_next_url(request, url):
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}):
        return url
    return "/"


def _user_must_change_password(user):
    if not user.is_authenticated:
        return False
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile.must_change_password


class RegisterForm(forms.Form):
    username = forms.CharField(max_length=150)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)
    must_change_password = forms.BooleanField(
        label="Require password change on first login",
        required=False,
        help_text="Use this when creating a premade account with a temporary password.",
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned


class FirstLoginPasswordChangeLoginView(LoginView):
    """Login view that sends temporary-password users to password change first."""

    template_name = "registration/login.html"

    def get_success_url(self):
        success_url = super().get_success_url()
        if _user_must_change_password(self.request.user):
            query = urlencode({"next": success_url})
            return f"{reverse('password_change')}?{query}"
        return success_url


class ForcedPasswordChangeView(PasswordChangeView):
    """Clear the first-login password-change flag after a successful change."""

    template_name = "registration/password_change_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["next"] = self.request.GET.get("next") or self.request.POST.get("next") or "/"
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])
        messages.success(self.request, "Your password has been changed.")
        return response

    def get_success_url(self):
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        return _safe_next_url(self.request, next_url)


class RegisterView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "registration/register.html"
    form_class = RegisterForm
    raise_exception = True

    def test_func(self):
        return self.request.user.is_superuser

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                self.get_login_url(),
                self.get_redirect_field_name(),
            )
        raise PermissionDenied

    def form_valid(self, form):
        user = User.objects.create_user(
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password1"],
        )
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.must_change_password = form.cleaned_data["must_change_password"]
        profile.save(update_fields=["must_change_password"])

        if profile.must_change_password:
            messages.success(
                self.request,
                f"Created {user.username}. Share the temporary password and ask them to sign in.",
            )
        else:
            messages.success(self.request, f"Created {user.username}.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return "/"
