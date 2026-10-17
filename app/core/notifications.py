"""Avisos al equipo cuando entra una solicitud de registro o una autoinscripción.

Todos los canales son opcionales y se configuran por `.env`:

- **Email**: a los administradores generales activos que tengan email y, si
  está definida, a la dirección extra ``SIGNUP_ALERT_EMAIL``. Para las
  autoinscripciones también se avisa a los usuarios del complejo del torneo.
- **WhatsApp**: vía CallMeBot (``WHATSAPP_ALERT_PHONE`` + ``WHATSAPP_ALERT_APIKEY``).

Si ningún canal está configurado, la solicitud igual queda en Configuración
general (o en el panel de parejas) y registrada en el log del servicio. Los
fallos de envío nunca interrumpen la página pública: sólo quedan anotados en
el log.
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
    _send_email(f"[Pádel Server] Nueva solicitud de registro: {request_row.email}", text)
    _send_whatsapp(text)


def notify_tournament_team_signup(tournament, team, phones: list[str], panel_url: str) -> None:
    """Avisa que una pareja se autoinscribió (queda pendiente de confirmar)."""
    logger.info("Autoinscripción: %s en %s", team, tournament.name)
    lines = []
    if tournament.complex_id:
        lines.append(f"Complejo: {tournament.complex.name}")
    lines.append(f"Torneo: {tournament.name}")
    lines.append(f"Dupla: {team}")
    for phone in phones:
        if phone:
            lines.append(f"Teléfono: {phone}")
    text = (
        "Pádel Server: nueva pareja anotada por autoinscripción (pendiente de confirmación).\n"
        + "\n".join(lines)
        + f"\nConfirmala desde el panel de parejas: {panel_url}"
    )
    extra = []
    if tournament.complex_id:
        extra = list(
            tournament.complex.users.filter(is_active=True)
            .exclude(email="")
            .values_list("email", flat=True)
        )
    _send_email(f"[Pádel Server] Nueva pareja anotada: {tournament.name}", text, extra)
    _send_whatsapp(text)


def _alert_recipients(extra: list[str] | None = None) -> list[str]:
    recipients = set(extra or [])
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


def _send_email(subject: str, text: str, extra_recipients: list[str] | None = None) -> None:
    recipients = _alert_recipients(extra_recipients)
    if not recipients:
        return
    try:
        send_mail(
            subject,
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
