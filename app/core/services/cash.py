"""Libro de caja de los turnos.

La agenda guarda el estado operativo del turno (si la seña o el saldo están
abonados); este módulo guarda cada movimiento de dinero en un libro separado.
La separación permite responder quién cobró, cuándo, con qué medio y cuánto se
devolvió, sin perder el historial cuando se corrige una marca desde el panel.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import CashClose, CourtBooking, CourtPayment

logger = logging.getLogger(__name__)

MONEY = Decimal("0.01")
MANUAL_METHODS = ("cash", "transfer", "card", "other")
PAYMENT_METHODS = {value for value, _label in CourtPayment.METHOD}


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _user_name(user) -> str:
    if not user:
        return ""
    return (user.get_full_name() or "").strip() or user.get_username()


def _snapshot(booking) -> dict:
    """Datos del turno que deben sobrevivir a cambios o bajas posteriores."""
    return {
        "booking_code": booking.code or "",
        "customer_name": booking.customer_name or "",
        "court_name": booking.court.name if booking.court_id else "",
        "booking_date": booking.date,
        "booking_start": booking.start_time,
    }


def record_payment(
    booking,
    *,
    kind: str,
    method: str,
    amount,
    user=None,
    paid_at=None,
    note: str = "",
    external_id: str = "",
    reverses=None,
    collected_by_name: str = "",
) -> CourtPayment | None:
    """Registra un movimiento positivo o negativo del libro de caja.

    ``amount`` siempre es positivo; el signo contable se deduce de ``kind``.
    ``external_id`` permite que un webhook repetido de Mercado Pago no duplique
    el movimiento.
    """
    amount = _money(amount)
    if amount <= 0:
        return None
    if kind not in dict(CourtPayment.KIND):
        raise ValueError(f"Tipo de movimiento inválido: {kind}")
    if method not in PAYMENT_METHODS:
        raise ValueError(f"Medio de pago inválido: {method}")

    external_id = str(external_id or "").strip()
    values = {
        "complex": booking.complex,
        "booking": booking,
        "kind": kind,
        "method": method,
        "amount": amount,
        "paid_at": paid_at or timezone.now(),
        "collected_by": user,
        "collected_by_name": collected_by_name or _user_name(user) or "Sistema",
        "note": (note or "")[:200],
        "reverses": reverses,
        **_snapshot(booking),
    }
    if external_id:
        payment, created = CourtPayment.objects.get_or_create(
            external_id=external_id,
            defaults=values,
        )
        if not created and payment.booking_id not in (None, booking.pk):
            logger.warning(
                "El movimiento externo %s ya pertenece a otro turno", external_id
            )
            return None
        return payment
    return CourtPayment.objects.create(**values)


def record_reversal(booking, kind: str, user=None, note: str = "") -> CourtPayment | None:
    """Revierte el último movimiento activo de un tipo (seña, saldo o total).

    No se modifica el movimiento original: se agrega un egreso vinculado para
    conservar la trazabilidad. Si no hay un movimiento activo, no inventa uno.
    """
    original = (
        CourtPayment.objects.filter(
            booking=booking,
            kind=kind,
            reversed_by__isnull=True,
        )
        .order_by("-paid_at", "-id")
        .first()
    )
    if original is None:
        logger.warning(
            "No se pudo revertir %s del turno %s: no hay movimiento activo",
            kind,
            booking.code,
        )
        return None
    try:
        return record_payment(
            booking,
            kind="reversal",
            method=original.method,
            amount=original.amount,
            user=user,
            note=note or f"Reversión de {original.get_kind_display().lower()}",
            reverses=original,
        )
    except IntegrityError:
        # Otra petición ya revirtió este movimiento.
        return None


def record_online_deposit(
    booking,
    payment_id: str = "",
    *,
    amount=None,
    note: str = "",
) -> CourtPayment | None:
    """Registra la seña acreditada por Mercado Pago.

    El webhook y la vuelta del checkout pueden llegar varias veces. Se reutiliza
    el movimiento activo o el ``external_id`` del pago para no duplicar caja.
    """
    payment_id = str(payment_id or "").strip()
    if payment_id:
        existing = CourtPayment.objects.filter(external_id=payment_id).first()
        if existing is not None:
            return existing

    active = (
        CourtPayment.objects.filter(
            booking=booking,
            kind__in=("deposit", "full"),
            reversed_by__isnull=True,
        )
        .order_by("-paid_at", "-id")
        .first()
    )
    if active is not None:
        # Un cobro manual previo puede no tener ID externo. Completarlo ayuda a
        # conciliar el webhook sin crear un segundo ingreso.
        if payment_id and not active.external_id:
            try:
                active.external_id = payment_id
                active.save(update_fields=["external_id"])
            except IntegrityError:
                pass
        return active

    kind = "deposit" if _money(booking.deposit_amount) > 0 else "full"
    expected = booking.deposit_amount if kind == "deposit" else booking.price
    return record_payment(
        booking,
        kind=kind,
        method="mercadopago",
        amount=amount if amount is not None else expected,
        note=note or "Pago online con Mercado Pago",
        external_id=payment_id,
        collected_by_name="Mercado Pago",
    )


def record_online_refund(
    booking,
    payment_id: str = "",
    *,
    kind: str = "refund",
    note: str = "",
) -> CourtPayment | None:
    """Registra una devolución o contracargo informado por Mercado Pago.

    El importe se toma del ingreso original. Si no existe un ingreso en el
    libro (por ejemplo, un dato anterior a la migración de caja), no se inventa
    un egreso: se devuelve ``None`` para no descuadrar los totales.
    """
    if kind not in ("refund", "chargeback"):
        raise ValueError("El tipo de egreso online debe ser refund o chargeback")
    payment_id = str(payment_id or "").strip()
    external_id = f"{kind}:{payment_id}" if payment_id else ""
    if external_id:
        existing = CourtPayment.objects.filter(external_id=external_id).first()
        if existing is not None:
            return existing

    original = (
        CourtPayment.objects.filter(
            booking=booking,
            kind__in=("deposit", "full"),
        )
        .order_by("-paid_at", "-id")
        .first()
    )
    if original is None:
        logger.warning(
            "No se pudo registrar %s del pago %s: falta el ingreso original",
            kind,
            payment_id,
        )
        return None
    if not external_id and CourtPayment.objects.filter(
        booking=booking, kind=kind, external_id=""
    ).exists():
        return None
    label = "Devolución" if kind == "refund" else "Contracargo"
    return record_payment(
        booking,
        kind=kind,
        method="mercadopago",
        amount=original.amount,
        note=note or f"{label} informado por Mercado Pago",
        external_id=external_id,
        collected_by_name="Mercado Pago",
    )


def refundable_amount(booking) -> Decimal:
    """Dinero que todavía puede devolverse de un turno según el libro."""
    total = sum(
        (payment.signed_amount for payment in CourtPayment.objects.filter(booking=booking)),
        Decimal("0.00"),
    )
    return max(_money(total), Decimal("0.00"))


def record_manual_refund(booking, amount, method: str, user, note: str = "") -> CourtPayment:
    """Registra una devolución hecha fuera de Mercado Pago."""
    amount = _money(amount)
    if method not in PAYMENT_METHODS:
        raise ValidationError("Elegí un medio de pago válido.")
    if amount <= 0:
        raise ValidationError("El importe de la devolución debe ser mayor a cero.")
    with transaction.atomic():
        # Serializa dos devoluciones simultáneas del mismo turno para que
        # ambas no puedan superar el dinero cobrado.
        CourtBooking.objects.select_for_update().filter(pk=booking.pk).exists()
        available = refundable_amount(booking)
        if amount > available:
            raise ValidationError(
                f"La devolución no puede superar el saldo cobrado del turno (${available:.2f})."
            )
        return record_payment(
            booking,
            kind="refund",
            method=method,
            amount=amount,
            user=user,
            note=note or "Devolución registrada desde caja",
        )


def payments_for_range(complex_obj, start, end, method: str = ""):
    """Movimientos del complejo en un rango de fechas de caja (inclusive)."""
    queryset = CourtPayment.objects.filter(
        complex=complex_obj,
        paid_at__date__gte=start,
        paid_at__date__lte=end,
    )
    if method in PAYMENT_METHODS:
        queryset = queryset.filter(method=method)
    return queryset.select_related("booking", "collected_by", "reverses").order_by("paid_at", "id")


def payments_for_day(complex_obj, day):
    return payments_for_range(complex_obj, day, day)


def summarize(payments) -> dict:
    """Totales de una lista o queryset de movimientos, separados por medio."""
    payments = list(payments)
    by_method = {
        method: {
            "income": Decimal("0.00"),
            "outgoing": Decimal("0.00"),
            "net": Decimal("0.00"),
        }
        for method, _label in CourtPayment.METHOD
    }
    total_income = Decimal("0.00")
    total_outgoing = Decimal("0.00")
    for payment in payments:
        bucket = by_method[payment.method]
        if payment.is_outgoing:
            bucket["outgoing"] += payment.amount
            total_outgoing += payment.amount
        else:
            bucket["income"] += payment.amount
            total_income += payment.amount
    for bucket in by_method.values():
        bucket["net"] = bucket["income"] - bucket["outgoing"]
    return {
        "payments": payments,
        "count": len(payments),
        "total_income": total_income,
        "total_outgoing": total_outgoing,
        "net_total": total_income - total_outgoing,
        "by_method": by_method,
    }


def summary_for_range(complex_obj, start, end, method: str = "") -> dict:
    return summarize(payments_for_range(complex_obj, start, end, method=method))


def summary_for_day(complex_obj, day) -> dict:
    return summarize(payments_for_day(complex_obj, day))


def close_day(complex_obj, day, user, note: str = "") -> CashClose:
    """Guarda una foto de los totales del día, sin bloquear movimientos futuros."""
    if CashClose.objects.filter(complex=complex_obj, date=day).exists():
        raise ValidationError("La caja de ese día ya está cerrada.")
    summary = summary_for_day(complex_obj, day)
    breakdown = {
        method: {
            "income": str(values["income"]),
            "outgoing": str(values["outgoing"]),
            "net": str(values["net"]),
        }
        for method, values in summary["by_method"].items()
    }
    try:
        with transaction.atomic():
            return CashClose.objects.create(
                complex=complex_obj,
                date=day,
                closed_by=user,
                closed_by_name=_user_name(user) or "Sistema",
                total_income=summary["total_income"],
                total_outgoing=summary["total_outgoing"],
                net_total=summary["net_total"],
                payment_breakdown=breakdown,
                note=(note or "")[:200],
            )
    except IntegrityError as exc:
        raise ValidationError("La caja de ese día ya está cerrada.") from exc
