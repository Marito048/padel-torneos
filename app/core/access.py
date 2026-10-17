"""Control de acceso multi-complejo y multi-torneo.

Reglas:
- Superusuario: acceso total.
- Torneo sin complejo asignado (datos heredados): accesible para staff autenticado.
- Torneo con complejo: sólo usuarios vinculados al complejo.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from core.models import Complex, Tournament


def is_superuser(user) -> bool:
    return bool(user and user.is_authenticated and user.is_superuser)


def accessible_complex_ids(user) -> set[int]:
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set(Complex.objects.values_list("id", flat=True))
    return set(user.complexes.values_list("id", flat=True))


def scoped_tournaments(user):
    """QuerySet de torneos visibles para el usuario."""
    qs = Tournament.objects.all()
    if is_superuser(user):
        return qs
    if not user or not user.is_authenticated:
        return qs.none()
    return qs.filter(Q(complex__isnull=True) | Q(complex_id__in=accessible_complex_ids(user))).distinct()


def user_can_access_complex(user, complex_obj: Complex | None) -> bool:
    if complex_obj is None:
        return True
    if is_superuser(user):
        return True
    if not user or not user.is_authenticated:
        return False
    return user.complexes.filter(pk=complex_obj.pk).exists()


def user_can_access_tournament(user, tournament: Tournament | None) -> bool:
    if tournament is None:
        return False
    if is_superuser(user):
        return True
    if not user or not user.is_authenticated:
        return False
    if tournament.complex_id is None:
        # Datos heredados sin complejo: sólo staff de la plataforma.
        return bool(user.is_staff)
    return user.complexes.filter(pk=tournament.complex_id).exists()


def require_tournament_access(user, tournament: Tournament) -> None:
    if not user_can_access_tournament(user, tournament):
        raise PermissionDenied("No tenés acceso a este torneo.")


def require_complex_access(user, complex_obj: Complex) -> None:
    if not user_can_access_complex(user, complex_obj):
        raise PermissionDenied("No tenés acceso a este complejo.")
