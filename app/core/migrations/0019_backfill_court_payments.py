from decimal import Decimal

from django.db import migrations


def backfill_payments(apps, schema_editor):
    """Crea movimientos para los turnos ya cobrados antes de existir Caja.

    No se conoce el medio de pago original, así que se usa ``other`` con una
    nota de migración. Los pagos online conservan el ID de Mercado Pago.
    """
    CourtBooking = apps.get_model("core", "CourtBooking")
    CourtPayment = apps.get_model("core", "CourtPayment")

    bookings = CourtBooking.objects.filter(is_paid=True).select_related("court")
    for booking in bookings.iterator():
        if booking.deposit_amount and booking.deposit_amount > Decimal("0"):
            kind = "deposit"
            amount = booking.deposit_amount
        else:
            kind = "full"
            amount = booking.price or Decimal("0")
        paid_at = booking.paid_at or booking.created_at
        if amount and amount > Decimal("0"):
            online = (
                booking.payment_provider == "mercadopago"
                and bool(booking.payment_id)
            )
            CourtPayment.objects.create(
                complex_id=booking.complex_id,
                booking_id=booking.pk,
                kind=kind,
                method="mercadopago" if online else "other",
                amount=amount,
                paid_at=paid_at,
                collected_by_name="Historial",
                note="Migrado del historial anterior a Caja",
                external_id=booking.payment_id if online else "",
                booking_code=booking.code or "",
                customer_name=booking.customer_name or "",
                court_name=booking.court.name if booking.court_id else "",
                booking_date=booking.date,
                booking_start=booking.start_time,
            )

        if booking.deposit_amount and booking.deposit_amount > Decimal("0") and booking.balance_paid:
            balance = max(booking.price - booking.deposit_amount, Decimal("0"))
            if balance > Decimal("0"):
                CourtPayment.objects.create(
                    complex_id=booking.complex_id,
                    booking_id=booking.pk,
                    kind="balance",
                    method="other",
                    amount=balance,
                    paid_at=booking.balance_paid_at or paid_at,
                    collected_by_name="Historial",
                    note="Migrado del historial anterior a Caja",
                    booking_code=booking.code or "",
                    customer_name=booking.customer_name or "",
                    court_name=booking.court.name if booking.court_id else "",
                    booking_date=booking.date,
                    booking_start=booking.start_time,
                )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_cashclose_courtpayment"),
    ]

    operations = [
        migrations.RunPython(backfill_payments, migrations.RunPython.noop),
    ]
