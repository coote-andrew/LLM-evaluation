"""Link existing Django users to Entra IDs from an administrator-reviewed CSV."""

import csv
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.models import EntraIdentity


class Command(BaseCommand):
    help = (
        "Preview or apply reviewed Django username-to-Entra identity links from CSV. "
        "Required columns: username,tenant_id,object_id. Optional: upn."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to the reviewed CSV mapping file.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create the links. Without this flag, no database changes are made.",
        )

    def handle(self, *args, **options):
        try:
            with open(options["csv_path"], newline="", encoding="utf-8-sig") as csv_file:
                rows = list(csv.DictReader(csv_file))
        except OSError as error:
            raise CommandError(f"Could not read mapping CSV: {error}") from error

        required_columns = {"username", "tenant_id", "object_id"}
        if not rows:
            raise CommandError("Mapping CSV has no data rows.")
        if not required_columns.issubset(rows[0]):
            raise CommandError(
                "Mapping CSV must contain columns: username, tenant_id, object_id."
            )

        user_model = get_user_model()
        valid_rows = []
        errors = []
        seen_users = set()
        seen_entra_ids = set()
        for line_number, row in enumerate(rows, start=2):
            username = row["username"].strip()
            tenant_id = row["tenant_id"].strip()
            object_id = row["object_id"].strip()
            try:
                uuid.UUID(tenant_id)
                uuid.UUID(object_id)
            except ValueError:
                errors.append(f"line {line_number}: tenant_id and object_id must be UUIDs")
                continue
            if username in seen_users:
                errors.append(f"line {line_number}: duplicate username {username!r}")
                continue
            if (tenant_id, object_id) in seen_entra_ids:
                errors.append(f"line {line_number}: duplicate Entra tenant/object ID")
                continue
            try:
                user = user_model.objects.get(username=username)
            except user_model.DoesNotExist:
                errors.append(f"line {line_number}: local user {username!r} does not exist")
                continue
            if EntraIdentity.objects.filter(user=user).exists():
                errors.append(f"line {line_number}: local user {username!r} is already linked")
                continue
            if EntraIdentity.objects.filter(
                tenant_id=tenant_id, object_id=object_id
            ).exists():
                errors.append(f"line {line_number}: Entra identity is already linked")
                continue
            seen_users.add(username)
            seen_entra_ids.add((tenant_id, object_id))
            valid_rows.append((user, tenant_id, object_id, row.get("upn", "").strip()))

        for error in errors:
            self.stderr.write(self.style.ERROR(error))
        if errors:
            raise CommandError("No identities were linked because the CSV has errors.")

        action = "Would link" if not options["apply"] else "Linking"
        self.stdout.write(f"{action} {len(valid_rows)} Entra identity record(s).")
        if not options["apply"]:
            self.stdout.write("Dry run only. Re-run with --apply after review.")
            return

        for user, tenant_id, object_id, upn in valid_rows:
            EntraIdentity.objects.create(
                user=user,
                tenant_id=tenant_id,
                object_id=object_id,
                issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
                last_known_upn=upn,
            )
        self.stdout.write(self.style.SUCCESS("Entra identity links created."))
