from django.core.management.base import BaseCommand, CommandError

from core.models import Tournament
from core.services.draws import generate_playoff_bracket, advance_stage


class Command(BaseCommand):
    help = "Genera playoffs (primera ronda o siguiente etapa)."

    def add_arguments(self, parser):
        parser.add_argument("--tournament", required=True, help="Slug del torneo.")
        parser.add_argument("--advance-from", help="Etapa desde la cual avanzar (ej: group no; usar quarter).")

    def handle(self, *args, **options):
        try:
            tournament = Tournament.objects.get(slug=options["tournament"])
        except Tournament.DoesNotExist as exc:
            raise CommandError(f"Torneo no encontrado: {options['tournament']}") from exc

        if options["advance_from"]:
            ms = advance_stage(tournament, options["advance_from"])
            self.stdout.write(self.style.SUCCESS(f"Creados {len(ms)} partidos de siguiente etapa."))
        else:
            ms = generate_playoff_bracket(tournament)
            self.stdout.write(self.style.SUCCESS(f"Creados {len(ms)} partidos de playoffs."))
