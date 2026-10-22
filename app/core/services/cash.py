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


def _net_paid(booking) -> Decimal:
    """Neto del libro para un turno (puede ser negativo)."""
    total = sum(
        (payment.signed_amount for payment in CourtPayment.objects.filter(booking=booking)),
        Decimal("0.00"),
    )
    return _money(total)


def _publish_state(source, target) -> None:
    """Copia el estado derivado del turno bloqueado al objeto que recibió el caller."""
    if target is None or source is target:
        return
    for field in (
        "is_paid",
        "balance_paid",
        "paid_at",
        "balance_paid_at",
        "payment_status",
    ):
        setattr(target, field, getattr(source, field))
    _invalidate_ledger_cache(target)


def _invalidate_ledger_cache(booking) -> None:
    booking.__dict__.pop("_ledger_summary_cache", None)
    prefetched = booking.__dict__.get("_prefetched_objects_cache")
    if prefetched:
        prefetched.pop("court_payments", None)


def ensure_ledger_opening(booking) -> list[CourtPayment]:
    """Crea el asiento de apertura de un turno legado (sin movimientos).

    Los turnos anteriores a Caja se reconocían sólo por banderas. Al primer
    movimiento se materializa lo ya cobrado para que el neto no lo cuente dos
    veces. Es idempotente: si ya hay cualquier movimiento, no hace nada.
    """
    _invalidate_ledger_cache(booking)
    if not booking.pk or CourtPayment.objects.filter(booking=booking).exists():
        return []
    price = _money(booking.price)
    if price <= 0:
        return []
    created: list[CourtPayment] = []
    snapshot = _snapshot(booking)
    common = {
        "complex": booking.complex,
        "booking": booking,
        "method": "other",
        "collected_by_name": "Historial",
        "note": "Saldo inicial migrado desde el estado del turno",
        **snapshot,
    }
    deposit = _money(booking.deposit_amount)
    if deposit > 0 and booking.is_paid:
        created.append(
            CourtPayment.objects.create(
                kind="deposit",
                amount=min(deposit, price),
                paid_at=booking.paid_at or booking.created_at,
                **common,
            )
        )
    elif deposit <= 0 and booking.is_paid:
        created.append(
            CourtPayment.objects.create(
                kind="full",
                amount=price,
                paid_at=booking.paid_at or booking.created_at,
                **common,
            )
        )
    if deposit > 0 and booking.balance_paid:
        balance = max(price - deposit, Decimal("0.00"))
        if balance > 0:
            created.append(
                CourtPayment.objects.create(
                    kind="balance",
                    amount=balance,
                    paid_at=booking.balance_paid_at or booking.created_at,
                    **common,
                )
            )
    if created:
        _invalidate_ledger_cache(booking)
        sync_booking_payment_state(booking)
    return created


def sync_booking_payment_state(booking) -> None:
    """Deriva ``is_paid``/``balance_paid`` del libro y los persiste.

    Se llama dentro de la misma transacción que registra el movimiento. Si el
    turno no tiene movimientos, no toca las banderas legadas.
    """
    _invalidate_ledger_cache(booking)
    payments = list(CourtPayment.objects.filter(booking=booking))
    if not payments:
        return
    net = sum((payment.signed_amount for payment in payments), Decimal("0.00"))
    income = [payment for payment in payments if not payment.is_outgoing]
    price = _money(booking.price)
    deposit = min(max(_money(booking.deposit_amount), Decimal("0.00")), price) if price > 0 else Decimal("0.00")
    if price <= 0:
        is_paid = True
        balance_paid = True
    else:
        deposit_target = deposit if deposit > 0 else price
        is_paid = net >= deposit_target
        balance_paid = net >= price
    paid_at = min((payment.paid_at for payment in income), default=None) if is_paid else None
    balance_paid_at = max((payment.paid_at for payment in income), default=None) if balance_paid else None
    payment_status = booking.payment_status
    has_refund = any(payment.kind in ("refund", "chargeback") for payment in payments)
    if has_refund and net <= 0 and payment_status not in (
        "approved_conflict",
        "amount_mismatch",
        "charged_back",
    ):
        payment_status = "refunded"
    CourtBooking.objects.filter(pk=booking.pk).update(
        is_paid=is_paid,
        balance_paid=balance_paid,
        paid_at=paid_at,
        balance_paid_at=balance_paid_at,
        payment_status=payment_status,
    )
    booking.is_paid = is_paid
    booking.balance_paid = balance_paid
    booking.paid_at = paid_at
    booking.balance_paid_at = balance_paid_at
    booking.payment_status = payment_status
    _invalidate_ledger_cache(booking)


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
    """Registra un movimiento del libro con lock del turno y sincroniza banderas.

    ``amount`` siempre es positivo; el signo contable se deduce de ``kind``.
    ``external_id`` identifica un evento externo y evita duplicados. El lock
    del turno serializa cobros y devoluciones concurrentes del mismo turno.
    """
    amount = _money(amount)
    if amount <= 0:
        return None
    if kind not in dict(CourtPayment.KIND):
        raise ValueError(f"Tipo de movimiento inválido: {kind}")
    if method not in PAYMENT_METHODS:
        raise ValueError(f"Medio de pago inválido: {method}")

    original_booking = booking
    external_id = str(external_id or "").strip()
    with transaction.atomic():
        booking = CourtBooking.objects.select_for_update().get(pk=booking.pk)
        ensure_ledger_opening(booking)
        if external_id:
            existing = CourtPayment.objects.filter(external_id=external_id).first()
            if existing is not None:
                if existing.booking_id not in (None, booking.pk):
                    logger.warning(
                        "El movimiento externo %s ya pertenece a otro turno", external_id
                    )
                    return None
                sync_booking_payment_state(booking)
                _publish_state(booking, original_booking)
                return existing

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
            "external_id": external_id,
            **_snapshot(booking),
        }
        try:
            payment = CourtPayment.objects.create(**values)
        except IntegrityError:
            if external_id:
                existing = CourtPayment.objects.filter(external_id=external_id).first()
                if existing is not None and existing.booking_id == booking.pk:
                    sync_booking_payment_state(booking)
                    _publish_state(booking, original_booking)
                    return existing
            raise
        sync_booking_payment_state(booking)
        _publish_state(booking, original_booking)
        return payment


