# Cierre previo al piloto: migraciones, cobros, endpoints y backup

> **Actualización de alcance (2026-10-21):** la primera versión del piloto
> opera sin pagos online. La integración de Mercado Pago y su auditoría se
> conservan como funcionalidad diferida; este documento mantiene el registro
> técnico de esa integración. El estado vigente y los criterios del piloto
> están en `docs/preparacion-piloto.md`.

Fecha: 2026-10-21. Todo el trabajo quedó **sin commit y sin deploy**. No se
modificó la base ni los servicios de producción. Las pruebas se ejecutaron en
PostgreSQL descartable (`padel-pg16-test`, `127.0.0.1:5434`).

## 1. Migraciones

### Estado encontrado

| Entorno | Historial `core` | Observación |
| --- | --- | --- |
| LXC de producción (`192.168.0.200`) | hasta `0019_backfill_court_payments` | Consulta de solo lectura con `showmigrations`. No se migró. |
| Docker local `padel-torneos-db-1` | hasta `0007_tournament_manual_bracket` | Stack local viejo; sólo se hicieron `SELECT`. |
| Base descartable nueva | hasta `0021_complex_mp_webhook_secret_encrypted` | Migración completa y suite de pruebas. |
| Otro clon | `0020_tournament_registration_code_unique_and_more.py` | **No disponible** en este equipo, el repositorio, el LXC, git objects ni backups. |

El código actual tiene dos migraciones nuevas:

- `0020_match_version` (rama de Objetivos 3–4).
- `0021_complex_mp_webhook_secret_encrypted`, que depende de `0020_match_version`.

La otra rama agrega una `0020` independiente. **No se renombró ninguna
migración**: el nombre está registrado en `django_migrations` y renombrarlo
rompería entornos donde ya se aplicó. Tampoco se inventó el contenido del
archivo ausente.

### Por qué no alcanza con dejar dos ramas

Con dos hojas `0020` y sin unión, Django 5.2 aborta comandos como `migrate` y
`showmigrations` con:

```
CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph
```

Se reprodujo en una copia descartable en `/tmp/opencode/padel-migration-recon`
con una `0020` sintética (no es el archivo real). Por eso la estrategia correcta
es conservar los dos nombres y agregar una **migración de unión vacía** que
dependa de todas las hojas:

```python
# 0022_merge_... (generado por `makemigrations --merge`)
dependencies = [
    ("core", "0020_match_version"),
    ("core", "0020_tournament_registration_code_unique_and_more"),
    ("core", "0021_complex_mp_webhook_secret_encrypted"),
]
operations = []
```

La unión **no repara operaciones incompatibles**. Antes de crearla hay que
comparar el archivo real y verificar que las dos ramas no alteren el mismo campo,
no creen la misma restricción con nombres distintos y no dependan una del estado
de la otra. Si hay conflicto, hay que separar las operaciones en migraciones
nuevas con una estrategia explícita; no editar una migración ya aplicada.

### Escenarios validados con la rama sintética

En una copia descartable se agregó una `0020` sintética que sólo hacía
`AlterField` de `Tournament.registration_code` y una `0022` de unión. Resultados:

| Escenario | Resultado |
| --- | --- |
| Sólo `0020_match_version` aplicada; después `migrate` completo | Aplicó `0021`, la otra `0020` y la unión. `core_match.version` y el secreto existen. |
| Sólo la otra `0020` aplicada; después `migrate` completo | Aplicó `0020_match_version`, `0021` y la unión. Ambos esquemas existen. |
| Base nueva con todas las ramas | Aplicó las dos `0020`, `0021` y la unión sin error. |

También se validó el estado real de producción: una base nueva migrada hasta
`0019` y luego hasta el final aplicó correctamente `0020_match_version` y
`0021_complex_mp_webhook_secret_encrypted`.

### Bloqueo real

No se puede agregar la unión definitiva al repositorio mientras falte
`0020_tournament_registration_code_unique_and_more.py`: una dependencia a un
archivo inexistente deja el grafo inválido. Hay que obtener ese archivo del otro
clon, comparar operaciones y recién ahí ejecutar `makemigrations --merge` o
escribir la unión equivalente. Antes de aplicar en producción: backup verificado,
prueba en base descartable con el historial real, y `migrate` en ventana
controlada.

## 2. Matriz de pagos y conciliación

### Regla contable adoptada

