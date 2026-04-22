"""
Management command: bulk_create_room_admins

Tạo hàng loạt user với vai trò Quản trị phòng (room_admin).

Cách dùng:
    python manage.py bulk_create_room_admins \
        --users '[{"username":"hoangnam","password":"prets10","room":"P01"}]' \
        [--update]

Hoặc từ file JSON:
    python manage.py bulk_create_room_admins --file /path/to/users.json [--update]
"""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from violations.services import (
    ROLE_ROOM_ADMIN,
    apply_user_role,
    ensure_valid_role_room,
    format_role_assignment_success,
)


class Command(BaseCommand):
    help = "Tạo hàng loạt user Quản trị phòng từ JSON inline hoặc file."

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--users",
            type=str,
            metavar="JSON",
            help=(
                'Danh sách user dạng JSON, ví dụ: \'[{"username":"nam","password":"x","room":"P01"}]\''
            ),
        )
        source.add_argument(
            "--file",
            type=str,
            metavar="PATH",
            help="Đường dẫn tới file JSON chứa danh sách user.",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            default=False,
            help="Nếu user đã tồn tại: cập nhật mật khẩu và vai trò thay vì bỏ qua.",
        )

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        raw = options["users"]
        file_path = options["file"]
        allow_update = options["update"]

        # --- Load dữ liệu ---
        if raw:
            try:
                users_data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CommandError(f"JSON không hợp lệ: {exc}") from exc
        else:
            try:
                with open(file_path, encoding="utf-8") as fh:
                    users_data = json.load(fh)
            except FileNotFoundError as exc:
                raise CommandError(f"Không tìm thấy file: {file_path}") from exc
            except json.JSONDecodeError as exc:
                raise CommandError(f"File JSON không hợp lệ: {exc}") from exc

        if not isinstance(users_data, list):
            raise CommandError("Dữ liệu phải là một mảng JSON (list of objects).")

        User = get_user_model()
        created_count = 0
        updated_count = 0
        skipped_count = 0
        error_count = 0

        for idx, entry in enumerate(users_data, start=1):
            username = (entry.get("username") or "").strip()
            password = (entry.get("password") or "").strip()
            room = (entry.get("room") or "").strip()
            first_name = (entry.get("first_name") or "").strip()
            last_name = (entry.get("last_name") or "").strip()

            # Validate bắt buộc
            if not username:
                self.stderr.write(self.style.ERROR(f"  [#{idx}] Thiếu 'username', bỏ qua."))
                error_count += 1
                continue
            if not password:
                self.stderr.write(self.style.ERROR(f"  [#{idx}] '{username}': Thiếu 'password', bỏ qua."))
                error_count += 1
                continue

            try:
                ensure_valid_role_room(ROLE_ROOM_ADMIN, room)
            except ValueError:
                self.stderr.write(
                    self.style.ERROR(f"  [#{idx}] '{username}': Thiếu 'room' (bắt buộc với room_admin), bỏ qua.")
                )
                error_count += 1
                continue

            try:
                with transaction.atomic():
                    user, created = User.objects.get_or_create(
                        username=username,
                        defaults={
                            "first_name": first_name,
                            "last_name": last_name,
                            "is_active": True,
                        },
                    )

                    if created:
                        user.set_password(password)
                        user.save()
                        apply_user_role(user, ROLE_ROOM_ADMIN, room)
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  [TẠO MỚI] {format_role_assignment_success(username, ROLE_ROOM_ADMIN, room)}"
                            )
                        )
                        created_count += 1
                    elif allow_update:
                        user.set_password(password)
                        if first_name:
                            user.first_name = first_name
                        if last_name:
                            user.last_name = last_name
                        user.save()
                        apply_user_role(user, ROLE_ROOM_ADMIN, room)
                        self.stdout.write(
                            self.style.WARNING(
                                f"  [CẬP NHẬT] {format_role_assignment_success(username, ROLE_ROOM_ADMIN, room)}"
                            )
                        )
                        updated_count += 1
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  [BỎ QUA]   '{username}' đã tồn tại. Dùng --update để cập nhật."
                            )
                        )
                        skipped_count += 1

            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(f"  [LỖI] '{username}': {exc}"))
                error_count += 1

        # --- Tóm tắt ---
        self.stdout.write("")
        self.stdout.write("=" * 50)
        self.stdout.write(f"Tổng: {len(users_data)} user")
        self.stdout.write(self.style.SUCCESS(f"  ✔ Tạo mới : {created_count}"))
        if allow_update:
            self.stdout.write(self.style.WARNING(f"  ↻ Cập nhật: {updated_count}"))
        self.stdout.write(self.style.WARNING(f"  – Bỏ qua  : {skipped_count}"))
        if error_count:
            self.stdout.write(self.style.ERROR(f"  ✘ Lỗi     : {error_count}"))
