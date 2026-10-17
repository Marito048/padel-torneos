"""Formularios propios de Pádel Server."""

from allauth.socialaccount.forms import SignupForm
from django import forms


class PadelSocialSignupForm(SignupForm):
    """Completa el registro del invitado que entra con Google.

    allauth llega a este formulario sólo cuando el email verificado de Google
    tiene una invitación pendiente. Se piden nombre, apellido y usuario; el
    email no se pregunta porque ya vino de la cuenta de Google.
    """

    first_name = forms.CharField(label="Nombre", max_length=150, required=True)
    last_name = forms.CharField(label="Apellido", max_length=150, required=True)

    def _get_signup_fields(self, kwargs):
        # El email ya está verificado por Google y coincidió con la invitación;
        # sin este ajuste allauth exige que ACCOUNT_SIGNUP_FIELDS incluya
        # "email" para el registro social.
        kwargs.pop("email_required", None)
        return super()._get_signup_fields(kwargs)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].widget.attrs.setdefault("autocomplete", "given-name")
        self.fields["last_name"].widget.attrs.setdefault("autocomplete", "family-name")
        self.order_fields(["first_name", "last_name", "username"])