El libro `CourtPayment` es la fuente de verdad cuando hay movimientos. Las
banderas legadas (`is_paid`, `balance_paid`) siguen siendo el fallback para
turnos sin libro. Fórmulas vigentes:

- `net_paid = ingresos - devoluciones/reversiones/contracargos`
- `paid_amount = max(net_paid, 0)`
- `refunded_amount = total de egresos`
- `overpaid_amount = max(net_paid - price, 0)`
- `outstanding_amount = max(price - paid_amount, 0)` salvo turnos cancelados o
  bloqueados, que no tienen saldo pendiente.
- `deposit_pending_amount = max(deposit - paid_amount, 0)`
- `balance_amount` se calcula sólo para turnos activos, descontando lo cobrado
  por encima de la seña.

### Casos cubiertos

| Caso | Comportamiento / prueba |
| --- | --- |
| Turno sin movimientos | No crea libro; usa banderas legadas. `paid=0`, seña pendiente y saldo esperado. |
| Seña cobrada y saldo pendiente | Seña acreditada; saldo se cobra en el club. |
| Pago parcial de seña | Se registra el importe real; no confirma el turno; queda seña pendiente. |
| Devolución parcial | Baja el neto y el saldo; no borra el ingreso. |
| Devolución total | Neto 0; estado `refunded`; saldo vuelve a quedar pendiente si el turno sigue activo. |
| Pago duplicado | IDs externos distintos se acumulan; el mismo ID es idempotente y no se puede reutilizar en otro turno. |
| Notificación repetida o tardía | No duplica el movimiento; una devolución/contracargo registrado para ese mismo pago impide que un `approved` tardío confirme o reviva el turno. |
| Pago online inferior a la seña | Se registra el importe real y se marca `amount_mismatch`; no confirma el turno. |
| Pago online superior a la seña/precio | Se registra; si alcanza la seña confirma; `overpaid_amount` expone el excedente. |
| Pago online sin importe o con importe inválido | Se marca `amount_mismatch` y no se inventa un movimiento de caja. |
| Pago online en otra moneda o sin moneda | Se marca `amount_mismatch` y no se suma a la caja en pesos. |
| Pago sin `external_reference` | Se rechaza aunque el código del turno venga en la URL de retorno. |
| Turno cancelado | No se revive por un pago aprobado; el dinero queda registrado y el estado pasa a `approved_conflict` cuando corresponde. |
| Turno bloqueado | No se confirma; el dinero queda en caja y el estado se marca para revisión. |
| Reversión manual y luego devolución online | La devolución se limita al importe no revertido; no se cuenta dos veces. |
| Aviso de pago después de un contracargo | El ingreso real queda en el libro, pero el estado `charged_back` y la revisión manual se conservan. |
| Banderas históricas inconsistentes | La auditoría las informa; no se reparan automáticamente. |

Pruebas agregadas o ajustadas en `app/core/tests_objetivo3.py` y
`app/core/tests_turnos.py` para: sin movimientos, pago parcial, bloqueo,
devolución total después de parcial, importe superior, moneda distinta, falta de
referencia, pago parcial seguido de pago correcto, contracargo y auditoría.

### Conciliación de datos históricos

No se modificó ningún dato histórico. Se agregó una auditoría de **solo lectura**:

```bash
python manage.py audit_cash --json > antes.json
python manage.py audit_cash --complex <slug> --strict
```

Informa:

- `is_paid`/`balance_paid` que no coinciden con el libro.
- `balance_paid=True` con `is_paid=False` (no se puede inferir la apertura).
- Pagos aprobados sin bandera de abonado.
- Neto negativo, devoluciones mayores a ingresos o importes no positivos.
- Movimientos huérfanos y reversiones duplicadas.
- `payment_status=charged_back` sin movimiento de contracargo.

Estrategia de reparación, cuando se autorice:

1. Generar backup y restaurarlo en una base nueva (ver `docs/backup-restore.md`).
2. Exportar la auditoría antes y guardar el `sha256` del backup y del JSON.
3. Clasificar cada turno con evidencia externa (Mercado Pago, banco, caja,
   WhatsApp). No inferir un cobro sólo por una bandera contradictoria.
4. Corregir con movimientos nuevos o con una actualización explícita de banderas
   derivadas; nunca editar ni borrar filas de `CourtPayment`.
