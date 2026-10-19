from django.contrib import admin

from .models import (
    Complex,
    Court,
    CourtBooking,
    CourtDateOverride,
    CourtSchedule,
    Invitation,
    Match,
    Player,
    Registration,
    RegistrationRequest,
    SchedulePause,
    Team,
    TemporaryPassword,
    TieBreakAudit,
    Tournament,
    TournamentPlayer,
    Zone,
    ZoneTeam,
)


class ComplexScopedAdminMixin:
    """Filtra el admin por los complejos autorizados del usuario.

    - ``complex_lookup``: ruta ORM desde el modelo hasta el complejo.
    - Superusuario: ve todo.
    - Torneos heredados sin complejo: visibles para staff.
    """

    complex_lookup = "complex"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        complex_ids = set(request.user.complexes.values_list("id", flat=True))
        if not complex_ids:
            return qs.none()
        lookup = f"{self.complex_lookup}__in"
        return qs.filter(**{lookup: complex_ids})

    def has_view_permission(self, request, obj=None):
        if not super().has_view_permission(request, obj):
            return False
        if obj is None or request.user.is_superuser:
            return True
        obj_complex = getattr(obj, "complex", None)
        if obj_complex is None:
            return bool(request.user.is_staff)
        return request.user.complexes.filter(pk=obj_complex.pk).exists()

    def has_change_permission(self, request, obj=None):
        if obj is not None and not self.has_view_permission(request, obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and not self.has_view_permission(request, obj):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Complex)
class ComplexAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "is_active", "created_at")
    list_filter = ("is_active", "city")
    search_fields = ("name", "slug", "city")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("users",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(pk__in=request.user.complexes.values_list("id", flat=True))


@admin.register(Court)
class CourtAdmin(ComplexScopedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "complex", "surface", "is_active")
    list_filter = ("surface", "is_active", "complex")
    search_fields = ("name",)


class SchedulePauseInline(admin.TabularInline):
    model = SchedulePause
    extra = 0


@admin.register(CourtSchedule)
class CourtScheduleAdmin(ComplexScopedAdminMixin, admin.ModelAdmin):
    list_display = ("complex", "day_type", "is_open", "start_time", "end_time", "slot_minutes")
    list_filter = ("day_type", "is_open", "complex")
    inlines = [SchedulePauseInline]


@admin.register(CourtDateOverride)
class CourtDateOverrideAdmin(ComplexScopedAdminMixin, admin.ModelAdmin):
    list_display = ("complex", "date", "is_closed", "start_time", "end_time", "note")
    list_filter = ("is_closed", "complex")
    ordering = ("-date",)


@admin.register(CourtBooking)
class CourtBookingAdmin(ComplexScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "date",
        "start_time",
        "end_time",
        "court",
        "complex",
        "status",
        "customer_name",
        "customer_phone",
        "source",
    )
    list_filter = ("status", "source", "complex", "court", "date")
    search_fields = ("customer_name", "customer_phone", "code")
    readonly_fields = ("code", "created_at", "cancelled_at")
    date_hierarchy = "date"


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "complex", "created_by", "created_at", "used_at", "used_by")
    list_filter = ("used_at", "complex")
    search_fields = ("email",)
    readonly_fields = ("created_at", "used_at", "used_by")


@admin.register(RegistrationRequest)
class RegistrationRequestAdmin(admin.ModelAdmin):
    list_display = ("email", "first_name", "last_name", "phone", "complex_name", "created_at")
    search_fields = ("email", "first_name", "last_name", "complex_name")


@admin.register(TemporaryPassword)
class TemporaryPasswordAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at")
    search_fields = ("user__username",)


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("last_name", "first_name", "gender", "level", "active", "phone")
    list_filter = ("gender", "level", "active")
    search_fields = ("first_name", "last_name", "phone", "email")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("__str__", "player1", "player2", "name")
    search_fields = ("name", "player1__last_name", "player2__last_name")
    autocomplete_fields = ("player1", "player2")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in {"player1", "player2"}:
            from .services.players import available_players, player_ids_in_tournament

            tournament_id = request.session.get("padel_tournament_id")
            object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            team = Team.objects.filter(pk=object_id).first() if object_id else None
            if tournament_id:
                tournament = Tournament.objects.filter(pk=tournament_id).first()
                if tournament is not None:
                    kwargs["queryset"] = available_players(tournament, exclude_team=team)
            else:
                used_ids = set()
                for values in Team.objects.values_list("player1_id", "player2_id"):
                    used_ids.update(v for v in values if v)
                if team is not None:
                    used_ids -= team.player_ids
                kwargs["queryset"] = Player.objects.filter(active=True).exclude(pk__in=used_ids)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class RegistrationInline(admin.TabularInline):
    model = Registration
    extra = 1
    autocomplete_fields = ("team",)


