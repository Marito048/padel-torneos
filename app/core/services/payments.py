"""Integración mínima con Mercado Pago Checkout Pro.

Sólo se usan las dos llamadas necesarias para el turnero:

- crear una preferencia de pago por la seña;
- consultar un pago para confirmar que fue aprobado.

Las credenciales nunca salen de este módulo ni se escriben en los logs. Los
tests reemplazan estas funciones por mocks, así que no hace falta red.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)


class MercadoPagoError(Exception):
    """Error controlado al hablar con Mercado Pago."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def deposit_amount_for(price) -> Decimal:
    """Calcula la seña (30% por defecto) redondeada a centavos."""
    amount = Decimal(price or 0)
    if amount <= 0:
        return Decimal("0.00")
    percent = Decimal(str(getattr(settings, "BOOKING_DEPOSIT_PERCENT", 30)))
    percent = min(max(percent, Decimal("0")), Decimal("100"))
    return (amount * percent / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _request_json(url: str, access_token: str, *, payload=None, timeout=None):
    data = None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=timeout or settings.MERCADO_PAGO_TIMEOUT) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as exc:
        # No se registra el cuerpo de la respuesta: puede contener datos del
        # pagador. El código HTTP alcanza para diagnosticar y reintentar.
        exc.read()
        logger.warning("Mercado Pago respondió %s", exc.code)
        raise MercadoPagoError(
            f"Mercado Pago respondió {exc.code}", status_code=exc.code
        ) from exc
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("No se pudo conectar con Mercado Pago: %s", exc)
        raise MercadoPagoError("No se pudo conectar con Mercado Pago") from exc


def create_checkout_preference(access_token: str, payload: dict) -> dict:
    """Crea una preferencia de Checkout Pro y devuelve la respuesta de MP."""
    if not access_token:
        raise MercadoPagoError("El complejo no tiene configurado Mercado Pago.")
    url = f"{settings.MERCADO_PAGO_API_BASE.rstrip('/')}/checkout/preferences"
    return _request_json(url, access_token, payload=payload)


def fetch_payment(access_token: str, payment_id: str) -> dict:
    """Consulta un pago por ID usando el token del complejo."""
    if not access_token:
        raise MercadoPagoError("El complejo no tiene configurado Mercado Pago.")
    if not str(payment_id or "").strip():
        raise MercadoPagoError("Falta el ID de pago.")
    url = f"{settings.MERCADO_PAGO_API_BASE.rstrip('/')}/v1/payments/{quote(str(payment_id), safe='')}"
    return _request_json(url, access_token)


def fetch_merchant_order(access_token: str, order_id: str) -> dict:
    """Consulta una orden comercial (notificación ``merchant_order``)."""
    if not access_token:
        raise MercadoPagoError("El complejo no tiene configurado Mercado Pago.")
    if not str(order_id or "").strip():
        raise MercadoPagoError("Falta el ID de la orden.")
    url = (
        f"{settings.MERCADO_PAGO_API_BASE.rstrip('/')}/merchant_orders/"
        f"{quote(str(order_id), safe='')}"
    )
    return _request_json(url, access_token)


def fetch_chargeback(access_token: str, chargeback_id: str) -> dict:
    """Consulta un contracargo por ID para asociarlo a su pago."""
    if not access_token:
        raise MercadoPagoError("El complejo no tiene configurado Mercado Pago.")
    if not str(chargeback_id or "").strip():
        raise MercadoPagoError("Falta el ID del contracargo.")
    url = (
        f"{settings.MERCADO_PAGO_API_BASE.rstrip('/')}/v1/chargebacks/"
        f"{quote(str(chargeback_id), safe='')}"
    )
    return _request_json(url, access_token)
