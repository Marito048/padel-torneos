"""Avisos al equipo cuando entra una solicitud de registro.

Todos los canales son opcionales y se configuran por `.env`:

- **Email**: a los administradores generales activos que tengan email y, si
  está definida, a la dirección extra ``SIGNUP_ALERT_EMAIL``.
- **WhatsApp**: vía CallMeBot (``WHATSAPP_ALERT_PHONE`` + ``WHATSAPP_ALERT_APIKEY``).

Si ningún canal está configurado, la solicitud igual queda en Configuración
general y registrada en el log del servicio. Los fallos de envío nunca
interrumpen la página pública: sólo quedan anotados en el log.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode
from urllib.request import urlopen

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


def notify_new_signup_request(request_row, panel_url: str) -> None:
    """Avisa que entró una solicitud de registro (email + WhatsApp)."""
    logger.info(
        "Nueva solicitud de registro: %s (%s)", request_row.email, request_row.complex_name
    )
    lines = [f"Email: {request_row.email}"]
    full_name = f"{request_row.first_name} {request_row.last_name}".strip()
    if full_name:
        lines.append(f"Nombre: {full_name}")
    if request_row.phone:
        lines.append(f"Teléfono: {request_row.phone}")
    if request_row.complex_name:
        lines.append(f"Complejo: {request_row.complex_name}")
    if request_row.complex_city:
        lines.append(f"Ciudad: {request_row.complex_city}")
    if request_row.complex_address:
        lines.append(f"Dirección: {request_row.complex_address}")
    text = "Pádel Server: nueva solicitud de registro.\n" + "\n".join(lines) + (
        f"\nRevisala en Configuración general → Solicitudes de registro: {panel_url}"
    )
    _send_email(request_row.email, text)
    _send_whatsapp(text)


def _alert_recipients() -> list[str]:
    recipients = set()
    configured = getattr(settings, "SIGNUP_ALERT_EMAIL", "")
    if configured:
        recipients.add(configured)
    User = get_user_model()
    recipients.update(
        User.objects.filter(is_superuser=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    return sorted(recipients)


def _send_email(email: str, text: str) -> None:
    recipients = _alert_recipients()
    if not recipients:
        return
    try:
        send_mail(
            f"[Pádel Server] Nueva solicitud de registro: {email}",
            text,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except Exception:
        logger.warning("No se pudo enviar el aviso por email", exc_info=True)


def _send_whatsapp(text: str) -> None:
    phone = getattr(settings, "WHATSAPP_ALERT_PHONE", "")
    apikey = getattr(settings, "WHATSAPP_ALERT_APIKEY", "")
    if not (phone and apikey):
        return
    params = urlencode({"phone": phone, "text": text, "apikey": apikey})
    try:
        with urlopen(f"{CALLMEBOT_URL}?{params}", timeout=5) as response:
            response.read()
    except Exception:
        logger.warning("No se pudo enviar el aviso por WhatsApp", exc_info=True)
