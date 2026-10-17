import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.db.models.functions import Greatest, Least, Lower

PLAYOFF_STAGE_ORDER = {
    "round_of_64": 1,
    "round_of_32": 2,
    "round_of_16": 3,
    "quarter": 4,
    "semi": 5,
    "final": 6,
}


def _valid_set_score(games_a: int, games_b: int) -> bool:
    """Un set válido de pádel: 6-x (x<=4), 7-5 o 7-6."""
    high, low = max(games_a, games_b), min(games_a, games_b)
    if games_a == games_b:
        return False
    return (high == 6 and low <= 4) or (high == 7 and low in (5, 6))


def classify_score(score: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]], list[str]]:
    """Clasifica los tokens de un marcador en (sets, supers, desconocidos).

    El super tie-break se acepta entre corchetes (``[11-9]``) o suelto, al final
    (``11-9``): un token que no es un set válido y cuyo ganador llega a 8 games
    o más se interpreta como super tie-break.
    """
    sets: list[tuple[int, int]] = []
    supers: list[tuple[int, int]] = []
    unknown: list[str] = []
    for token in (score or "").split():
        bracket_match = re.fullmatch(r"\[(\d+)-(\d+)\]", token)
        if bracket_match:
            supers.append((int(bracket_match.group(1)), int(bracket_match.group(2))))
            continue
        plain_match = re.fullmatch(r"(\d+)-(\d+)", token)
        if not plain_match:
            unknown.append(token)
            continue
        games_a, games_b = int(plain_match.group(1)), int(plain_match.group(2))
        if _valid_set_score(games_a, games_b) or max(games_a, games_b) < 8:
            sets.append((games_a, games_b))
        else:
            supers.append((games_a, games_b))
    return sets, supers, unknown


def implied_winner_id(score: str, team1_id, team2_id) -> int | None:
    """Ganador que sugiere un marcador leído con team1 primero (None si no lo define)."""
    sets, supers, _ = classify_score(score)
    sets_t1 = sum(a > b for a, b in sets)
    sets_t2 = sum(b > a for a, b in sets)
    if sets_t1 != sets_t2:
        return team1_id if sets_t1 > sets_t2 else team2_id
    if supers and supers[0][0] != supers[0][1]:
        return team1_id if supers[0][0] > supers[0][1] else team2_id
    return None


def flip_score(score: str) -> str:
    """Espeja el marcador (6-3 2-6 → 3-6 6-2), incluido el super tie-break."""
    return re.sub(r"(\d+)-(\d+)", lambda match: f"{match.group(2)}-{match.group(1)}", score or "")


class Complex(models.Model):
    """Complejo deportivo. Aísla los datos de cada club."""

    name = models.CharField("Nombre", max_length=120)
    slug = models.SlugField("Slug", unique=True)
    address = models.CharField("Dirección", max_length=200, blank=True)
    city = models.CharField("Ciudad", max_length=120, blank=True)
    phone = models.CharField("Teléfono", max_length=40, blank=True)
    email = models.EmailField("Email", blank=True)
    is_active = models.BooleanField("Activo", default=True)
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="complexes",
        verbose_name="Usuarios autorizados",
    )
    created_at = models.DateTimeField("Creado", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Complejo"
        verbose_name_plural = "Complejos"

    def __str__(self) -> str:
        return self.name


class Invitation(models.Model):
    """Invitación al panel por email de Google.

    La administración carga el email de la persona; cuando ingresa con Google,
    su cuenta se crea automáticamente. Si la invitación trae un complejo
    asignado (personal de un complejo existente) entra directo a él; si no
    (futuro dueño), completa su registro y crea su propio complejo.
    """

    email = models.EmailField("Email de Google")
    complex = models.ForeignKey(
        Complex,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations",
        verbose_name="Complejo (opcional)",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_invitations",
        verbose_name="Invitada por",
    )
    created_at = models.DateTimeField("Creada", auto_now_add=True)
    used_at = models.DateTimeField("Usada", null=True, blank=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="used_invitations",
        verbose_name="Usada por",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Invitación"
        verbose_name_plural = "Invitaciones"
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                condition=Q(used_at__isnull=True),
                name="unique_pending_invitation_email",
            )
        ]

    def __str__(self) -> str:
        return self.email

    @classmethod
    def find_pending(cls, email: str | None):
        """Invitación pendiente para ese email (sin distinguir mayúsculas)."""
        if not email:
            return None
        return cls.objects.filter(used_at__isnull=True, email__iexact=email.strip()).first()


