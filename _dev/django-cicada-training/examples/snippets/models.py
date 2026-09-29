"""Illustrative models only — not imported by Cicada.

A Django model is both:
1. a Python class used by application code; and
2. a schema description from which Django creates a database table by migration.

The production Cicada equivalents are TestCase (called Project in the UI),
TestCaseVersion, and TestCaseRow in core/models.py.
"""

import uuid

from django.conf import settings
from django.db import models


class Project(models.Model):
    """A container that groups a dataset, prompts, runs, and evaluations."""

    # UUIDs are safe to expose in URLs and do not reveal how many projects exist.
    # primary_key=True means this field replaces Django's default integer `id`.
    # default=uuid.uuid4 creates an ID when a Project is first saved.
    # editable=False prevents this implementation detail appearing in generated forms.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # CharField is a short string column. max_length is required and is enforced
    # by forms and database schema where supported.
    name = models.CharField(max_length=255)

    # ForeignKey means “many projects can belong to one user”.
    # settings.AUTH_USER_MODEL avoids assuming the project uses Django's built-in
    # User class directly.
    # SET_NULL preserves a project if its original user account is removed.
    # null=True permits the database value to be empty in that rare situation.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_projects",
    )

    # DateTimeField records when Django first saves the row. auto_now_add is
    # suitable for immutable creation timestamps (not for “last modified” time).
    created_at = models.DateTimeField(auto_now_add=True)

    # Meta is model configuration, not a database field. Ordering makes
    # Project.objects.all() return newest first unless a query overrides it.
    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        # Used by Django admin, shell output, and choice fields.
        return self.name


class ProjectRow(models.Model):
    """One structured example: flexible input and expected-output payloads."""

    # Every row belongs to exactly one project. CASCADE says deleting a project
    # also deletes its rows. related_name="rows" creates project.rows in Python.
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="rows"
    )

    # PositiveIntegerField is a non-negative integer. It represents the row's
    # human-friendly position in an uploaded data file, not its database key.
    row_number = models.PositiveIntegerField()

    # JSONField stores JSON-compatible Python values: dict, list, str, number,
    # bool, or None. It is useful when column names vary between projects.
    # Example input: {"input_note": "Illustrative note", "input_age": 52}
    input_fields = models.JSONField(default=dict)

    # Expected labels belong beside inputs so an evaluation can compare a model
    # result with the correct answer for the same row.
    # Example expected output: {"output_primary_label": "positive"}
    expected_output_fields = models.JSONField(default=dict)

    class Meta:
        # A database constraint enforces a business rule even if two requests
        # arrive at the same time: each project can have row number 1 only once.
        constraints = [
            models.UniqueConstraint(
                fields=["project", "row_number"], name="one_row_number_per_project"
            )
        ]

        # Explicitly declare how related rows should be returned by default.
        ordering = ["row_number"]
