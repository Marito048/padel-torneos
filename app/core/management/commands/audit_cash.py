"""Auditoría de solo lectura del libro de caja y las banderas legadas.

No corrige datos. Sirve para decidir una reparación manual después de una copia
de seguridad y para comparar el antes/después de esa reparación.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from core.models import CourtBooking, CourtPayment
from core.services import cash as cash_service


class Command(BaseCommand):
    help = "Informa incoherencias de caja sin modificar la base."

    def add_arguments(self, parser):
        parser.add_argument("--complex", help="Slug del complejo a auditar.")
        parser.add_argument(
            "--json",
            action="store_true",
            help="Salida JSON apta para guardar como evidencia.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Sale con código 1 si encuentra incoherencias.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Máximo de turnos a revisar (0 = todos).",
        )

    def handle(self, *args, **options):
        queryset = (
            CourtBooking.objects.select_related("complex", "court")
            .prefetch_related("court_payments")
            .order_by("complex__slug", "date", "start_time", "pk")
        )
        if options["complex"]:
            queryset = queryset.filter(complex__slug=options["complex"])
        if options["limit"] and options["limit"] > 0:
            queryset = queryset[: options["limit"]]

        findings = []
        for booking in queryset:
            issues = cash_service.audit_booking(
                booking, payments=list(booking.court_payments.all())
            )
            if issues:
                findings.append(
                    {
                        "booking_id": booking.pk,
                        "code": booking.code,
                        "complex": booking.complex.slug,
                        "date": booking.date.isoformat(),
                        "start_time": booking.start_time.isoformat(timespec="minutes"),
                        "status": booking.status,
                        "issues": issues,
                    }
                )

        orphan_payments = CourtPayment.objects.filter(booking__isnull=True).count()
        duplicate_reversals = (
            CourtPayment.objects.filter(reverses__isnull=False)
            .values("reverses_id")
            .annotate(total=Count("id"))
            .filter(total__gt=1)
            .count()
        )
        report = {
            "bookings_reviewed": queryset.count(),
            "bookings_with_issues": len(findings),
            "orphan_payments": orphan_payments,
            "duplicate_reversals": duplicate_reversals,
            "findings": findings,
        }
        if options["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(
                f"Turnos revisados: {report['bookings_reviewed']} · "
                f"con incoherencias: {report['bookings_with_issues']} · "
                f"movimientos huérfanos: {orphan_payments} · "
                f"reversiones duplicadas: {duplicate_reversals}"
            )
            for finding in findings:
                self.stdout.write(
                    f"- {finding['code']} {finding['complex']} "
                    f"{finding['date']} {finding['start_time']}: "
                    + " | ".join(finding["issues"])
                )
        if options["strict"] and (
            findings or orphan_payments or duplicate_reversals
        ):
            raise SystemExit(1)