def record_reversal(
    booking,
    kind: str | None = None,
    user=None,
    note: str = "",
    amount=None,
) -> CourtPayment | None:
    """Revierte el último ingreso activo, sin devolver más que el neto cobrado.

    No se modifica el movimiento original: se agrega un egreso vinculado para
    conservar la trazabilidad. Si una devolución previa ya redujo el neto, la
    reversión se limita a lo que queda; nunca deja el neto en negativo.
    """
    with transaction.atomic():
        locked = CourtBooking.objects.select_for_update().get(pk=booking.pk)
        ensure_ledger_opening(locked)
        queryset = CourtPayment.objects.filter(
            booking=locked,
            kind__in=("deposit", "balance", "full"),
            reversed_by__isnull=True,
        )
        if kind:
            queryset = queryset.filter(kind=kind)
        original = queryset.order_by("-paid_at", "-id").first()
        if original is None:
            logger.warning(
                "No se pudo revertir %s del turno %s: no hay movimiento activo",
                kind or "cobro",
                locked.code,
            )
            return None
        available = max(_net_paid(locked), Decimal("0.00"))
        if available <= 0:
            return None
        requested = _money(amount) if amount is not None else original.amount
        reverse_amount = min(original.amount, available, requested)
        if reverse_amount <= 0:
            return None
        try:
            payment = record_payment(
                locked,
                kind="reversal",
                method=original.method,
                amount=reverse_amount,
                user=user,
                note=note or f"Reversión de {original.get_kind_display().lower()}",
                reverses=original,
            )
        except IntegrityError:
            # Otra petición ya revirtió este movimiento.
            return None
        _publish_state(locked, booking)
        return payment


def record_online_deposit(
    booking,
    payment_id: str = "",
    *,
    amount=None,
    note: str = "",
) -> CourtPayment | None:
    """Registra un pago acreditado por Mercado Pago.

    Cada ``payment_id`` distinto es un ingreso distinto: si el cliente pagó dos
    veces, la caja lo refleja y ``overpaid_amount`` lo muestra. Un cobro manual
    previo sin ID se concilia sólo si hay un único ingreso activo y el importe
    coincide; así el webhook no duplica ni se come un pago distinto.
    """
    payment_id = str(payment_id or "").strip()
    with transaction.atomic():
        locked = CourtBooking.objects.select_for_update().get(pk=booking.pk)
        ensure_ledger_opening(locked)
        if payment_id:
            existing = CourtPayment.objects.filter(external_id=payment_id).first()
            if existing is not None:
                if existing.booking_id not in (None, locked.pk):
                    logger.warning("El pago %s ya pertenece a otro turno", payment_id)
                    return None
                sync_booking_payment_state(locked)
                _publish_state(locked, booking)
                return existing

        expected = _money(amount) if amount is not None else _money(
            locked.deposit_amount if _money(locked.deposit_amount) > 0 else locked.price
        )
        if expected <= 0:
            return None

        active_manual = list(
            CourtPayment.objects.filter(
                booking=locked,
                kind__in=("deposit", "balance", "full"),
                reversed_by__isnull=True,
                external_id="",
            )
        )
        if (
            payment_id
            and len(active_manual) == 1
            and _money(active_manual[0].amount) == expected
        ):
            try:
                active_manual[0].external_id = payment_id
                active_manual[0].save(update_fields=["external_id"])
                sync_booking_payment_state(locked)
                _publish_state(locked, booking)
                return active_manual[0]
            except IntegrityError:
                pass

        net = _net_paid(locked)
        price = _money(locked.price)
        deposit = _money(locked.deposit_amount)
        if deposit > 0 and net < deposit:
            kind = "deposit"
        elif net < price:
            kind = "balance"
        else:
            kind = "full"
        payment = record_payment(
            locked,
            kind=kind,
            method="mercadopago",
            amount=expected,
            note=note or "Pago online con Mercado Pago",
            external_id=payment_id,
            collected_by_name="Mercado Pago",
        )
        _publish_state(locked, booking)
        return payment


