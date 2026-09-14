"""Move an Entra identity from an auto-provisioned account to an existing user."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.models import EntraIdentity


class Command(BaseCommand):
    help = (
        "Transfer an Entra identity from an auto-provisioned local user to an "
        "existing local user, preserving the target user's RBAC."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "source_username",
            help="Current SSO-created username, for example entra_<object-id>.",
        )
        parser.add_argument(
            "target_username",
            help="Existing local username whose RBAC should be retained.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Transfer the identity. Without this flag, no database changes are made.",
        )

    def handle(self, *args, **options):
        source_username = options["source_username"]
        target_username = options["target_username"]
        if source_username == target_username:
            raise CommandError("Source and target usernames must be different.")

        user_model = get_user_model()
        try:
            source_user = user_model.objects.get(username=source_username)
        except user_model.DoesNotExist as error:
            raise CommandError(
                f"Source user {source_username!r} does not exist."
            ) from error
        try:
            target_user = user_model.objects.get(username=target_username)
        except user_model.DoesNotExist as error:
            raise CommandError(
                f"Target user {target_username!r} does not exist."
            ) from error
        try:
            identity = EntraIdentity.objects.get(user=source_user)
        except EntraIdentity.DoesNotExist as error:
            raise CommandError(
                f"Source user {source_username!r} has no Entra identity."
            ) from error
        if EntraIdentity.objects.filter(user=target_user).exists():
            raise CommandError(
                f"Target user {target_username!r} already has an Entra identity."
            )

        if not options["apply"]:
            self.stdout.write(
                f"Would transfer Entra identity from {source_username!r} "
                f"to {target_username!r}. Dry run only."
            )
            return

        identity.user = target_user
        identity.save(update_fields=["user"])
        source_user.is_active = False
        source_user.save(update_fields=["is_active"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Transferred Entra identity to {target_username!r}. "
                f"The former SSO-created user {source_username!r} was retained "
                "but deactivated."
            )
        )
