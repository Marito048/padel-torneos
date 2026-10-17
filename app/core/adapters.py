"""Adaptadores de django-allauth.

El acceso al panel es por invitación: la administración crea los usuarios a
mano o carga el email de Google en la lista de invitaciones. Cuando la persona
ingresa con Google, la cuenta se crea/completa automáticamente si el email
(verificado por el proveedor) tiene una invitación pendiente; sin invitación el
registro sigue cerrado.
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.urls import reverse
from django.utils import timezone

from core.models import Invitation, TemporaryPassword


class PadelAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False

    def set_password(self, user, password):
        super().set_password(user, password)
        # La contraseña dejó de ser la temporal que entregó la administración.
        TemporaryPassword.objects.filter(user=user).delete()

    def get_password_change_redirect_url(self, request):
        # Después de cambiarla, de vuelta al panel (con el aviso de éxito).
        return reverse("tournament_select")


class PadelSocialAccountAdapter(DefaultSocialAccountAdapter):
    def _pending_invitation(self, sociallogin):
        for address in sociallogin.email_addresses:
            if not address.verified:
                continue
            invitation = Invitation.find_pending(address.email)
            if invitation:
                return invitation
        return None

    def is_open_for_signup(self, request, sociallogin):
        # Sólo se abre el registro para emails invitados (verificados por Google).
        if self._pending_invitation(sociallogin):
            return True
        return super().is_open_for_signup(request, sociallogin)

    def is_auto_signup_allowed(self, request, sociallogin):
        # Invitación con complejo (personal de un complejo existente): la cuenta
        # se crea sin formulario, con los datos que vienen de Google.
        invitation = self._pending_invitation(sociallogin)
        if invitation and invitation.complex_id:
            return True
        return super().is_auto_signup_allowed(request, sociallogin)

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if self._pending_invitation(sociallogin):
            # Los invitados entran al panel.
            user.is_staff = True
        return user

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        invitation = self._pending_invitation(sociallogin)
        if invitation:
            # El formulario social no siempre pasó por `populate_user`; el
            # acceso al panel se garantiza acá.
            if not user.is_staff:
                user.is_staff = True
                user.save(update_fields=["is_staff"])
            invitation.used_at = timezone.now()
            invitation.used_by = user
            invitation.save(update_fields=["used_at", "used_by"])
            if invitation.complex_id:
                user.complexes.add(invitation.complex)
                sociallogin.state["next"] = reverse("tournament_select")
            else:
                # Futuro dueño: va a Configuración general a crear su complejo.
                sociallogin.state["next"] = reverse("general_settings")
        return user