class RegistrationRequest(models.Model):
    """Solicitud pública de cuenta, para quien no usa Google.

    Se carga desde la página «Registrarse» con los datos de la persona y de su
    complejo; la administración general las ve en Configuración general y desde
    ahí crea el usuario (con esos datos precargados) o descarta la solicitud.
    """

    email = models.EmailField("Email")
    first_name = models.CharField("Nombre", max_length=150, blank=True, default="")
    last_name = models.CharField("Apellido", max_length=150, blank=True, default="")
    phone = models.CharField("Teléfono", max_length=40, blank=True, default="")
    complex_name = models.CharField("Complejo", max_length=120, blank=True, default="")
    complex_city = models.CharField("Ciudad del complejo", max_length=120, blank=True, default="")
    complex_address = models.CharField(
        "Dirección del complejo", max_length=200, blank=True, default=""
    )
    created_at = models.DateTimeField("Creada", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Solicitud de registro"
        verbose_name_plural = "Solicitudes de registro"
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="unique_registration_request_email",
            )
        ]

    def __str__(self) -> str:
        return self.email


class TemporaryPassword(models.Model):
    """Marca de contraseña temporal entregada por la administración.

    Se crea al dar de alta un usuario con contraseña (o al resetearla desde la
    administración) y se elimina cuando la persona la cambia: el adaptador de
    allauth la limpia al pasar por ``PadelAccountAdapter.set_password``.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="temporary_password",
        verbose_name="Usuario",
    )
    created_at = models.DateTimeField("Creada", auto_now_add=True)

    class Meta:
        verbose_name = "Contraseña temporal"
        verbose_name_plural = "Contraseñas temporales"

    def __str__(self) -> str:
        return self.user.username


class Court(models.Model):
    SURFACE = [
        ("artificial", "Césped artificial"),
        ("crystal", "Cristal"),
        ("wall", "Muro"),
    ]
    complex = models.ForeignKey(
        Complex,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="courts",
        verbose_name="Complejo",
    )
    name = models.CharField("Cancha", max_length=60)
    surface = models.CharField("Superficie", max_length=20, choices=SURFACE, default="artificial")
    is_active = models.BooleanField("Activa", default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Cancha"
        verbose_name_plural = "Canchas"

    def __str__(self) -> str:
        return self.name


class Player(models.Model):
    GENDER = [("M", "Masculino"), ("F", "Femenino")]
    LEVEL = [
        ("principiante", "Principiante"),
        ("octava", "Octava"),
        ("septima", "Séptima"),
        ("sexta", "Sexta"),
        ("quinta", "Quinta"),
        ("cuarta", "Cuarta"),
        ("tercera", "Tercera"),
        ("segunda", "Segunda"),
        ("primera", "Primera"),
    ]

    first_name = models.CharField("Nombre", max_length=60)
    last_name = models.CharField("Apellido", max_length=60)
    phone = models.CharField("Teléfono", max_length=40, blank=True)
    email = models.EmailField(blank=True)
    gender = models.CharField("Género", max_length=1, choices=GENDER)
    level = models.CharField("Nivel", max_length=20, choices=LEVEL, default="principiante")
    active = models.BooleanField("Activo", default=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name = "Jugador"
        verbose_name_plural = "Jugadores"

    def __str__(self) -> str:
        return f"{self.last_name}, {self.first_name}"


class Team(models.Model):
    player1 = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="team_as_p1")
    player2 = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="team_as_p2")
    name = models.CharField("Nombre de dupla", max_length=120, blank=True)

    class Meta:
        verbose_name = "Dupla"
        verbose_name_plural = "Duplas"

    def __str__(self) -> str:
        base = f"{self.player1} / {self.player2}"
        return f"{self.name} ({base})" if self.name else base

    @property
    def display_name(self) -> str:
        """Nombre para cuadros y listados cortos: la dupla si tiene nombre; si no, los apellidos."""
        if self.name:
            return self.name
        return f"{self.player1.last_name} / {self.player2.last_name}"

    @property
    def player_ids(self) -> set[int]:
        return {pid for pid in (self.player1_id, self.player2_id) if pid}

    def clean(self):
        if self.player1_id and self.player2_id and self.player1_id == self.player2_id:
            raise ValidationError("Una dupla no puede tener el mismo jugador dos veces.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Tournament(models.Model):
    CATEGORY = [
        ("principiante", "Principiante"),
        ("octava", "Octava"),
        ("septima", "Séptima"),
        ("sexta", "Sexta"),
        ("quinta", "Quinta"),
        ("cuarta", "Cuarta"),
        ("tercera", "Tercera"),
        ("segunda", "Segunda"),
        ("primera", "Primera"),
    ]
    STATUS = [("draft", "Borrador"), ("open", "Inscripciones"), ("running", "En curso"), ("finished", "Finalizado")]
    FORMAT = [
        ("zonas_playoffs", "Zonas + eliminación directa"),
        ("round_robin", "Solo round robin (sin llaves)"),
    ]

    complex = models.ForeignKey(
        Complex,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tournaments",
        verbose_name="Complejo",
    )
    name = models.CharField("Nombre", max_length=120)
    slug = models.SlugField(unique=True)
    category = models.CharField("Categoría", max_length=20, choices=CATEGORY, default="principiante")
    start_date = models.DateField("Inicio")
    end_date = models.DateField("Fin", null=True, blank=True)
    format_type = models.CharField("Tipo de torneo", max_length=20, choices=FORMAT, default="zonas_playoffs")
    best_of_sets = models.PositiveSmallIntegerField("Sets al mejor de", default=2, help_text="2 = 2 sets + super si van 1-1; 3 = tercer set o super si van 1-1")
    teams_per_zone = models.PositiveSmallIntegerField("Duplas por zona", default=3)
    qualifiers_per_zone = models.PositiveSmallIntegerField("Clasificados por zona", default=2)
    status = models.CharField("Estado", max_length=12, choices=STATUS, default="draft")
    manual_bracket = models.BooleanField("Cruces armados a mano", default=False)
    registration_code = models.CharField("Código de autoinscripción", max_length=12, blank=True, default="")
    self_registration_enabled = models.BooleanField("Autoinscripción abierta", default=False)
    courts = models.ManyToManyField(Court, blank=True, verbose_name="Canchas")

    class Meta:
        ordering = ["-start_date"]
        verbose_name = "Torneo"
        verbose_name_plural = "Torneos"

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if self.best_of_sets not in (1, 2, 3, 5):
            raise ValidationError({"best_of_sets": "El formato de sets debe ser al mejor de 1, 2, 3 o 5."})
        if self.teams_per_zone < 2:
            raise ValidationError({"teams_per_zone": "Cada zona necesita al menos 2 duplas."})
        if self.qualifiers_per_zone < 1:
            raise ValidationError({"qualifiers_per_zone": "Debe clasificar al menos una dupla por zona."})
        if self.qualifiers_per_zone > self.teams_per_zone:
            raise ValidationError({"qualifiers_per_zone": "No pueden clasificar más duplas que las que juegan la zona."})

    @property
    def playoffs_enabled(self) -> bool:
        return self.format_type == "zonas_playoffs"


class Zone(models.Model):
    """Zona (grupo) de un torneo: round robin entre sus duplas."""

    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="zones")
    name = models.CharField("Nombre de zona", max_length=40)
    order = models.PositiveSmallIntegerField("Orden", default=0)
    teams = models.ManyToManyField(Team, through="ZoneTeam", related_name="zones")

    class Meta:
        ordering = ["tournament", "order", "name"]
        verbose_name = "Zona"
        verbose_name_plural = "Zonas"
        unique_together = ("tournament", "name")

    def __str__(self) -> str:
        return f"{self.tournament.name} – Zona {self.name}"


class ZoneTeam(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE)
    team = models.ForeignKey(Team, on_delete=models.PROTECT)
    seed = models.PositiveSmallIntegerField("Seed", null=True, blank=True)

    class Meta:
        unique_together = ("zone", "team")
        ordering = ["seed", "id"]
        verbose_name = "Dupla en zona"
        verbose_name_plural = "Duplas en zona"

    def __str__(self) -> str:
        return f"{self.team} ({self.zone})"

    def clean(self):
        if self.zone_id is None or self.team is None:
            return
        if self.team.player1_id == self.team.player2_id:
            raise ValidationError("La dupla no puede tener el mismo jugador dos veces.")
        tournament = self.zone.tournament
        other_assignments = ZoneTeam.objects.filter(zone__tournament=tournament).exclude(pk=self.pk).exclude(team=self.team)
        players = self.team.player_ids
        for assignment in other_assignments.select_related("team"):
            if players & assignment.team.player_ids:
                raise ValidationError("Un jugador de esta dupla ya está asignado a otra dupla del torneo.")
        duplicate = ZoneTeam.objects.filter(team=self.team, zone__tournament=tournament).exclude(pk=self.pk).exists()
        if duplicate:
            raise ValidationError("La dupla ya está asignada a una zona de este torneo.")

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            result = super().save(*args, **kwargs)
            self._skip_players_sync = False
            from core.services.players import sync_tournament_players

            sync_tournament_players(self.zone.tournament)
            return result

    def delete(self, *args, **kwargs):
        tournament = self.zone.tournament
        with transaction.atomic():
            result = super().delete(*args, **kwargs)
            from core.services.players import sync_tournament_players

            sync_tournament_players(tournament)
            return result


class Registration(models.Model):
    STATUS = [
        ("pending", "Pendiente"),
        ("paid", "Pagado"),
        ("confirmed", "Confirmado"),
        ("cancelled", "Cancelado"),
    ]
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="registrations")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="registrations")
    status = models.CharField("Estado", max_length=12, choices=STATUS, default="pending")
    source = models.CharField(
        "Origen",
        max_length=12,
        choices=[("panel", "Panel"), ("public", "Autoinscripción")],
        default="panel",
    )
    paid_at = models.DateTimeField("Pagado el", null=True, blank=True)
    created_at = models.DateTimeField("Inscripto el", auto_now_add=True)

    class Meta:
        unique_together = ("tournament", "team")
        verbose_name = "Inscripción"
        verbose_name_plural = "Inscripciones"

    def __str__(self) -> str:
        return f"{self.team} – {self.tournament}"

    def clean(self):
        if not self.tournament_id or not self.team_id:
            return
        if self.status == "cancelled":
            return
        players = self.team.player_ids
        other_entries = TournamentPlayer.objects.filter(tournament=self.tournament).exclude(team=self.team)
        for entry in other_entries.select_related("team"):
            if entry.player_id in players:
                raise ValidationError("Un jugador de esta dupla ya está inscripto en el torneo con otra dupla.")

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            result = super().save(*args, **kwargs)
            from core.services.players import sync_tournament_players

            sync_tournament_players(self.tournament)
            return result

    def delete(self, *args, **kwargs):
        tournament = self.tournament
        with transaction.atomic():
            result = super().delete(*args, **kwargs)
            from core.services.players import sync_tournament_players

            sync_tournament_players(tournament)
            return result


class TournamentPlayer(models.Model):
    """Registro único (torneo, jugador). Es la garantía de base de datos
    de que un jugador no integra dos duplas del mismo torneo."""

    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="tournament_players")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="tournament_entries")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="tournament_entries")
    created_at = models.DateTimeField("Creado", auto_now_add=True)

    class Meta:
        unique_together = ("tournament", "player")
        indexes = [models.Index(fields=["tournament", "player"])]
        verbose_name = "Jugador en torneo"
        verbose_name_plural = "Jugadores en torneo"

    def __str__(self) -> str:
        return f"{self.player} · {self.tournament}"


class TieBreakAudit(models.Model):
    """Auditoría append-only de todo sorteo de desempate aleatorio."""

    SCOPE_CHOICES = [
        ("zone_standings", "Desempate dentro de zona"),
        ("cross_zone_position", "Comparación entre zonas"),
        ("playoff_seed", "Seed de llave"),
        ("zone_draw", "Sorteo de zonas"),
    ]

    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="tie_break_audits")
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, null=True, blank=True, related_name="tie_break_audits")
    scope_type = models.CharField("Tipo", max_length=24, choices=SCOPE_CHOICES)
    scope_key = models.CharField("Clave de alcance", max_length=300, db_index=True)
    input_signature = models.CharField("Firma de resultados", max_length=128, db_index=True, blank=True, default="")
    tiebreaker_seed = models.BigIntegerField("Seed aleatorio reproducible")
    participants_ordered = models.JSONField("Participantes antes del sorteo", default=list)
    result_ordered = models.JSONField("Resultado del sorteo", default=list)
    criteria_summary = models.JSONField("Resumen de criterios", default=dict, blank=True)
    triggered_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="tie_break_audits")
    algorithm = models.CharField("Algoritmo", max_length=80, default="points-set_diff-game_diff-seeded-draw")
    algorithm_version = models.PositiveSmallIntegerField("Versión de algoritmo", default=1)
    created_at = models.DateTimeField("Creado", auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Auditoría de desempate"
        verbose_name_plural = "Auditorías de desempate"

    def __str__(self) -> str:
        return f"{self.scope_type} · {self.scope_key} · seed {self.tiebreaker_seed}"

    def delete(self, *args, **kwargs):
        raise ValidationError("La auditoría de sorteos es append-only: no puede eliminarse.")

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("La auditoría de sorteos es append-only: no puede modificarse.")
        return super().save(*args, **kwargs)


class Match(models.Model):
    STAGE = [
        ("group", "Zona (round robin)"),
        ("round_of_64", "Treintaidosavos"),
        ("round_of_32", "Dieciseisavos"),
        ("round_of_16", "Octavos"),
        ("quarter", "Cuartos"),
        ("semi", "Semifinal"),
        ("final", "Final"),
    ]
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="matches")
    stage = models.CharField("Etapa", max_length=20, choices=STAGE, default="group")
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, null=True, blank=True, related_name="matches")
    round_number = models.PositiveSmallIntegerField("Ronda", default=1)
    slot = models.PositiveSmallIntegerField("Posición", default=1)
    bracket_node = models.CharField("Nodo de llave", max_length=120, blank=True, default="")
    next_match = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feeders",
        verbose_name="Partido siguiente",
    )
    next_slot = models.PositiveSmallIntegerField("Posición en el partido siguiente", null=True, blank=True)
    is_bye = models.BooleanField("Descanso (bye)", default=False)
    court = models.ForeignKey(Court, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Cancha")
    team1 = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="m_as_t1", null=True, blank=True)
    team2 = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="m_as_t2", null=True, blank=True)
    winner = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="wins")
    is_walkover = models.BooleanField("Walkover", default=False)
    walkover_reason = models.TextField("Motivo del walkover", blank=True)
    score = models.CharField(
        "Resultado",
        max_length=200,
        blank=True,
        help_text='Ej: "6-4 4-6 11-9" (super tie-break a 11, con o sin corchetes)',
    )
    decided_by_super = models.BooleanField("Decidido por super tie-break", default=False)
    scheduled_at = models.DateTimeField("Programado", null=True, blank=True)
    played_at = models.DateTimeField("Jugado", null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_matches",
        verbose_name="Cargado por",
    )
    recorded_at = models.DateTimeField("Cargado el", null=True, blank=True)

    class Meta:
        ordering = ["round_number", "slot", "id"]
        verbose_name = "Partido"
        verbose_name_plural = "Partidos"
        constraints = [
            models.UniqueConstraint(
                "zone",
                Least("team1_id", "team2_id"),
                Greatest("team1_id", "team2_id"),
                condition=Q(stage="group", team1__isnull=False, team2__isnull=False, zone__isnull=False),
                name="uniq_group_pair_per_zone",
            ),
            models.UniqueConstraint(
                "next_match",
                "next_slot",
                condition=Q(next_match__isnull=False),
                name="uniq_bracket_feeder_slot",
            ),
        ]

    def __str__(self) -> str:
        if self.is_bye:
            return f"Bye · {self.team1 or self.team2}"
        return f"{self.team1 or '?'} vs {self.team2 or '?'}"

    @property
    def display_stage(self) -> str:
        return dict(self.STAGE).get(self.stage, self.stage)

    @property
    def has_real_data(self) -> bool:
        """Datos deportivos reales (los byes son estructura, no datos)."""
        if self.is_bye:
            return False
        return (
            bool((self.score or "").strip())
            or self.is_walkover
            or bool((self.walkover_reason or "").strip())
            or self.winner_id is not None
        )

    @property
    def is_decided(self) -> bool:
        return self.winner_id is not None

    @property
    def status_label(self) -> str:
        if self.is_bye:
            return "Descanso"
        if self.is_walkover:
            return "Walkover"
        if self.winner_id is not None and (self.score or "").strip():
            return "Finalizado"
        return "Pendiente"

    def derive_winner_from_score(self):
        """Determina el ganador a partir de un marcador válido (sin super si hay empate de sets)."""
        if self.is_walkover or not (self.score or "").strip() or not self.team1_id or not self.team2_id:
            return self.winner
        winner_id = implied_winner_id(self.score, self.team1_id, self.team2_id)
        if winner_id is not None:
            self.winner_id = winner_id
        return self.winner

    def get_constraints(self):
        """Omite la validación Python de la restricción de pares de zona.

        Esa restricción usa expresiones (LEAST/GREATEST) y su validación en
        Python produce falsos positivos sin conexión a los datos reales; la
        garantía efectiva es el índice único parcial de PostgreSQL y la
        comprobación explícita de ``clean()``.
        """
        constraints = super().get_constraints()
        return [
            (model_class, [c for c in model_constraints if getattr(c, "name", None) != "uniq_group_pair_per_zone"])
            for model_class, model_constraints in constraints
        ]

    def clean(self):
        from core.services.draws import parse_sets  # noqa: F401  (se importa para consistencia futura)

        if self.team1_id is not None and self.team2_id is not None and self.team1_id == self.team2_id:
            raise ValidationError("Los participantes del partido deben ser distintos.")

        if self.next_slot is not None and self.next_match_id is None:
            raise ValidationError("La posición en el partido siguiente requiere un partido siguiente.")
        if self.next_match_id is not None:
            if self.next_match is None:
                raise ValidationError("El partido siguiente indicado no existe.")
            if self.next_match.tournament_id != self.tournament_id:
                raise ValidationError("El partido siguiente debe pertenecer al mismo torneo.")
            if self.next_slot not in (1, 2):
                raise ValidationError("La posición en el partido siguiente debe ser 1 o 2.")
            if PLAYOFF_STAGE_ORDER.get(self.next_match.stage, 0) <= PLAYOFF_STAGE_ORDER.get(self.stage, 0):
                raise ValidationError("El partido siguiente debe ser de una etapa posterior.")

        if self.is_bye:
            if self.stage == "group":
                raise ValidationError("Un descanso (bye) no puede estar en la fase de zonas.")
            present = [pid for pid in (self.team1_id, self.team2_id) if pid is not None]
            if len(present) != 1:
                raise ValidationError("Un descanso debe tener exactamente una dupla presente.")
            if self.winner_id != present[0]:
                raise ValidationError("La dupla con descanso debe ser la ganadora directa.")
            if (self.score or "").strip():
                raise ValidationError("Un descanso no tiene marcador.")
            if self.is_walkover or (self.walkover_reason or "").strip():
                raise ValidationError("Un descanso no es un walkover.")
            self.decided_by_super = False
            return

        if self.stage == "group":
            if self.zone_id is not None:
                if self.tournament_id is not None and self.zone.tournament_id != self.tournament_id:
                    raise ValidationError("La zona no pertenece al torneo indicado.")
                allowed_teams = set(self.zone.teams.values_list("pk", flat=True))
                if self.team1_id is not None and self.team1_id not in allowed_teams:
                    raise ValidationError("El primer equipo no está asignado a esta zona.")
                if self.team2_id is not None and self.team2_id not in allowed_teams:
                    raise ValidationError("El segundo equipo no está asignado a esta zona.")
                if self.team1_id and self.team2_id:
                    duplicate = (
                        Match.objects.filter(tournament_id=self.tournament_id, stage="group", zone_id=self.zone_id)
                        .exclude(pk=self.pk)
                        .filter(
                            Q(team1_id=self.team1_id, team2_id=self.team2_id)
                            | Q(team1_id=self.team2_id, team2_id=self.team1_id)
                        )
                    )
                    if duplicate.exists():
                        raise ValidationError("Ya existe un partido entre estas duplas en esta zona.")
        elif self.zone_id is not None:
            raise ValidationError("Los partidos eliminatorios no se asignan a una zona.")

        reason = (self.walkover_reason or "").strip()
        score = (self.score or "").strip()

        if self.is_walkover:
            if not reason:
                raise ValidationError("El walkover requiere un motivo obligatorio.")
            if score:
                raise ValidationError("Un walkover no puede tener marcador ni sets o games ficticios.")
            if self.team1_id is None or self.team2_id is None:
                raise ValidationError("El partido no tiene ambos participantes confirmados.")
            if self.winner_id is None:
                raise ValidationError("El walkover requiere un ganador obligatorio.")
            if self.winner_id not in {self.team1_id, self.team2_id}:
                raise ValidationError("El ganador del walkover debe ser uno de los participantes.")
            if self.decided_by_super:
                raise ValidationError("Un walkover no se decide por super tie-break.")
            return

        if reason:
            self.walkover_reason = ""
        if not score:
            if self.winner_id is not None:
                raise ValidationError("Debe indicarse el resultado cuando hay ganador.")
            self.decided_by_super = False
            return

        if self.team1_id is None or self.team2_id is None:
            raise ValidationError("El partido no tiene ambos participantes confirmados.")

        best_of_sets = getattr(self.tournament, "best_of_sets", 2) if self.tournament_id else 2
        if not isinstance(best_of_sets, int) or best_of_sets < 1:
            raise ValidationError("Cantidad de sets inválida para el torneo.")

        parsed_sets, super_scores, unknown_tokens = classify_score(score)

        if unknown_tokens:
            if any(token.startswith("[") for token in unknown_tokens):
                raise ValidationError("Super tie-break inválido; usá por ejemplo 11-9.")
            raise ValidationError("Formato de set inválido. Usá por ejemplo 6-4.")

        minimum_sets = best_of_sets // 2 + 1
        if not minimum_sets <= len(parsed_sets) <= best_of_sets:
            raise ValidationError(
                "La cantidad de sets jugados no es válida para un torneo "
                f"al mejor de {best_of_sets} sets."
            )
        if len(super_scores) > 1:
            raise ValidationError("El super tie-break se indica una sola vez.")

        sets = []
        for games_a, games_b in parsed_sets:
            if games_a == games_b:
                raise ValidationError("Un set no puede terminar empatado.")
            if not _valid_set_score(games_a, games_b):
                raise ValidationError(
                    "Marcador de set imposible. Se admite 6-0 a 6-4, 7-5 y 7-6."
                )
            sets.append((games_a, games_b))

        team1_sets = sum(a > b for a, b in sets)
        team2_sets = sum(b > a for a, b in sets)

        super_tiebreak = None
        if super_scores:
            super_tiebreak = super_scores[0]
            if super_tiebreak[0] == super_tiebreak[1]:
                raise ValidationError("El super tie-break no puede terminar empatado.")
            hi, lo = max(super_tiebreak), min(super_tiebreak)
            if hi < 11 or hi - lo < 2:
                raise ValidationError(
                    "Super tie-break imposible: se juega a 11 puntos con diferencia mínima de 2."
                )

        defined_by_super = team1_sets == 1 and team2_sets == 1

        if defined_by_super:
            if super_tiebreak is None:
                if best_of_sets >= 3:
                    raise ValidationError(
                        "Hay empate de sets: cargá el tercer set o el super tie-break (por ejemplo 11-9)."
                    )
                raise ValidationError("Hay empate de sets; falta el super tie-break, por ejemplo 11-9.")
            expected_winner_id = self.team1_id if super_tiebreak[0] > super_tiebreak[1] else self.team2_id
        elif team1_sets == team2_sets:
            raise ValidationError("El partido no tiene ganador de sets.")
        else:
            if super_tiebreak is not None:
                raise ValidationError("El super tie-break solo se usa cuando cada pareja ganó un set.")
            expected_winner_id = self.team1_id if team1_sets > team2_sets else self.team2_id

        if self.winner_id is None:
            raise ValidationError("Debe indicarse el ganador del partido.")
        if self.winner_id not in {self.team1_id, self.team2_id}:
            raise ValidationError("El ganador debe ser uno de los participantes.")
        if self.winner_id != expected_winner_id:
            raise ValidationError("El ganador no coincide con el resultado cargado.")

        self.decided_by_super = defined_by_super

    def save(self, *args, **kwargs):
        auto_progress = not getattr(self, "_skip_auto_progress", False)

        if self.is_bye:
            self.winner = self.team1 or self.team2
            self.score = ""
            self.is_walkover = False
            self.walkover_reason = ""
        elif not self.is_walkover and self.winner_id is None and (self.score or "").strip() and self.team1_id and self.team2_id:
            self.derive_winner_from_score()

        if self.is_bye or self.is_walkover or self.score or self.winner_id is not None:
            self.full_clean()

        if self.pk is not None:
            try:
                old = Match.objects.get(pk=self.pk)
            except Match.DoesNotExist:
                old = None
            changed = old is not None and (
                old.score != self.score
                or old.winner_id != self.winner_id
                or old.is_walkover != self.is_walkover
                or old.walkover_reason != self.walkover_reason
                or old.team1_id != self.team1_id
                or old.team2_id != self.team2_id
                or old.is_bye != self.is_bye
            )
            if changed:
                from core.services.tournament_workflow import can_update_match

                if not can_update_match(self, {}):
                    raise ValidationError(
                        "La corrección está bloqueada por dependencias con resultado o datos parciales/inconsistentes."
                    )

        with transaction.atomic():
            result = super().save(*args, **kwargs)
            if auto_progress and (self.winner_id is not None or self.is_bye):
                from core.services.progression import progress_after_match_save

                progress_after_match_save(self.pk)
        return result
