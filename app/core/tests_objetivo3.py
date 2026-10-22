"""Regresión del Objetivo 3: cobros, devoluciones y consistencia de caja."""

from datetime import time, timedelta
from decimal import Decimal
from io import StringIO
import json
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import CourtBooking, CourtPayment
from core.services import cash as cash_service
from core.services import schedule as schedule_service
from core.tests_turnos import TurnosFixtureMixin
from core.views import _apply_mp_payment

User = get_user_model()


class PaymentConsistencyTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex(booking_price=Decimal("8000"))
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))
        self.admin = User.objects.create_superuser("admin-pagos", password="clave-segura-123")
        self.booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(9, 0),
            customer_name="Ana Pagos",
            customer_phone="2215551234",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )

    def refresh(self, booking=None):
        booking = booking or self.booking
        booking.refresh_from_db()
        return booking

    def record(self, kind="deposit", amount="2400.00", method="cash", booking=None, **kwargs):
        return cash_service.record_payment(
            booking or self.booking,
            kind=kind,
            method=method,
            amount=Decimal(amount),
            user=self.admin,
            paid_at=timezone.now(),
            note="Movimiento de prueba",
            **kwargs,
        )

    def test_ledger_is_the_source_of_truth_for_paid_and_balance(self):
        self.record("deposit", "2400.00")
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("2400.00"))
        self.assertEqual(booking.balance_amount, Decimal("5600.00"))
        self.assertEqual(booking.deposit_pending_amount, Decimal("0.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("5600.00"))
        self.assertTrue(booking.is_paid)
        self.assertFalse(booking.balance_paid)

        self.record("balance", "5600.00", method="transfer")
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("8000.00"))
        self.assertEqual(booking.balance_amount, Decimal("0.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("0.00"))
        self.assertTrue(booking.balance_paid)

    def test_prefetched_ledger_cache_is_invalidated_after_a_new_payment(self):
        booking = CourtBooking.objects.prefetch_related("court_payments").get(
            pk=self.booking.pk
        )
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        cash_service.record_payment(
            booking,
            kind="deposit",
            method="cash",
            amount=Decimal("2400.00"),
            user=self.admin,
        )
        self.assertEqual(booking.paid_amount, Decimal("2400.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("5600.00"))

    def test_partial_refund_after_full_payment_reopens_the_balance(self):
        self.record("deposit", "2400.00")
        self.record("balance", "5600.00")
        cash_service.record_manual_refund(
            self.booking, Decimal("1000.00"), "cash", self.admin, note="Lluvia"
        )
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("7000.00"))
        self.assertEqual(booking.refunded_amount, Decimal("1000.00"))
        self.assertEqual(booking.balance_amount, Decimal("1000.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("1000.00"))
        self.assertFalse(booking.balance_paid)
        self.assertTrue(booking.is_paid)

    def test_partial_refund_of_the_deposit_shows_both_pending_parts(self):
        self.record("deposit", "2400.00")
        cash_service.record_manual_refund(
            self.booking, Decimal("1000.00"), "cash", self.admin
        )
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("1400.00"))
        self.assertEqual(booking.deposit_pending_amount, Decimal("1000.00"))
        self.assertEqual(booking.balance_amount, Decimal("5600.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("6600.00"))
        self.assertFalse(booking.is_paid)

    def test_full_refund_clears_flags_and_leaves_nothing_refundable(self):
        self.record("deposit", "2400.00")
        cash_service.record_manual_refund(
            self.booking, Decimal("2400.00"), "transfer", self.admin
        )
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(booking.refunded_amount, Decimal("2400.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("8000.00"))
        self.assertEqual(cash_service.refundable_amount(booking), Decimal("0.00"))
        self.assertFalse(booking.is_paid)
        self.assertFalse(booking.balance_paid)
        self.assertEqual(booking.payment_status, "refunded")

    def test_cancelled_booking_has_no_pending_balance_but_keeps_refundable_money(self):
        self.record("deposit", "2400.00")
        self.booking.status = "cancelled"
        self.booking.cancelled_at = timezone.now()
        self.booking.save(update_fields=["status", "cancelled_at"])
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("2400.00"))
        self.assertEqual(booking.balance_amount, Decimal("0.00"))
        self.assertEqual(booking.deposit_pending_amount, Decimal("0.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("0.00"))
        self.assertEqual(cash_service.refundable_amount(booking), Decimal("2400.00"))

    def test_manual_refund_cannot_exceed_net_and_can_be_idempotent(self):
        self.record("deposit", "2400.00")
        with self.assertRaises(ValidationError):
            cash_service.record_manual_refund(
                self.booking, Decimal("3000.00"), "cash", self.admin
            )
        first = cash_service.record_manual_refund(
            self.booking,
            Decimal("1000.00"),
            "cash",
            self.admin,
            idempotency_key="devolucion-1",
        )
        second = cash_service.record_manual_refund(
            self.booking,
            Decimal("1000.00"),
            "cash",
            self.admin,
            idempotency_key="devolucion-1",
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            CourtPayment.objects.filter(booking=self.booking, kind="refund").count(), 1
        )
        self.assertEqual(self.refresh().paid_amount, Decimal("1400.00"))

    def test_reversal_after_partial_refund_is_capped_at_net(self):
        self.record("deposit", "2400.00")
        cash_service.record_manual_refund(
            self.booking, Decimal("1000.00"), "cash", self.admin
        )
        reversal = cash_service.record_reversal(self.booking, user=self.admin)
        self.assertIsNotNone(reversal)
        self.assertEqual(reversal.amount, Decimal("1400.00"))
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(booking.net_paid_amount, Decimal("0.00"))
        self.assertEqual(cash_service.refundable_amount(booking), Decimal("0.00"))
        self.assertFalse(booking.is_paid)

    def test_two_distinct_online_payments_are_both_recorded(self):
        cash_service.record_online_deposit(self.booking, "pay-1", amount=Decimal("2400"))
        cash_service.record_online_deposit(self.booking, "pay-2", amount=Decimal("2400"))
        booking = self.refresh()
        self.assertEqual(
            CourtPayment.objects.filter(
                booking=booking, kind__in=("deposit", "balance", "full")
            ).count(),
            2,
        )
        self.assertEqual(booking.paid_amount, Decimal("4800.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("3200.00"))

        cash_service.record_online_deposit(self.booking, "pay-3", amount=Decimal("1000"))
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("5800.00"))
        self.assertEqual(booking.overpaid_amount, Decimal("0.00"))

    def test_duplicate_online_payment_id_is_idempotent(self):
        first = cash_service.record_online_deposit(
            self.booking, "pay-1", amount=Decimal("2400")
        )
        second = cash_service.record_online_deposit(
            self.booking, "pay-1", amount=Decimal("2400")
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            CourtPayment.objects.filter(booking=self.booking, kind="deposit").count(), 1
        )
        self.assertEqual(self.refresh().paid_amount, Decimal("2400.00"))

    def test_manual_deposit_is_reconciled_with_the_online_payment(self):
        manual = self.record("deposit", "2400.00")
        online = cash_service.record_online_deposit(
            self.booking, "pay-1", amount=Decimal("2400")
        )
        self.assertEqual(manual.pk, online.pk)
        self.assertEqual(CourtPayment.objects.filter(booking=self.booking).count(), 1)
        self.assertEqual(online.external_id, "pay-1")

    def test_distinct_online_amount_is_not_swallowed_by_the_manual_entry(self):
        self.record("deposit", "2400.00")
        cash_service.record_online_deposit(self.booking, "pay-1", amount=Decimal("3000"))
        booking = self.refresh()
        self.assertEqual(CourtPayment.objects.filter(booking=booking).count(), 2)
        self.assertEqual(booking.paid_amount, Decimal("5400.00"))

    def test_online_refund_of_one_payment_is_not_capped_by_a_later_payment(self):
        cash_service.record_online_deposit(self.booking, "pay-1", amount=Decimal("2400"))
        cash_service.record_online_deposit(self.booking, "pay-2", amount=Decimal("5600"))
        cash_service.record_online_refund(self.booking, "pay-1", amount=Decimal("2400"))
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("5600.00"))
        self.assertEqual(booking.refunded_amount, Decimal("2400.00"))
        # Un segundo aviso con un total mayor para el mismo pago no inventa
        # una devolución superior a lo que ese pago aportó.
        cash_service.record_online_refund(self.booking, "pay-1", amount=Decimal("9999"))
        booking = self.refresh()
        self.assertEqual(booking.refunded_amount, Decimal("2400.00"))
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="refund").count(), 1
        )

    def test_online_partial_refunds_record_only_the_delta(self):
        cash_service.record_online_deposit(self.booking, "pay-1", amount=Decimal("2400"))
        cash_service.record_online_refund(self.booking, "pay-1", amount=Decimal("1000"))
        cash_service.record_online_refund(self.booking, "pay-1", amount=Decimal("1500"))
        cash_service.record_online_refund(self.booking, "pay-1", amount=Decimal("1500"))
        booking = self.refresh()
        refunds = CourtPayment.objects.filter(booking=booking, kind="refund")
        self.assertEqual(refunds.count(), 2)
        self.assertEqual(sum(p.amount for p in refunds), Decimal("1500.00"))
        self.assertEqual(booking.paid_amount, Decimal("900.00"))

        cash_service.record_online_refund(self.booking, "pay-1", amount=Decimal("2400"))
        self.assertEqual(
            sum(
                p.amount
                for p in CourtPayment.objects.filter(booking=booking, kind="refund")
            ),
            Decimal("2400.00"),
        )
        self.assertEqual(self.refresh().paid_amount, Decimal("0.00"))

    def test_online_refund_after_a_reversal_does_not_double_count_the_outflow(self):
        cash_service.record_online_deposit(self.booking, "pay-1", amount=Decimal("2400"))
        reversal = cash_service.record_reversal(self.booking, user=self.admin)
        self.assertIsNotNone(reversal)
        self.assertEqual(self.refresh().net_paid_amount, Decimal("0.00"))

        cash_service.record_online_refund(self.booking, "pay-1", amount=Decimal("2400"))
        booking = self.refresh()
        self.assertEqual(booking.net_paid_amount, Decimal("0.00"))
        self.assertEqual(booking.refunded_amount, Decimal("2400.00"))
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="refund").count(), 0
        )

    def test_refund_before_income_is_recorded_and_regularized_later(self):
        cash_service.record_online_refund(self.booking, "pay-x", amount=Decimal("1000"))
        booking = self.refresh()
        self.assertEqual(booking.net_paid_amount, Decimal("-1000.00"))
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(booking.refunded_amount, Decimal("1000.00"))

        cash_service.record_online_deposit(self.booking, "pay-x", amount=Decimal("2400"))
        booking = self.refresh()
        self.assertEqual(booking.net_paid_amount, Decimal("1400.00"))
        self.assertEqual(booking.paid_amount, Decimal("1400.00"))
        self.assertFalse(booking.is_paid)

    def test_legacy_booking_gets_an_opening_entry_before_the_next_movement(self):
        legacy = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(10, 30),
            customer_name="Legado",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
            is_paid=True,
            paid_at=timezone.now(),
            payment_status="manual",
            payment_provider="manual",
        )
        self.record("balance", "5600.00", booking=legacy)
        legacy = self.refresh(legacy)
        self.assertEqual(
            CourtPayment.objects.filter(booking=legacy, kind="deposit").count(), 1
        )
        self.assertEqual(
            CourtPayment.objects.filter(booking=legacy, kind="balance").count(), 1
        )
        self.assertEqual(legacy.paid_amount, Decimal("8000.00"))
        self.assertTrue(legacy.balance_paid)

    def test_legacy_booking_can_be_refunded_without_double_counting(self):
        legacy = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(12, 0),
            customer_name="Legado refund",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
            is_paid=True,
            paid_at=timezone.now(),
            payment_status="manual",
            payment_provider="manual",
        )
        cash_service.record_manual_refund(
            legacy, Decimal("2400.00"), "cash", self.admin
        )
        legacy = self.refresh(legacy)
        self.assertEqual(legacy.paid_amount, Decimal("0.00"))
        self.assertEqual(legacy.net_paid_amount, Decimal("0.00"))
        self.assertEqual(
            CourtPayment.objects.filter(booking=legacy, kind="deposit").count(), 1
        )
        self.assertEqual(
            CourtPayment.objects.filter(booking=legacy, kind="refund").count(), 1
        )

    def test_mp_payment_applies_partial_and_total_refund_amounts(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(13, 30),
            status="pending_payment",
            customer_name="MP parcial",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        approved = {
            "id": "pay-mp-1",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "2400.00",
            "currency_id": "ARS",
        }
        with mock.patch("core.views.notify_court_booking"):
            self.assertTrue(_apply_mp_payment(booking, approved))
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)

        partial = {
            **approved,
            "status": "refunded",
            "transaction_amount_refunded": "1000.00",
        }
        self.assertTrue(_apply_mp_payment(booking, partial))
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("1400.00"))
        self.assertEqual(booking.refunded_amount, Decimal("1000.00"))

        total = {
            **approved,
            "status": "refunded",
            "transaction_amount_refunded": "2400.00",
        }
        self.assertTrue(_apply_mp_payment(booking, total))
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(booking.refunded_amount, Decimal("2400.00"))
        self.assertFalse(booking.is_paid)

    def test_approved_after_refund_records_both_events_without_reviving(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(15, 0),
            status="pending_payment",
            customer_name="MP desordenado",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        refunded = {
            "id": "pay-mp-out-of-order",
            "status": "refunded",
            "external_reference": booking.code,
            "transaction_amount": "2400.00",
            "transaction_amount_refunded": "2400.00",
            "currency_id": "ARS",
        }
        self.assertTrue(_apply_mp_payment(booking, refunded))
        approved = {**refunded, "status": "approved"}
        self.assertTrue(_apply_mp_payment(booking, approved))
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, "refunded")
        self.assertEqual(booking.status, "pending_payment")
        self.assertEqual(booking.net_paid_amount, Decimal("0.00"))
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="deposit").count(), 1
        )
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="refund").count(), 1
        )

    def test_panel_button_uses_the_real_outstanding_after_a_refund(self):
        self.record("deposit", "2400.00")
        self.record("balance", "5600.00")
        cash_service.record_manual_refund(
            self.booking, Decimal("1000.00"), "cash", self.admin
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("panel_turnos"),
            {"complejo": self.complex.slug, "fecha": self.day.isoformat()},
        )
        self.assertContains(response, "Cobrar saldo $1000")
        self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "marcar_pagado",
                "turno_id": self.booking.pk,
            },
        )
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("8000.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("0.00"))
        self.assertTrue(booking.balance_paid)

    def test_caja_refund_token_prevents_double_submit(self):
        self.record("deposit", "2400.00")
        self.client.force_login(self.admin)
        today = timezone.localdate()
        response = self.client.get(
            reverse("panel_turnos_caja"),
            {
                "complejo": self.complex.slug,
                "desde": today.isoformat(),
                "hasta": today.isoformat(),
                "codigo": self.booking.code,
            },
        )
        match = re.search(
            r'name="refund_token" value="([^"]+)"', response.content.decode("utf-8")
        )
        self.assertIsNotNone(match)
        payload = {
            "complejo": self.complex.slug,
            "desde": today.isoformat(),
            "hasta": today.isoformat(),
            "codigo": self.booking.code,
            "monto": "1000,00",
            "metodo": "cash",
            "nota": "Doble clic",
            "refund_token": match.group(1),
        }
        self.client.post(reverse("panel_turnos_caja_devolucion"), payload)
        self.client.post(reverse("panel_turnos_caja_devolucion"), payload)
        booking = self.refresh()
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="refund").count(), 1
        )
        self.assertEqual(booking.paid_amount, Decimal("1400.00"))

    def test_booking_without_movements_has_no_ledger_and_keeps_the_legacy_balance(self):
        booking = self.refresh()
        self.assertEqual(booking.net_paid_amount, Decimal("0.00"))
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(booking.refunded_amount, Decimal("0.00"))
        self.assertEqual(booking.overpaid_amount, Decimal("0.00"))
        self.assertEqual(booking.deposit_pending_amount, Decimal("2400.00"))
        self.assertEqual(booking.balance_amount, Decimal("5600.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("8000.00"))
        self.assertFalse(booking.is_paid)
        self.assertFalse(booking.balance_paid)
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking).count(), 0
        )

    def test_partial_deposit_payment_keeps_the_hold_and_the_pending_parts(self):
        self.record("deposit", "1000.00")
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("1000.00"))
        self.assertEqual(booking.deposit_pending_amount, Decimal("1400.00"))
        self.assertEqual(booking.balance_amount, Decimal("5600.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("7000.00"))
        self.assertFalse(booking.is_paid)
        self.assertFalse(booking.balance_paid)

    def test_blocked_booking_has_no_pending_balance_but_keeps_the_money(self):
        self.record("deposit", "2400.00")
        self.booking.status = "blocked"
        self.booking.save(update_fields=["status"])
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("2400.00"))
        self.assertEqual(booking.balance_amount, Decimal("0.00"))
        self.assertEqual(booking.deposit_pending_amount, Decimal("0.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("0.00"))
        self.assertEqual(cash_service.refundable_amount(booking), Decimal("2400.00"))

    def test_total_refund_after_a_partial_refund_clears_the_net(self):
        self.record("deposit", "2400.00")
        self.record("balance", "5600.00")
        cash_service.record_manual_refund(
            self.booking, Decimal("1000.00"), "cash", self.admin
        )
        cash_service.record_manual_refund(
            self.booking, Decimal("7000.00"), "transfer", self.admin
        )
        booking = self.refresh()
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(booking.refunded_amount, Decimal("8000.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("8000.00"))
        self.assertFalse(booking.is_paid)
        self.assertFalse(booking.balance_paid)
        self.assertEqual(booking.payment_status, "refunded")

    def test_online_amount_above_the_deposit_is_recorded_and_flagged(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(17, 30),
            status="pending_payment",
            customer_name="MP superior",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "id": "pay-superior",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "3000.00",
            "currency_id": "ARS",
        }
        with mock.patch("core.views.notify_court_booking"):
            self.assertTrue(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.payment_status, "amount_mismatch")
        self.assertEqual(booking.paid_amount, Decimal("3000.00"))
        self.assertEqual(booking.overpaid_amount, Decimal("0.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("5000.00"))
        self.assertEqual(
            CourtPayment.objects.get(booking=booking, kind="deposit").amount,
            Decimal("3000.00"),
        )

    def test_online_amount_above_the_price_exposes_the_overpayment(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(18, 0),
            status="pending_payment",
            customer_name="MP sobrepago",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "id": "pay-sobrepago",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "9000.00",
            "currency_id": "ARS",
        }
        with mock.patch("core.views.notify_court_booking"):
            self.assertTrue(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)
        self.assertTrue(booking.balance_paid)
        self.assertEqual(booking.payment_status, "amount_mismatch")
        self.assertEqual(booking.overpaid_amount, Decimal("1000.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("0.00"))
        self.assertEqual(cash_service.refundable_amount(booking), Decimal("9000.00"))

    def test_underpayment_then_correct_payment_confirms_without_losing_cash(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(17, 0),
            status="pending_payment",
            customer_name="MP reintento",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        first = {
            "id": "pay-parcial-1",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "1000.00",
            "currency_id": "ARS",
        }
        second = {
            "id": "pay-parcial-2",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "2400.00",
            "currency_id": "ARS",
        }
        with mock.patch("core.views.notify_court_booking") as notify:
            self.assertTrue(_apply_mp_payment(booking, first))
            booking.refresh_from_db()
            self.assertEqual(booking.status, "pending_payment")
            self.assertEqual(booking.payment_status, "amount_mismatch")
            self.assertEqual(booking.paid_amount, Decimal("1000.00"))
            notify.assert_not_called()

            self.assertTrue(_apply_mp_payment(booking, second))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertTrue(booking.is_paid)
        self.assertEqual(booking.payment_status, "approved")
        self.assertEqual(booking.paid_amount, Decimal("3400.00"))
        self.assertEqual(booking.outstanding_amount, Decimal("4600.00"))
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="deposit").count(), 2
        )
        notify.assert_called_once()

    def test_underpayment_on_a_cancelled_booking_does_not_revive_it(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(16, 30),
            status="cancelled",
            customer_name="MP cancelado parcial",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "id": "pay-cancelado-parcial",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "1000.00",
            "currency_id": "ARS",
        }
        self.assertTrue(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(booking.payment_status, "approved_conflict")
        self.assertEqual(booking.paid_amount, Decimal("1000.00"))
        self.assertFalse(booking.is_paid)

    def test_chargeback_status_survives_a_repeated_approved_notification(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(15, 30),
            status="booked",
            customer_name="MP contracargo",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        chargeback = {
            "id": "pay-contracargo",
            "status": "charged_back",
            "external_reference": booking.code,
            "transaction_amount": "2400.00",
            "currency_id": "ARS",
        }
        approved = dict(chargeback, status="approved")
        self.assertTrue(_apply_mp_payment(booking, chargeback))
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, "charged_back")
        self.assertFalse(booking.is_paid)
        self.assertEqual(
            CourtPayment.objects.get(booking=booking, kind="chargeback").amount,
            Decimal("2400.00"),
        )
        self.assertFalse(
            CourtPayment.objects.filter(booking=booking, kind="deposit").exists()
        )

        self.assertTrue(_apply_mp_payment(booking, approved))
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, "charged_back")
        self.assertFalse(booking.is_paid)
        self.assertEqual(booking.net_paid_amount, Decimal("0.00"))
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="deposit").count(), 1
        )
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="chargeback").count(), 1
        )

    def test_payment_id_from_another_booking_cannot_be_reused(self):
        first = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(14, 0),
            status="booked",
            customer_name="MP primer turno",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        second = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(14, 30),
            status="pending_payment",
            customer_name="MP segundo turno",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        self.assertTrue(
            cash_service.record_online_deposit(
                first, "pay-compartido", amount=Decimal("2400")
            )
        )
        payment = {
            "id": "pay-compartido",
            "status": "approved",
            "external_reference": second.code,
            "transaction_amount": "2400.00",
            "currency_id": "ARS",
        }
        self.assertFalse(_apply_mp_payment(second, payment))
        second.refresh_from_db()
        self.assertEqual(second.status, "pending_payment")
        self.assertFalse(second.is_paid)
        self.assertFalse(CourtPayment.objects.filter(booking=second).exists())

    def test_online_payment_without_amount_does_not_invent_cash(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(16, 0),
            status="pending_payment",
            customer_name="MP sin importe",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "id": "pay-sin-importe",
            "status": "approved",
            "external_reference": booking.code,
            "currency_id": "ARS",
        }
        self.assertFalse(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertEqual(booking.payment_status, "amount_mismatch")
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertFalse(CourtPayment.objects.filter(booking=booking).exists())

    def test_online_currency_mismatch_is_flagged_without_cash(self):

        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(18, 30),
            status="pending_payment",
            customer_name="MP dólares",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "id": "pay-moneda",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "2400.00",
            "currency_id": "USD",
        }
        self.assertFalse(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)
        self.assertEqual(booking.payment_status, "amount_mismatch")
        self.assertEqual(CourtPayment.objects.filter(booking=booking).count(), 0)

    @override_settings(MERCADO_PAGO_CURRENCY="UYU")
    def test_online_payment_accepts_the_configured_currency(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(19, 15),
            status="pending_payment",
            customer_name="MP moneda configurada",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "id": "pay-moneda-configurada",
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "2400.00",
            "currency_id": "UYU",
        }
        with mock.patch("core.views.notify_court_booking"):
            self.assertTrue(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)
        self.assertEqual(booking.status, "booked")

    def test_payment_without_id_is_not_applied(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(19, 30),
            status="pending_payment",
            customer_name="MP sin id",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "status": "approved",
            "external_reference": booking.code,
            "transaction_amount": "2400.00",
            "currency_id": "ARS",
        }
        self.assertFalse(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)
        self.assertEqual(CourtPayment.objects.filter(booking=booking).count(), 0)

    def test_payment_without_external_reference_is_not_applied(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(19, 0),
            status="pending_payment",
            customer_name="MP sin referencia",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )
        payment = {
            "id": "pay-sin-referencia",
            "status": "approved",
            "external_reference": "",
            "transaction_amount": "2400.00",
            "currency_id": "ARS",
        }
        self.assertFalse(_apply_mp_payment(booking, payment))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)
        self.assertEqual(CourtPayment.objects.filter(booking=booking).count(), 0)

    def test_audit_reports_inconsistent_legacy_flags_without_writing(self):
        legacy = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(20, 0),
            customer_name="Legado inconsistente",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
            is_paid=False,
            balance_paid=True,
        )
        issues = cash_service.audit_booking(legacy)
        self.assertTrue(
            any("no se puede inferir la apertura" in issue for issue in issues)
        )
        self.assertFalse(CourtPayment.objects.filter(booking=legacy).exists())

        output = StringIO()
        call_command("audit_cash", json=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertGreaterEqual(report["bookings_with_issues"], 1)
        self.assertTrue(
            any(item["code"] == legacy.code for item in report["findings"])
        )

    def test_audit_detects_stale_flags_after_a_ledger_movement(self):
        self.record("deposit", "2400.00")
        CourtBooking.objects.filter(pk=self.booking.pk).update(is_paid=False)
        self.booking.refresh_from_db()
        issues = cash_service.audit_booking(self.booking)
        self.assertTrue(any("is_paid=False" in issue for issue in issues))

    def test_repeated_full_payment_action_does_not_double_charge(self):
        booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(16, 30),
            customer_name="Sin seña",
            price=Decimal("8000"),
            deposit_amount=Decimal("0"),
        )
        self.client.force_login(self.admin)
        payload = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "accion": "marcar_pagado",
            "turno_id": booking.pk,
        }
        self.client.post(reverse("panel_turno_accion"), payload)
        self.client.post(reverse("panel_turno_accion"), payload)
        booking.refresh_from_db()
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="full").count(), 1
        )
        self.assertEqual(booking.paid_amount, Decimal("8000.00"))
