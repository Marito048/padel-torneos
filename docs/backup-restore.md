# Backup y restauración de PostgreSQL

Fecha de la prueba: 2026-10-21. Entorno: PostgreSQL 16.15 descartable
(`padel-pg16-test`, `127.0.0.1:5434`). No se usó la base de producción.

## Procedimiento probado

Se creó una base fuente `padel_backup_source`, se aplicaron todas las
migraciones y se cargaron datos de dominio (complejo, cancha, horario, torneo,
jugadores, dupla, turno, dos movimientos de caja, cierre de caja y solicitud de
registro). La prueba se repitió al cerrar la auditoría con las bases
`padel_pilot_source` y `padel_pilot_restore`, sin cambios de esquema. Luego:

```bash
# 1. Copia lógica comprimida y legible por pg_restore
docker exec padel-pg16-test pg_dump -U postgres -Fc \
  --no-owner --no-privileges padel_backup_source > /tmp/opencode/padel_backup_source.dump

# 2. Comprobar que no está vacío y que el archivo se puede leer
stat -c%s /tmp/opencode/padel_backup_source.dump
docker exec -i padel-pg16-test pg_restore -l \
  < /tmp/opencode/padel_backup_source.dump > /tmp/opencode/padel_backup_source.toc
sha256sum /tmp/opencode/padel_backup_source.dump

# 3. Restaurar en una base NUEVA, nunca sobre la fuente
docker exec padel-pg16-test createdb -U postgres padel_restore_target
docker exec -i padel-pg16-test pg_restore -U postgres \
  --no-owner --no-privileges -d padel_restore_target \
  < /tmp/opencode/padel_backup_source.dump
```

## Resultado obtenido

- Dump: 148.723 bytes; TOC legible con 385 líneas; `pg_restore` terminó sin error.
- Base restaurada: 39 tablas, 59 claves foráneas, 74 restricciones únicas,
  157 índices y 56 filas en `django_migrations` (21 de la app `core`).
- Conteos de dominio idénticos entre origen y destino:
  `auth_user=1`, `core_complex=1`, `core_court=1`, `core_courtbooking=1`,
  `core_courtpayment=2`, `core_cashclose=1`, `core_courtschedule=1`,
  `core_tournament=1`, `core_player=2`, `core_team=1`,
  `core_registrationrequest=1`.
- `pg_dump --schema-only` origen/destino: única diferencia, el token aleatorio
  `\restrict` y el formato textual del predicado del índice parcial; el esquema
  es equivalente.
- Integridad: 0 pagos huérfanos, 0 importes no positivos, 0 señas mayores que el
  precio, 0 duplas huérfanas y 0 migraciones de `core` sin registrar.
- Controles Django sobre la base restaurada: `manage.py check` sin errores y
  `manage.py migrate --check` sin migraciones pendientes.
- Los campos cifrados se descifraron correctamente después de restaurar usando
  el mismo `SECRET_KEY` (token y secreto de webhook de Mercado Pago).

## Limitaciones y riesgos

- Es una copia **lógica**, no un respaldo continuo. No hay WAL archivado ni
  recuperación a un punto en el tiempo (PITR); la pérdida máxima depende de la
  frecuencia con que se ejecute el backup.
- La prueba fue de 149 KB y no mide tiempos de una base grande. No se probó una
  restauración en otra versión mayor de PostgreSQL ni en otro servidor.
- No se probó una copia fuera del equipo ni un almacenamiento inmutable. Un
  backup en el mismo disco no protege de pérdida del servidor.
- El dump contiene hashes de contraseñas y credenciales cifradas de Mercado Pago:
  debe guardarse con permisos restrictivos, cifrado en reposo y con acceso
  auditado. No debe subirse al repositorio.
- El README documenta respaldos históricos de octubre, pero no hay una tarea
  programada verificada en este repositorio. Antes del piloto hay que definir
  frecuencia, retención, copia externa y una prueba periódica de restauración.

## Verificación posterior a una reparación

Después de cualquier conciliación manual:

1. Guardar el dump y su `sha256` antes de tocar datos.
2. Exportar `python manage.py audit_cash --json > antes.json`.
3. Reparar sólo mediante movimientos nuevos o actualización explícita de
   banderas derivadas, nunca editando o borrando `CourtPayment`.
4. Exportar `despues.json`, comparar los códigos afectados y los totales.
5. Restaurar el dump en una base nueva y repetir `check`, `migrate --check` y
   las consultas de integridad de este documento.
