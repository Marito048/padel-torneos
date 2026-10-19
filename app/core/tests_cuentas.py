"""Pruebas de administración de cuentas y complejos.

Cubre la suspensión/reactivación de usuarios (licencia impaga), la eliminación
definitiva de cuentas con sus complejos y torneos, y la eliminación o abandono
de complejos por parte de sus administradores.
"""

from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Complex, Court, CourtBooking, Player, Registration, Team, Tournament

User = get_user_model()


class AccountsFixtureMixin:
    def make_user(self, username, *, password="clave-segura-123", **kwargs):
        return User.objects.create_user(username, password=password, **kwargs)

    def make_complex(self, name, slug, *users):
        complex_obj = Complex.objects.create(name=name, slug=slug)
        if users:
            complex_obj.users.add(*users)
        return complex_obj

    def make_tournament(self, complex_obj, name, slug):
        return Tournament.objects.create(
            complex=complex_obj,
            name=name,
            slug=slug,
            start_date=timezone.localdate(),
        )

    def make_booking(self, complex_obj, court):
        return CourtBooking.objects.create(
            complex=complex_obj,
            court=court,
            date=timezone.localdate() + timedelta(days=1),
            start_time=time(9, 0),
            end_time=time(10, 30),
            customer_name="Cliente",
        )

    def make_team(self):
        p1 = Player.objects.create(first_name="Ana", last_name="Pérez", gender="F")
        p2 = Player.objects.create(first_name="Beto", last_name="Gómez", gender="M")
        return Team.objects.create(player1=p1, player2=p2)


class SuspendUserTests(AccountsFixtureMixin, TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("jefe", password="clave-segura-000")
        self.staff = self.make_user("encargado", is_staff=True)
        self.target = self.make_user("moroso", is_staff=True)
        self.complex = self.make_complex("Complejo Uno", "complejo-uno", self.staff, self.target)
        self.client.force_login(self.boss)

    def test_suspend_blocks_login_and_preserves_data(self):
        response = self.client.post(
            reverse("general_user_toggle_active", args=[self.target.pk]), follow=True
        )
        self.assertContains(response, "suspendido")
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)
        self.assertTrue(self.complex.users.filter(pk=self.target.pk).exists())

        self.client.logout()
        self.assertFalse(self.client.login(username="moroso", password="clave-segura-123"))

        self.client.force_login(self.boss)
        response = self.client.post(
            reverse("general_user_toggle_active", args=[self.target.pk]), follow=True
        )
        self.assertContains(response, "reactivado")
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.client.logout()
        self.assertTrue(self.client.login(username="moroso", password="clave-segura-123"))

    def test_cannot_suspend_self(self):
        response = self.client.post(
            reverse("general_user_toggle_active", args=[self.boss.pk]), follow=True
        )
        self.assertContains(response, "No podés suspender tu propia cuenta")
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.is_active)

    def test_staff_without_access_cannot_suspend(self):
        outsider = self.make_user("ajeno", is_staff=True)
        self.client.force_login(outsider)
        response = self.client.post(reverse("general_user_toggle_active", args=[self.target.pk]))
        self.assertEqual(response.status_code, 403)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_list_shows_status_and_suspend_actions(self):
        self.target.is_active = False
        self.target.save(update_fields=["is_active"])
        response = self.client.get(reverse("general_settings"))
        self.assertContains(response, "Suspendido")
        self.assertContains(response, "Reactivar")
        self.assertContains(response, reverse("general_user_delete", args=[self.target.pk]))

    def test_suspend_button_in_user_form(self):
        response = self.client.get(reverse("general_user_edit", args=[self.target.pk]))
        self.assertContains(response, "Suspender cuenta")
        self.assertContains(response, "Eliminar definitivamente")