def record_online_refund(
    booking,
    payment_id: str = "",
    *,
    kind: str = "refund",
    note: str = "",
    amount=None,
    refund_id: str = "",
) -> CourtPayment | None:
    """Registra una devolución o contracargo online por el delta informado.

    ``amount`` es el total acumulado devuelto para ese pago (lo que informa
    Mercado Pago en ``transaction_amount_refunded`` o la suma de ``refunds``).
    Si hay devoluciones parciales sucesivas, se registra sólo la diferencia.
    Una devolución que llega antes del ingreso se registra igual: el neto queda
    negativo y se regulariza cuando llega el pago.
    """
    if kind not in ("refund", "chargeback"):
        raise ValueError("El tipo de egreso online debe ser refund o chargeback")
    payment_id = str(payment_id or "").strip()
    prefix = f"{kind}:{payment_id}" if payment_id else ""
    with transaction.atomic():
        locked = CourtBooking.objects.select_for_update().get(pk=booking.pk)
        ensure_ledger_opening(locked)
        recorded = list(
            CourtPayment.objects.filter(
                booking=locked,
                kind=kind,
                external_id__startswith=prefix,
            )
        ) if prefix else []
        already = sum((payment.amount for payment in recorded), Decimal("0.00"))

        original = (
            CourtPayment.objects.filter(
                booking=locked,
                kind__in=("deposit", "balance", "full"),
            )
            .order_by("-paid_at", "-id")
            .first()
        )
        if payment_id:
            # La devolución pertenece a un pago concreto: no se limita con el
            # importe de otro cobro posterior del mismo turno.
            original = (
                CourtPayment.objects.filter(
                    booking=locked,
                    kind__in=("deposit", "balance", "full"),
                    external_id=payment_id,
                )
                .order_by("-paid_at", "-id")
                .first()
            )
        if amount is None:
            if original is None:
                logger.warning(
                    "No se pudo registrar %s del pago %s: falta el ingreso original",
                    kind,
                    payment_id,
                )
                return None
            target = original.amount
        else:
            target = _money(amount)
        if original is not None:
            # Una reversión contable del mismo ingreso ya redujo el neto. No
            # registrar además la devolución completa: se limita el techo al
            # importe del pago que todavía no fue revertido, para no contar dos
            # veces la misma salida de dinero.
            reversed_amount = sum(
                (
                    payment.amount
                    for payment in CourtPayment.objects.filter(reverses=original)
                ),
                Decimal("0.00"),
            )
            ceiling = max(original.amount - reversed_amount, Decimal("0.00"))
            # Capacidad neta del turno: no dejar el libro en negativo por una
            # devolución online posterior a otras salidas (manuales o
            # reversiones). ``already`` son las devoluciones online de este
            # mismo pago que el neto ya descuenta.
            available = max(_net_paid(locked), Decimal("0.00")) + already
            target = min(target, ceiling, available)
        delta = target - already
        if delta <= 0:
            return recorded[-1] if recorded else None

        if prefix:
            if not recorded:
                external_id = prefix
            elif refund_id:
                external_id = f"{prefix}:{refund_id}:{target:.2f}"
            else:
                external_id = f"{prefix}:{target:.2f}"
        else:
            external_id = f"{kind}:{refund_id}" if refund_id else ""

        label = "Devolución" if kind == "refund" else "Contracargo"
        payment = record_payment(
            locked,
            kind=kind,
            method="mercadopago",
            amount=delta,
            note=note or f"{label} informado por Mercado Pago",
            external_id=external_id,
            collected_by_name="Mercado Pago",
        )
        _publish_state(locked, booking)
        return payment