5. Volver a exportar la auditoría, comparar los códigos afectados y los totales,
   y restaurar el backup en otra base para repetir los controles.
6. Registrar para cada cambio: código de turno, valores antes/después, usuario,
   motivo, comando/SQL y fecha.

## 3. Auditoría de endpoints y webhook

### `turno_cancelar`

- **Auth/authz:** público; la autorización es el código del turno (8 caracteres
  aleatorios, unicidad en base). No hay login.
- **Entrada:** sólo `code` de la URL y POST con CSRF. Se normaliza a mayúsculas.
- **Concurrencia:** el POST toma `select_for_update` del turno, el mismo lock que
  usa el webhook. Si pago y cancelación compiten, uno se aplica y el otro ve el
  estado resultante; una cancelación explícita no se revive.
- **Idempotencia:** repetir el POST no vuelve a cancelar; informa que ya no está
  activo.
- **Auditoría:** `logger.info` con el código del turno. No hay modelo de auditoría.
- **Abuso:** límite por sesión (`PUBLIC_CODE_MAX_PER_WINDOW`, 60 consultas/
  300 s por defecto, compartido por GET/POST de este endpoint); el código tiene
  alta entropía y el POST exige CSRF. Bajo Funnel no hay IP real para limitar
  por origen, y el límite se evade rotando cookies.

### `signup_request`

- **Auth:** público; no crea usuarios automáticamente.
- **Entrada:** email, nombres, teléfono y datos del complejo; honeypot
  `website`; validación de email; límites de longitud antes de escribir.
- **Duplicados:** compara email sin distinguir mayúsculas contra usuarios y
  solicitudes; captura `IntegrityError` por carrera.
- **Abuso:** límite por sesión de `SIGNUP_REQUEST_MAX_PER_WINDOW` intentos cada
  `SIGNUP_REQUEST_WINDOW_SECONDS` (10/900 s por defecto), con `LocMemCache`.
  Es por worker y se evade rotando cookies; no es una defensa por IP.
- **Auditoría:** la solicitud queda en `RegistrationRequest`; los avisos por
  email/WhatsApp se registran en logs. No hay CAPTCHA ni verificación de teléfono.
- **Nota:** al ser una solicitud manual, la revisión del administrador sigue
  siendo la defensa principal.

### Webhook de Mercado Pago

- **Auth:** `csrf_exempt` por ser servidor-a-servidor. Se valida `x-signature`
  con HMAC-SHA256 y el manifiesto oficial
  `id:{data.id};request-id:{x-request-id};ts:{ts};` antes de llamar a la API.
  Con token de Mercado Pago, el secreto es **obligatorio** (por defecto y
  siempre en `config.settings.prod`); sin secreto el webhook responde `503` y
  no procesa la notificación. Los complejos sin token no se ven afectados.
- **Topics:** `payment`, `topic_merchant_order_wh`/`merchant_order` y
  `topic_chargebacks_wh`/`chargeback`. Otros topics se acusan con 200 sin tocar
  la caja.
- **Asociación:** se consulta el pago/orden contra la API con el token del
  complejo; no se confía en el body. Se exige `external_reference` exacto,
  `id` devuelto igual al consultado, moneda igual a `MERCADO_PAGO_CURRENCY` y,
  para pagos, importe positivo. Un pago sin `id` no se aplica.
- **Idempotencia:** el libro usa IDs externos; un evento repetido no duplica el
  ingreso. Una devolución antes del ingreso deja neto negativo y se regulariza
  al llegar el pago.
- **Reintentos:** 200 para eventos procesados o desconocidos; 401 para firma
  inválida; 503 cuando falta el secreto obligatorio; 502 para errores
  transitorios de Mercado Pago, para que reintente.
- **Contracargos:** se marca el turno `charged_back` y se registra en logs para
  revisión manual; no se inventa una salida de caja. La auditoría lo reporta.
- **Rate limit:** no se agregó uno servidor-a-servidor. La firma es la barrera
  principal; Funnel no permite una identidad de origen confiable. Al exigir el
  secreto, una notificación sin firma no llega a consultar la API.
- **Pérdida de eventos:** se soportan las notificaciones de pago y de orden; si
  Mercado Pago no entrega ninguna, la vuelta del checkout también consulta la
  API. No hay conciliación batch contra la API; sería una mejora posterior.

### `turno_pago_retorno`

