"""Central application-level authorization for projects and model configs."""

from django.db.models import Q

from core.models import (
    EvaluationConfig,
    EvaluationRun,
    ModelConfig,
    Project,
    PromptTemplate,
    ShareRole,
    TestRun,
    Visibility,
)


def _is_staff(user):
    return bool(getattr(user, "is_authenticated", False) and user.is_staff)


def visible_projects(user):
    """Projects available to a user, including public and explicit shares."""
    if _is_staff(user):
        return Project.objects.all()
    if not getattr(user, "is_authenticated", False):
        return Project.objects.none()
    return Project.objects.filter(
        Q(visibility=Visibility.PUBLIC)
        | Q(created_by=user)
        | Q(visibility=Visibility.SHARED, shares__user=user)
    ).distinct()


def editable_projects(user):
    """Projects whose configuration and child data a user may change."""
    if _is_staff(user):
        return Project.objects.all()
    if not getattr(user, "is_authenticated", False):
        return Project.objects.none()
    return Project.objects.filter(
        Q(created_by=user)
        | Q(
            visibility=Visibility.SHARED,
            shares__user=user,
            shares__role=ShareRole.EDITOR,
        )
    ).distinct()


def manageable_projects(user):
    """Projects whose visibility, owner, and shares a user may administer."""
    if _is_staff(user):
        return Project.objects.all()
    if not getattr(user, "is_authenticated", False):
        return Project.objects.none()
    return Project.objects.filter(created_by=user)


def visible_model_configs(user):
    """Model configurations a user can select or inspect."""
    if _is_staff(user):
        return ModelConfig.objects.all()
    if not getattr(user, "is_authenticated", False):
        return ModelConfig.objects.none()
    return ModelConfig.objects.filter(
        Q(visibility=Visibility.PUBLIC)
        | Q(created_by=user)
        | Q(visibility=Visibility.SHARED, shares__user=user)
    ).distinct()


def editable_model_configs(user):
    """Model configurations a user may update."""
    if _is_staff(user):
        return ModelConfig.objects.all()
    if not getattr(user, "is_authenticated", False):
        return ModelConfig.objects.none()
    return ModelConfig.objects.filter(
        Q(created_by=user)
        | Q(
            visibility=Visibility.SHARED,
            shares__user=user,
            shares__role=ShareRole.EDITOR,
        )
    ).distinct()


def manageable_model_configs(user):
    """Model configurations whose visibility, owner, and shares can be managed."""
    if _is_staff(user):
        return ModelConfig.objects.all()
    if not getattr(user, "is_authenticated", False):
        return ModelConfig.objects.none()
    return ModelConfig.objects.filter(created_by=user)


def visible_prompt_templates(user):
    """Active prompt templates available for use in new runs."""
    return PromptTemplate.objects.filter(
        test_case__in=visible_projects(user),
        is_active=True,
    )


def visible_evaluation_configs(user):
    return EvaluationConfig.objects.filter(test_case__in=visible_projects(user))


def visible_test_runs(user):
    return TestRun.objects.filter(
        test_case_version__test_case__in=visible_projects(user)
    )


def visible_evaluation_runs(user):
    return EvaluationRun.objects.filter(
        test_run__test_case_version__test_case__in=visible_projects(user)
    )
