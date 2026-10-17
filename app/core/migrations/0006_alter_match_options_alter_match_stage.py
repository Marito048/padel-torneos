# Generado a mano: etapas de llave para 16avos/8avos y orden por ronda.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_match_recorded_at_match_recorded_by"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="match",
            options={
                "ordering": ["round_number", "slot", "id"],
                "verbose_name": "Partido",
                "verbose_name_plural": "Partidos",
            },
        ),
        migrations.AlterField(
            model_name="match",
            name="stage",
            field=models.CharField(
                choices=[
                    ("group", "Zona (round robin)"),
                    ("round_of_64", "Treintaidosavos"),
                    ("round_of_32", "Dieciseisavos"),
                    ("round_of_16", "Octavos"),
                    ("quarter", "Cuartos"),
                    ("semi", "Semifinal"),
                    ("final", "Final"),
                ],
                default="group",
                max_length=20,
                verbose_name="Etapa",
            ),
        ),
    ]