class DeleteUserTests(AccountsFixtureMixin, TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("jefe", password="clave-segura-000")
        self.target = self.make_user("moroso", is_staff=True)
        self.partner = self.make_user("socio", is_staff=True)
        self.own_complex = self.make_complex("Complejo Propio", "complejo-propio", self.target)
        self.shared_complex = self.make_complex(
            "Complejo Compartido", "complejo-compartido", self.target, self.partner
        )
        self.own_tournament = self.make_tournament(self.own_complex, "Torneo Propio", "torneo-propio")
        self.shared_tournament = self.make_tournament(
            self.shared_complex, "Torneo Compartido", "torneo-compartido"
        )
        self.court = Court.objects.create(complex=self.own_complex, name="Cancha 1")
        self.booking = self.make_booking(self.own_complex, self.court)
        self.team = self.make_team()
        Registration.objects.create(tournament=self.own_tournament, team=self.team, status="confirmed")
        self.client.force_login(self.boss)

    def test_delete_page_requires_superuser(self):
        self.client.force_login(self.partner)
        response = self.client.get(reverse("general_user_delete", args=[self.target.pk]))
        self.assertEqual(response.status_code, 403)

    def test_delete_requires_exact_username(self):
        url = reverse("general_user_delete", args=[self.target.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complejo Propio")
        self.assertContains(response, "Complejo Compartido")

        response = self.client.post(url, {"confirm_username": "otro"}, follow=True)
        self.assertContains(response, "nombre de usuario exacto")
        self.assertTrue(User.objects.filter(pk=self.target.pk).exists())
        self.assertTrue(Complex.objects.filter(pk=self.own_complex.pk).exists())

        response = self.client.post(url, {"confirm_username": "moroso"}, follow=True)
        self.assertContains(response, "eliminado definitivamente")
        self.assertFalse(User.objects.filter(pk=self.target.pk).exists())
        self.assertFalse(Complex.objects.filter(pk=self.own_complex.pk).exists())
        self.assertFalse(Tournament.objects.filter(pk=self.own_tournament.pk).exists())
        self.assertFalse(Court.objects.filter(pk=self.court.pk).exists())
        self.assertFalse(CourtBooking.objects.filter(pk=self.booking.pk).exists())

        # El complejo compartido y su torneo quedan, pero sin el usuario eliminado.
        self.assertTrue(Complex.objects.filter(pk=self.shared_complex.pk).exists())
        self.assertEqual(self.shared_complex.users.count(), 1)
        self.assertTrue(self.shared_complex.users.filter(pk=self.partner.pk).exists())
        self.assertTrue(Tournament.objects.filter(pk=self.shared_tournament.pk).exists())

        # La base compartida de jugadores y duplas se conserva.
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())
        self.assertEqual(Player.objects.count(), 2)

    def test_cannot_delete_self(self):
        response = self.client.post(
            reverse("general_user_delete", args=[self.boss.pk]),
            {"confirm_username": "jefe"},
            follow=True,
        )
        self.assertContains(response, "No podés eliminar tu propia cuenta")
        self.assertTrue(User.objects.filter(pk=self.boss.pk).exists())

    def test_non_superuser_does_not_see_delete_link(self):
        self.client.force_login(self.partner)
        response = self.client.get(reverse("general_settings"))
        self.assertNotContains(response, reverse("general_user_delete", args=[self.target.pk]))


class DeleteComplexTests(AccountsFixtureMixin, TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("jefe", password="clave-segura-000")
        self.owner = self.make_user("dueno", is_staff=True)
        self.partner = self.make_user("socio", is_staff=True)
        self.own_complex = self.make_complex("Complejo Propio", "complejo-propio", self.owner)
        self.shared_complex = self.make_complex(
            "Complejo Compartido", "complejo-compartido", self.owner, self.partner
        )
        self.tournament = self.make_tournament(self.own_complex, "Torneo Propio", "torneo-propio")
        self.shared_tournament = self.make_tournament(
            self.shared_complex, "Torneo Compartido", "torneo-compartido"
        )
        self.court = Court.objects.create(complex=self.own_complex, name="Cancha 1")
        self.booking = self.make_booking(self.own_complex, self.court)
        self.team = self.make_team()
        Registration.objects.create(tournament=self.tournament, team=self.team, status="confirmed")
        self.client.force_login(self.owner)

    def test_owner_deletes_own_complex_with_confirmation(self):
        url = reverse("general_complex_delete", args=[self.own_complex.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complejo Propio")

        response = self.client.post(url, {"confirm_name": "otro"}, follow=True)
        self.assertContains(response, "nombre del complejo exacto")
        self.assertTrue(Complex.objects.filter(pk=self.own_complex.pk).exists())

        response = self.client.post(url, {"confirm_name": "Complejo Propio"}, follow=True)
        self.assertContains(response, "eliminado con sus torneos")
        self.assertFalse(Complex.objects.filter(pk=self.own_complex.pk).exists())
        self.assertFalse(Tournament.objects.filter(pk=self.tournament.pk).exists())
        self.assertFalse(Court.objects.filter(pk=self.court.pk).exists())
        self.assertFalse(CourtBooking.objects.filter(pk=self.booking.pk).exists())
        # El otro complejo, sus torneos y la base de jugadores quedan intactos.
        self.assertTrue(Complex.objects.filter(pk=self.shared_complex.pk).exists())
        self.assertTrue(Tournament.objects.filter(pk=self.shared_tournament.pk).exists())
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())
        self.assertEqual(Player.objects.count(), 2)

    def test_owner_cannot_delete_shared_complex(self):
        url = reverse("general_complex_delete", args=[self.shared_complex.pk])
        response = self.client.get(url)
        self.assertContains(response, "sólo el administrador general puede eliminarlo")

        response = self.client.post(url, {"confirm_name": "Complejo Compartido"}, follow=True)
        self.assertContains(response, "otros usuarios asignados")
        self.assertTrue(Complex.objects.filter(pk=self.shared_complex.pk).exists())

    def test_superuser_deletes_shared_complex(self):
        self.client.force_login(self.boss)
        response = self.client.post(
            reverse("general_complex_delete", args=[self.shared_complex.pk]),
            {"confirm_name": "Complejo Compartido"},
            follow=True,
        )
        self.assertContains(response, "eliminado con sus torneos")
        self.assertFalse(Complex.objects.filter(pk=self.shared_complex.pk).exists())
        self.assertTrue(Complex.objects.filter(pk=self.own_complex.pk).exists())

    def test_owner_leaves_shared_complex(self):
        response = self.client.post(
            reverse("general_complex_leave", args=[self.shared_complex.pk]), follow=True
        )
        self.assertContains(response, "Dejaste de administrar")
        self.assertFalse(self.shared_complex.users.filter(pk=self.owner.pk).exists())
        self.assertTrue(self.shared_complex.users.filter(pk=self.partner.pk).exists())
        self.assertTrue(Complex.objects.filter(pk=self.shared_complex.pk).exists())

    def test_leave_sole_complex_rejected(self):
        response = self.client.post(
            reverse("general_complex_leave", args=[self.own_complex.pk]), follow=True
        )
        self.assertContains(response, "única persona a cargo")
        self.assertTrue(self.own_complex.users.filter(pk=self.owner.pk).exists())

    def test_settings_list_shows_complex_actions(self):
        response = self.client.get(reverse("general_settings"))
        self.assertContains(response, reverse("general_complex_delete", args=[self.own_complex.pk]))
        self.assertContains(response, reverse("general_complex_leave", args=[self.shared_complex.pk]))
        self.assertNotContains(response, reverse("general_complex_leave", args=[self.own_complex.pk]))
