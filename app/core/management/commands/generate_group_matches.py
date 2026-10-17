from django.core.management.base import BaseCommand, CommandError
from django.shortcuts import get_object_or_404

from core.models import Tournament
from core.services.draws import generate_group_matches


class Command(BaseCommand):
    help = "Genera los partidos de zonas (round robin) de un torneo."

    def add_arguments(self, parser):
        parser.add_argument("--tournament", required=True, help="Slug del torneo.")

    def handle(self, *args, **options):
        try:
            tournament = Tournament.objects.get(slug=options["tournament"])
        except Tournament.DoesNotExist as exc:
            raise CommandError(f"Torneo no encontrado: {options['tournament']}") from exc
        matches = generate_group_matches(tournament)
        self.stdout.write(self.style.SUCCESS(f"Creados {len(matches)} partidos de zona."))
