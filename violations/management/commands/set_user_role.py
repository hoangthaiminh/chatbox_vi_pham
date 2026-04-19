from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from violations.services import (
    ROLE_ROOM_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_VIEWER,
    apply_user_role,
    ensure_valid_role_room,
    format_role_assignment_success,
)


class Command(BaseCommand):
    help = "Assign role/group for a user and optionally set exam room for room admin."

    def add_arguments(self, parser):
        parser.add_argument("username", type=str)
        parser.add_argument(
            "--role",
            choices=[ROLE_SUPER_ADMIN, ROLE_ROOM_ADMIN, ROLE_VIEWER],
            required=True,
            help="Role to assign",
        )
        parser.add_argument(
            "--room",
            type=str,
            default="",
            help="Room code/name (required for room_admin)",
        )

    def handle(self, *args, **options):
        username = options["username"]
        role = options["role"]
        room_name = (options["room"] or "").strip()

        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist as exc:
            raise CommandError(f"User '{username}' does not exist.") from exc

        try:
            room_name = ensure_valid_role_room(role, room_name)
        except ValueError as exc:
            raise CommandError(f"--room is required for role {ROLE_ROOM_ADMIN}") from exc

        apply_user_role(user, role, room_name)
        self.stdout.write(self.style.SUCCESS(format_role_assignment_success(username, role, room_name)))
