"""Crea (o repara) el administrador general a partir de variables de entorno.

Se usa en el arranque del servicio en Railway, donde no hay consola para
correr ``createsuperuser`` a mano:

    python manage.py ensure_superuser

Lee:

* ``DJANGO_SUPERUSER_USERNAME``  (obligatorio para hacer algo)
* ``DJANGO_SUPERUSER_PASSWORD``  (obligatorio para hacer algo)
* ``DJANGO_SUPERUSER_EMAIL``     (opcional)
* ``DJANGO_SUPERUSER_RESET_PASSWORD=1`` fuerza a reescribir la contraseña de
  un usuario que ya existe. Sin esto, una redeploy NO pisa la contraseña que
  el administrador haya cambiado desde el panel.
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Crea o repara el superusuario definido en las variables de entorno."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Resetea la contraseña aunque el usuario ya exista.",
        )

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()
        force = options.get("force") or os.environ.get(
            "DJANGO_SUPERUSER_RESET_PASSWORD", ""
        ).strip().lower() in {"1", "true", "yes", "si", "sí"}

        if not username or not password:
            self.stdout.write(
                self.style.WARNING(
                    "ensure_superuser: sin DJANGO_SUPERUSER_USERNAME/PASSWORD no se hace nada."
                )
            )
            return

        try:
            validate_password(password)
        except ValidationError as exc:
            self.stderr.write(
                self.style.WARNING(
                    "ensure_superuser: la contraseña de '%s' no cumple la validación "
                    "del proyecto (%s). Se usa igual." % (username, "; ".join(exc.messages))
                )
            )

        User = get_user_model()

        with transaction.atomic():
            user = User.objects.filter(username=username).first()
            created = user is None

            if created:
                user = User(username=username, email=email)
            else:
                # El usuario existe: se mantienen sus datos y sólo se garantiza
                # que pueda administrar el sitio.
                user.is_staff = True
                user.is_superuser = True
                user.is_active = True
                if email:
                    user.email = email
                if force:
                    user.set_password(password)
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        "ensure_superuser: '%s' ya existía (administrador general)." % username
                    )
                )
                return

            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            if email:
                user.email = email
            user.set_password(password)
            user.save()

        self.stdout.write(
            self.style.SUCCESS(
                "ensure_superuser: administrador general '%s' creado." % username
            )
        )
