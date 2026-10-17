"""Context processors del proyecto."""

from django.conf import settings

from core.models import TemporaryPassword


def auth_extras(request):
    """Expone banderas de autenticación a las plantillas."""
    user = getattr(request, "user", None)
    requires_password_change = False
    if user is not None and user.is_authenticated:
        requires_password_change = TemporaryPassword.objects.filter(user=user).exists()
    return {
        "google_login_enabled": getattr(settings, "GOOGLE_LOGIN_ENABLED", False),
        "requires_password_change": requires_password_change,
    }