- **Auth:** público con código de turno.
- **Entrada:** `payment_id`/`collection_id` de la query; se consulta la API y se
  exige que el ID devuelto coincida y que la referencia sea la del turno.
- **Riesgo:** un GET con un ID ajeno puede forzar una consulta, pero no aplicar
  el pago a otro turno por la validación de referencia. Tiene límite por sesión
  (`PUBLIC_CODE_MAX_PER_WINDOW`) y se evade rotando cookies.
- **Idempotencia:** el libro y los estados hacen que repetir la vuelta no
  duplique el cobro.

## 4. Backup y restauración

El procedimiento completo y las limitaciones están en
[`docs/backup-restore.md`](backup-restore.md). Resumen de la prueba aislada:

- Base fuente descartable migrada y con datos de dominio.
- Dump custom de 148.723 bytes, TOC de 385 líneas y `pg_restore` sin errores.
- Restauración en una base nueva: 39 tablas, 59 FKs, 74 únicas, 157 índices,
  56 migraciones (21 de `core`), conteos de dominio idénticos.
- Cero pagos huérfanos, importes no positivos, señas mayores al precio, duplas
  huérfanas o migraciones de `core` sin registrar.
- `check` y `migrate --check` sobre la base restaurada: OK.
- Los campos cifrados de Mercado Pago se descifraron con el mismo `SECRET_KEY`.

Limitaciones: no hay PITR/WAL, backup externo, retención programada ni prueba de
volumen. El dump contiene hashes y credenciales cifradas; debe protegerse.

## 5. Validación final

| Control | Resultado |
| --- | --- |
| `python manage.py test core` en PostgreSQL | **435 pruebas, OK** (250 s) |
| `python manage.py check` | OK |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `check --deploy` con `config.settings.prod` | Sólo `security.W009` por el `SECRET_KEY` de desarrollo del entorno |
| Rama local `0019 → 0021` en base descartable | OK |
| Escenarios de dos ramas + unión (réplica sintética; sólo recon, no valida la migración real) | Informativo |
| `audit_cash` sobre base restaurada | 0 hallazgos |
| `git diff --check` / `compileall` | OK |
| Commit / deploy / cambios en producción | Ninguno |

## 6. Bloqueos y riesgos antes del piloto

1. **Bloqueo duro:** falta el archivo real
   `0020_tournament_registration_code_unique_and_more.py`. Sin él no se puede
   agregar la unión definitiva ni validar que las operaciones no se pisen. Con
   dos ramas sin unión, Django no ejecuta `migrate`.
2. **Secreto de webhook:** hay que cargarlo por complejo en el panel. Desde la
   corrección de esta etapa, si falta, el checkout y el webhook quedan
   bloqueados (fail-closed); no se puede cobrar online hasta completarlo.
3. **Conciliación histórica:** la auditoría está lista, pero no se ejecutó sobre
   producción ni se repararon datos. Requiere ventana, backup verificado y
   aprobación.
4. **Backup:** no hay schedule, retención, copia externa ni PITR verificados.
5. **Funnel:** no hay IP real del visitante; los límites por sesión no son
   equivalentes a límites por IP. Los límites de reservas siguen siendo por
   teléfono y exigen revisión manual.
6. **Contracargos:** se marcan para revisión; no se contabiliza automáticamente
   el resultado del contracargo.
7. **Pagos reales:** no se usaron credenciales reales ni se hicieron cobros. La
   validación de firma se probó con HMAC calculado en tests; falta una prueba
   controlada con el panel real de Mercado Pago.
8. **Sin commit:** todos los cambios permanecen en el árbol de trabajo, junto
   con los archivos no trackeados previos (`docs/bosquejo-turnos*`).

## 7. Próximos pasos recomendados

1. Obtener y revisar la `0020` del otro clon; comparar operaciones.
2. Agregar la unión con `makemigrations --merge`, validar los cuatro escenarios
   en bases descartables y recién entonces planificar el deploy.
3. Cargar el secreto de webhook en cada complejo con cobro online.
4. Ejecutar `audit_cash --json` en producción en modo lectura y revisar los
   hallazgos con el complejo antes de reparar nada.
5. Definir backup programado, copia externa y simulacro de restauración.
6. Hacer una prueba controlada con cuenta de prueba de Mercado Pago, incluyendo
   pago aprobado, rechazado, devolución y reintento del webhook.