class TournamentPlayerInline(admin.TabularInline):
    model = TournamentPlayer
    extra = 0
    readonly_fields = ("player", "team", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Tournament)
class TournamentAdmin(ComplexScopedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "complex", "category", "format_type", "status", "start_date", "end_date")
    list_filter = ("status", "category", "format_type", "complex")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [RegistrationInline, TournamentPlayerInline]
    filter_horizontal = ("courts",)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "courts" and not request.user.is_superuser:
            kwargs["queryset"] = Court.objects.filter(
                complex__in=request.user.complexes.values_list("id", flat=True)
            )
        return super().formfield_for_manytomany(db_field, request, **kwargs)


class ZoneTeamInline(admin.TabularInline):
    model = ZoneTeam
    extra = 1
    autocomplete_fields = ("team",)


class ZoneScopedAdmin(ComplexScopedAdminMixin, admin.ModelAdmin):
    complex_lookup = "tournament__complex"


@admin.register(Zone)
class ZoneAdmin(ZoneScopedAdmin):
    list_display = ("__str__", "tournament", "order")
    list_filter = ("tournament",)
    search_fields = ("name", "tournament__name")
    inlines = [ZoneTeamInline]


@admin.register(Registration)
class RegistrationAdmin(ZoneScopedAdmin):
    list_display = ("tournament", "team", "status", "paid_at", "created_at")
    list_filter = ("status", "tournament")
    autocomplete_fields = ("team", "tournament")
    search_fields = ("team__player1__last_name", "team__player2__last_name")


@admin.register(TournamentPlayer)
class TournamentPlayerAdmin(ZoneScopedAdmin):
    list_display = ("tournament", "player", "team", "created_at")
    list_filter = ("tournament",)
    search_fields = ("player__last_name", "team__name")
    readonly_fields = ("tournament", "player", "team", "created_at")


@admin.register(Match)
class MatchAdmin(ZoneScopedAdmin):
    list_display = (
        "tournament",
        "stage",
        "zone",
        "round_number",
        "slot",
        "team1",
        "team2",
        "winner",
        "score",
        "is_bye",
        "scheduled_at",
    )
    list_filter = ("tournament", "stage", "zone", "round_number", "is_bye")
    autocomplete_fields = ("team1", "team2", "winner", "court", "zone")
    date_hierarchy = "scheduled_at"
    search_fields = ("score",)
    readonly_fields = ("recorded_by", "recorded_at")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("tournament", "zone", "team1", "team2", "winner", "court", "next_match")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in {"team1", "team2"}:
            zone_id = request.GET.get("zone") or request.POST.get("zone")
            zone = None
            object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            if object_id:
                zone = Match.objects.filter(pk=object_id).values_list("zone_id", flat=True).first()
                zone = Zone.objects.filter(pk=zone).first() if zone else None
            if zone is None and zone_id:
                zone = Zone.objects.filter(pk=zone_id).first()
            if zone is not None:
                kwargs["queryset"] = zone.teams.all()
            else:
                kwargs["queryset"] = Team.objects.all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    class Media:
        js = ("core/match_team_filter.js",)

    def save_model(self, request, obj, form, change):
        from .services.tournament_workflow import register_match_result

        if change:
            # Las correcciones y resultados pasan por el workflow transaccional.
            register_match_result(
                obj,
                score=obj.score,
                winner=obj.winner,
                is_walkover=obj.is_walkover,
                walkover_reason=obj.walkover_reason,
                court=obj.court,
                played_at=obj.played_at,
                actor=request.user,
            )
        else:
            obj.save()


@admin.register(TieBreakAudit)
class TieBreakAuditAdmin(admin.ModelAdmin):
    list_display = ("scope_type", "scope_key", "tournament", "zone", "tiebreaker_seed", "created_at")
    list_filter = ("scope_type", "tournament", "created_at")
    readonly_fields = ("tiebreaker_seed", "participants_ordered", "result_ordered", "criteria_summary", "created_at")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
