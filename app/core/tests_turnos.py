"""Pruebas del turnero de canchas.

Cubre la grilla de horarios (duración, pausas, feriados y fechas especiales),
la reserva pública sin registro (alta, conflictos y cancelación) y la gestión
desde el panel (reservas manuales, bloqueos y configuración de horarios).
"""

from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Complex, Court, CourtBooking, CourtDateOverride, CourtSchedule, SchedulePause
from core.services import schedule as schedule_service

User = get_user_model()

# Fechas fijas: 2030-01-07 es lunes, 2030-01-12 sábado y 2030-01-13 domingo.
MONDAY = date(2030, 1, 7)
SATURDAY = date(2030, 1, 12)
SUNDAY = date(2030, 1, 13)


class TurnosFixtureMixin:
    def make_complex(self, slug="complejo-1", name="Complejo 1", **kwargs):
        return Complex.objects.create(name=name, slug=slug, **kwargs)

    def make_court(self, complex_obj, name="Cancha 1", **kwargs):
        return Court.objects.create(complex=complex_obj, name=name, **kwargs)

    def make_schedule(self, complex_obj, day_type="weekday", **kwargs):
        return CourtSchedule.objects.create(complex=complex_obj, day_type=day_type, **kwargs)

    @staticmethod
    def add_minutes(start, minutes):
        return (datetime.combine(date(2000, 1, 1), start) + timedelta(minutes=minutes)).time()

    def make_booking(self, complex_obj, court, day, start, status="booked", **kwargs):
        return CourtBooking.objects.create(
            complex=complex_obj,
            court=court,
            date=day,
            start_time=start,
            end_time=self.add_minutes(start, 90),
            status=status,
            **kwargs,
        )


class SlotGenerationTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()

    def slot_starts(self, day):
        return [start.strftime("%H:%M") for start, _ in schedule_service.slots_for_day(self.complex, day)]

    def test_default_weekday_grid(self):
        # 09:00 a 23:00 con turnos de 90 minutos → 9 turnos.
        self.assertEqual(
            self.slot_starts(MONDAY),
            ["09:00", "10:30", "12:00", "13:30", "15:00", "16:30", "18:00", "19:30", "21:00"],
        )

    def test_pause_breaks_the_sequence_and_resumes_after(self):
        schedule = self.make_schedule(self.complex)
        SchedulePause.objects.create(schedule=schedule, start_time="13:00", end_time="14:00")
        # El turno de 12:00 se pisa con la pausa y el siguiente arranca 14:00.
        self.assertEqual(
            self.slot_starts(MONDAY),
            ["09:00", "10:30", "14:00", "15:30", "17:00", "18:30", "20:00", "21:30"],
        )

    def test_half_hour_pause(self):
        schedule = self.make_schedule(self.complex)
        SchedulePause.objects.create(schedule=schedule, start_time="13:30", end_time="14:00")
        self.assertEqual(
            self.slot_starts(MONDAY),
            ["09:00", "10:30", "12:00", "14:00", "15:30", "17:00", "18:30", "20:00", "21:30"],
        )

    def test_each_day_type_has_its_own_start(self):
        self.make_schedule(self.complex, day_type="weekday", start_time="09:00", end_time="23:00")
        self.make_schedule(self.complex, day_type="saturday", start_time="10:00", end_time="22:00", slot_minutes=120)
        self.make_schedule(self.complex, day_type="sunday", start_time="15:00", end_time="21:00")
        self.assertEqual(
            self.slot_starts(SATURDAY),
            ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"],
        )
        self.assertEqual(self.slot_starts(SUNDAY), ["15:00", "16:30", "18:00", "19:30"])

    def test_closed_day_type_has_no_slots(self):
        self.make_schedule(self.complex, day_type="sunday", is_open=False)
        self.assertEqual(self.slot_starts(SUNDAY), [])
        self.assertEqual(len(self.slot_starts(MONDAY)), 9)

    def test_closed_override_cancels_everything(self):
        CourtDateOverride.objects.create(complex=self.complex, date=MONDAY, is_closed=True, note="feriado")
        self.assertEqual(self.slot_starts(MONDAY), [])
        config = schedule_service.day_config(self.complex, MONDAY)
        self.assertFalse(config.is_open)
        self.assertEqual(config.override.note, "feriado")

    def test_open_override_sets_custom_hours(self):
        CourtDateOverride.objects.create(
            complex=self.complex, date=MONDAY, is_closed=False, start_time="15:00", end_time="21:00"
        )
        self.assertEqual(self.slot_starts(MONDAY), ["15:00", "16:30", "18:00", "19:30"])

    def test_override_hours_multiple_of_slot(self):
        # Cierre 20:15 no coincide con la grilla: el último turno empieza 18:00 y termina 19:30.
        self.make_schedule(self.complex, slot_minutes=90)
        CourtDateOverride.objects.create(
            complex=self.complex, date=MONDAY, is_closed=False, start_time="18:00", end_time="20:15"
        )
        self.assertEqual(self.slot_starts(MONDAY), ["18:00"])

    def test_slot_for_validates_grid(self):
        self.assertIsNotNone(schedule_service.slot_for(self.complex, MONDAY, time(9, 0)))
        self.assertIsNone(schedule_service.slot_for(self.complex, MONDAY, time(9, 10)))

    def test_is_slot_past(self):
        today = timezone.localdate()
        self.assertTrue(schedule_service.is_slot_past(today - timedelta(days=1), time(9, 0)))
        self.assertFalse(schedule_service.is_slot_past(today + timedelta(days=1), time(9, 0)))

    def test_unique_active_booking_constraint(self):
        court = self.make_court(self.complex)
        self.make_booking(self.complex, court, MONDAY, time(9, 0))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_booking(self.complex, court, MONDAY, time(9, 0))

    def test_cancelled_booking_frees_the_slot(self):
        court = self.make_court(self.complex)
        booking = self.make_booking(self.complex, court, MONDAY, time(9, 0))
        booking.status = "cancelled"
        booking.save(update_fields=["status"])
        # No hay choque: el mismo horario se puede volver a reservar.
        self.make_booking(self.complex, court, MONDAY, time(9, 0))


class PublicBookingTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        # Asegura que el día elegido tenga la grilla hábil aunque caiga fin de semana.
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))

    def reserve(self, **overrides):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "cancha": self.court.pk,
            "hora": "09:00",
            "nombre": "Ana Pérez",
            "telefono": "2215551234",
            "nota": "",
        }
        data.update(overrides)
        return self.client.post(reverse("turno_reservar"), data)

    def test_public_page_shows_free_slots(self):
        response = self.client.get(reverse("turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Libre")
        self.assertContains(response, self.complex.name)

    def test_booking_form_prefilled_for_free_slot(self):
        response = self.client.get(
            reverse("turnos"),
            {"complejo": self.complex.slug, "fecha": self.day.isoformat(), "cancha": self.court.pk, "hora": "09:00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirmá tu turno")

    def test_public_booking_creates_and_redirects_to_confirmation(self):
        response = self.reserve()
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.source, "public")
        self.assertEqual(booking.customer_name, "Ana Pérez")
        self.assertEqual(len(booking.code), 8)
        self.assertRedirects(response, reverse("turno_confirmado", args=[booking.code]))

    def test_booked_slot_cannot_be_reserved_again(self):
        self.reserve()
        response = self.reserve(nombre="Otro Cliente")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CourtBooking.objects.count(), 1)
        # La grilla pública muestra el horario como ocupado.
        self.assertContains(self.client.get(response.url), "Ocupado")

    def test_booking_requires_name_and_phone(self):
        self.reserve(nombre="")
        self.reserve(telefono="123")
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_booking_outside_grid_rejected(self):
        self.reserve(hora="09:10")
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_past_date_rejected(self):
        self.reserve(fecha=(timezone.localdate() - timedelta(days=1)).isoformat())
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_cancellation_frees_the_slot(self):
        self.reserve()
        booking = CourtBooking.objects.get()
        response = self.client.post(reverse("turno_cancelar", args=[booking.code]))
        self.assertRedirects(response, reverse("turno_confirmado", args=[booking.code]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertIsNotNone(booking.cancelled_at)
        # El horario vuelve a estar libre.
        response = self.reserve(nombre="Otra Persona")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CourtBooking.objects.filter(status="booked").count(), 1)

    def test_confirmation_page_shows_code(self):
        self.reserve()
        booking = CourtBooking.objects.get()
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, booking.code)

    def test_complex_without_courts_is_not_offered_publicly(self):
        other = self.make_complex(slug="complejo-2", name="Complejo 2")
        response = self.client.get(reverse("turnos"), {"complejo": other.slug, "fecha": self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        # El selector público solo muestra complejos con canchas activas.
        self.assertNotContains(response, "Complejo 2")
        self.assertContains(response, self.complex.name)

    def test_no_bookable_complexes_shows_message(self):
        Court.objects.update(is_active=False)
        response = self.client.get(reverse("turnos"), {"fecha": self.day.isoformat()})
        self.assertContains(response, "no hay complejos disponibles")


class PanelTurnosTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.other_complex = self.make_complex(slug="complejo-2", name="Complejo 2")
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))

        self.admin = User.objects.create_superuser("admin-turnos", password="clave-segura-123")
        self.staff = User.objects.create_user("staff-turnos", password="clave-segura-123")
        self.staff.complexes.add(self.other_complex)

    def panel_data(self, action="reservar", **overrides):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "accion": action,
            "cancha": self.court.pk,
            "hora": "09:00",
            "nombre": "Cliente Panel",
            "telefono": "2215550000",
            "nota": "",
        }
        data.update(overrides)
        return data

    def test_panel_requires_login(self):
        response = self.client.get(reverse("panel_turnos"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)

    def test_staff_of_other_complex_cannot_book(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("panel_turno_accion"), self.panel_data())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_admin_creates_manual_booking(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("panel_turno_accion"), self.panel_data())
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.source, "panel")
        self.assertEqual(booking.created_by, self.admin)
        self.assertEqual(booking.customer_name, "Cliente Panel")

    def test_block_and_release_slot(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("panel_turno_accion"), self.panel_data(action="bloquear", nombre=""))
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "blocked")

        response = self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "cancelar",
                "turno_id": booking.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_schedule_saved_from_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_horario",
                "tipo_dia": "saturday",
                "abierto": "on",
                "desde": "10:00",
                "hasta": "22:00",
                "duracion": "120",
            },
        )
        self.assertEqual(response.status_code, 302)
        schedule = CourtSchedule.objects.get(complex=self.complex, day_type="saturday")
        self.assertEqual(schedule.start_time.strftime("%H:%M"), "10:00")
        self.assertEqual(schedule.slot_minutes, 120)

    def test_pause_added_from_panel(self):
        self.client.force_login(self.admin)
        schedule = self.make_schedule(self.complex, day_type="weekday")
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "agregar_pausa",
                "horario_id": schedule.pk,
                "desde": "13:00",
                "hasta": "14:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(schedule.pauses.count(), 1)

    def test_special_date_from_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_fecha",
                "fecha": self.day.isoformat(),
                "cerrado": "on",
                "nota": "feriado",
            },
        )
        self.assertEqual(response.status_code, 302)
        override = CourtDateOverride.objects.get(complex=self.complex, date=self.day)
        self.assertTrue(override.is_closed)

    def test_grid_shows_customer_name_in_panel(self):
        self.client.force_login(self.admin)
        self.make_booking(self.complex, self.court, self.day, time(9, 0), customer_name="Ana")
        response = self.client.get(
            reverse("panel_turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ana")