def refundable_amount(booking) -> Decimal:
    """Dinero que todavía puede devolverse de un turno según el libro."""
    return max(_money(booking.net_paid_amount), Decimal("0.00"))


def record_manual_refund(
    booking,
    amount,
    method: str,
    user,
    note: str = "",
    idempotency_key: str = "",
) -> CourtPayment:
    """Registra una devolución hecha fuera de Mercado Pago.

    ``idempotency_key`` es el token del formulario: si se reenvía el mismo
    formulario, se devuelve el movimiento ya creado en vez de duplicarlo.
    """
    amount = _money(amount)
    if method not in PAYMENT_METHODS:
        raise ValidationError("Elegí un medio de pago válido.")
    if amount <= 0:
        raise ValidationError("El importe de la devolución debe ser mayor a cero.")
    key = str(idempotency_key or "").strip()
    external_id = f"manual-refund:{key}" if key else ""
    with transaction.atomic():
        # Serializa dos devoluciones simultáneas del mismo turno para que
        # ambas no puedan superar el dinero cobrado.
        locked = CourtBooking.objects.select_for_update().get(pk=booking.pk)
        if external_id:
            existing = CourtPayment.objects.filter(external_id=external_id).first()
            if existing is not None:
                if existing.booking_id == locked.pk:
                    sync_booking_payment_state(locked)
                    _publish_state(locked, booking)
                    return existing
                raise ValidationError("La clave de idempotencia pertenece a otro turno.")
        ensure_ledger_opening(locked)
        available = refundable_amount(locked)
        if amount > available:
            raise ValidationError(
                f"La devolución no puede superar el saldo cobrado del turno (${available:.2f})."
            )
        payment = record_payment(
            locked,
            kind="refund",
            method=method,
            amount=amount,
            user=user,
            note=note or "Devolución registrada desde caja",
            external_id=external_id,
        )
        _publish_state(locked, booking)
        return payment


def audit_booking(booking, payments=None) -> list[str]:
    """Detecta incoherencias sin escribir ni reparar datos.

    La auditoría es deliberadamente conservadora: una bandera legada
    contradictoria se informa, no se interpreta como un cobro. La apertura del
    libro sólo debe crearse cuando exista evidencia suficiente (un movimiento
    nuevo o una revisión manual).
    """
    issues: list[str] = []
    price = _money(booking.price)
    deposit = min(max(_money(booking.deposit_amount), Decimal("0.00")), price) if price > 0 else Decimal("0.00")
    if _money(booking.deposit_amount) > price and price > 0:
        issues.append("La seña supera el precio del turno.")
    if booking.balance_paid and not booking.is_paid:
        issues.append("balance_paid=True con is_paid=False; no se puede inferir la apertura.")
    if booking.is_paid and booking.paid_at is None:
        issues.append("is_paid=True sin paid_at.")
    if booking.balance_paid and booking.balance_paid_at is None:
        issues.append("balance_paid=True sin balance_paid_at.")
    if booking.payment_status == "approved" and not booking.is_paid:
        issues.append("payment_status=approved con is_paid=False.")
    if booking.payment_id and booking.payment_status == "approved" and not booking.is_paid:
        issues.append("Hay un pago online aprobado pero el turno no está abonado.")

    if payments is None:
        payments = list(CourtPayment.objects.filter(booking=booking))
    if booking.payment_status == "charged_back" and not any(
        payment.kind == "chargeback" for payment in payments
    ):
        issues.append(
            "payment_status=charged_back sin movimiento de contracargo; "
            "revisar el importe y el resultado antes de conciliar."
        )
    if not payments:
        return issues

    income = sum(
        (payment.signed_amount for payment in payments if not payment.is_outgoing),
        Decimal("0.00"),
    )
    outgoing = sum(
        (payment.amount for payment in payments if payment.is_outgoing),
        Decimal("0.00"),
    )
    net = _money(income - outgoing)
    if net < 0:
        issues.append("El neto del libro es negativo (devoluciones sin ingreso).")
    if price > 0:
        deposit_target = deposit if deposit > 0 else price
        expected_is_paid = net >= deposit_target
        expected_balance_paid = net >= price
        if bool(booking.is_paid) != expected_is_paid:
            issues.append(
                f"is_paid={booking.is_paid} no coincide con el libro "
                f"(neto {net}, seña {deposit_target})."
            )
        if bool(booking.balance_paid) != expected_balance_paid:
            issues.append(
                f"balance_paid={booking.balance_paid} no coincide con el libro "
                f"(neto {net}, precio {price})."
            )
    if outgoing > income and net < 0:
        issues.append("Las devoluciones superan los ingresos registrados.")
    return issues


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
    if day > timezone.localdate():
        raise ValidationError("No se puede cerrar la caja de una fecha futura.")
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
