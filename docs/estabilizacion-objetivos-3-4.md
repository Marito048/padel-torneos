# Estabilización — Objetivos 3 y 4 + revisión de throttling

> **Actualización de alcance (2026-10-21):** la primera versión del piloto
> opera sin pagos online. Este documento conserva el registro técnico de la
> integración de Mercado Pago y de la caja, que queda diferida. El estado
> vigente está en `docs/preparacion-piloto.md`.

Documento de diseño, cambios y cierre. Base inspeccionada: `main` en `4be83a0`.
No se tocó producción ni la base de datos del stack Docker; las pruebas se
ejecutan contra un PostgreSQL 16 descartable.

> Estado: los cambios descritos están implementados en el árbol de trabajo y
> probados contra PostgreSQL. **No se commiteó ni se desplegó nada.**

> Nota de contexto: el trabajo de los Objetivos 1 y 2 descripto en el informe
> anterior vive en otro clon (Windows) y **no está en este árbol**. Este documento
> y los cambios que siguen se hacen sobre `4be83a0`. Los archivos que se tocan
> (`views.py`, `models.py`, `cash.py`, `draws.py`, `players.py`) se solapan con
> aquel trabajo; la reconciliación será manual.

## 1. Objetivo 3 — Cobros, devoluciones y caja

### 1.1 Comportamiento inicial (antes de los cambios)

`CourtBooking` guarda el estado operativo con dos banderas:

- `is_paid`: seña abonada (o turno completo si no hay seña).
- `balance_paid`: saldo del club abonado.

`paid_amount` y `balance_amount` son propiedades calculadas **a partir de las
banderas**, no del libro de caja (`app/core/models.py:522-542`):

- `paid_amount` = seña si `is_paid`, más `price - deposit_amount` si
  `balance_paid`, tope `price`.
- `balance_amount` = `price - deposit_amount` mientras `balance_paid` sea falso
  (aunque la seña todavía no esté paga); 0 si `balance_paid` o si el turno no
  tiene seña y está pago.

El libro `CourtPayment` es append-only (`app/core/models.py:571-680`):

- Ingresos: `deposit`, `balance`, `full`.
- Egresos: `refund`, `chargeback`, `reversal` (importe positivo, signo negativo
  en `signed_amount`).
- `refundable_amount()` suma el neto del libro (`app/core/services/cash.py:244`).

Flujos:

| Flujo | Qué hace hoy | Problema |
|---|---|---|
| Cobro manual de seña | `panel_turno_accion` pone `is_paid=True` y registra `deposit` (`views.py:3379-3412`) | No hay lock: dos POST simultáneos pueden registrar dos veces. |
| Cobro manual de saldo | Pone `balance_paid=True` y registra `balance` (`views.py:3353-3367`) | El importe sale de `balance_amount` (banderas), no del neto real. |
| Desmarcar cobro | Baja la bandera y revierte el último movimiento del tipo (`views.py:3368-3378`, `cash.py:103`) | Puede revertir más de lo cobrado si antes hubo una devolución parcial (neto negativo). |
| Devolución manual | `record_manual_refund` bloquea el turno y valida contra el neto (`cash.py:253`) | No actualiza banderas ni `payment_status`; `paid_amount` sigue mostrando el total. Dos envíos iguales del mismo formulario pueden registrar dos devoluciones válidas. |
| Devolución online | Registra **el importe completo** del ingreso original, una sola vez por `payment_id` (`cash.py:191`) | Una devolución parcial de Mercado Pago se registra como total. Una devolución que llega antes del ingreso se pierde. |
| Pago online duplicado | Si ya hay un ingreso activo, lo reutiliza y descarta el segundo pago (`cash.py:158-176`) | El dinero del segundo pago existe pero no queda en caja. |
| Pago tardío | Si el turno estaba vencido y el horario sigue libre, lo revive; si otro tomó el horario, `approved_conflict` (`views.py:2467-2519`) | El ingreso se registra; la bandera `is_paid` se pone a mano antes del libro. |
| Cancelación | No devuelve nada automáticamente; avisa que hay que devolver (`views.py:2906-2935`, `3434-3451`) | `balance_amount` sigue mostrando saldo pendiente en un turno cancelado. |
| Cierre de caja | Guarda una foto de los totales (`cash.py:336`) | Correcto por diseño; movimientos posteriores no modifican el cierre. |

Casos de divergencia reproducidos:

1. Seña 2400 + saldo 5600; devolución parcial 1000 → caja neta 7000, pero
   `paid_amount` sigue 8000 y `balance_amount` 0.
2. Devolución total de la seña → `is_paid` sigue `True`, `paid_amount` 2400,
   caja neta 0.
3. Dos pagos online distintos aprobados para el mismo turno → el segundo no
   entra al libro.
4. Devolución online parcial → el egreso queda por el total.
5. Devolución antes del ingreso → no se registra.
6. Dos cobros manuales simultáneos → dos ingresos.
7. Desmarcar después de una devolución parcial → reversión por el importe
   original, neto negativo.

### 1.2 Regla contable implementada (única)

**El libro `CourtPayment` es la fuente de verdad del dinero en cuanto el turno
tiene al menos un movimiento.** Las banderas `is_paid`/`balance_paid` pasan a
ser estado derivado y se sincronizan después de cada movimiento. Para turnos
sin movimientos (legado), se mantiene el cálculo por banderas hasta que el
primer movimiento cree el asiento de apertura.

Definiciones (importes en centavos, `Decimal`):

- `net_paid = suma(signed_amount)` de todos los movimientos del turno.
  Puede ser negativo si llegó una devolución antes que el ingreso.
- `paid_amount = max(net_paid, 0)`: dinero neto cobrado (lo que muestra la UI).
- `refunded_amount = suma(amount de refund/chargeback/reversal)`.
- `overpaid_amount = max(net_paid - price, 0)`.
- `outstanding_amount = 0` si el turno está `cancelled` o `blocked`; si no,
  `min(max(price - net_paid, 0), price)`. Es la deuda total viva.
- `deposit_pending_amount = 0` si está cancelado/bloqueado; si no,
  `min(max(min(deposit_amount, price) - net_paid, 0), min(deposit_amount, price))`.
- `balance_amount` (saldo que se cobra en el club) =
  `max(price - max(net_paid, deposit_amount), 0)`, con tope `price`; 0 si está
  cancelado/bloqueado. Conserva la semántica de la UI: la seña se cobra aparte
  y el club cobra el resto; si una devolución reduce el neto por debajo de la
  seña, la diferencia aparece en `deposit_pending_amount`, no se esconde.
- `refundable_amount = max(net_paid, 0)`: nunca se devuelve más de lo cobrado.

Banderas derivadas (solo si hay libro):

- `is_paid = net_paid >= min(deposit_amount, price)` si hay seña; si no,
  `net_paid >= price`. Precio 0: `True`.
- `balance_paid = net_paid >= price`.
- `paid_at` = primer ingreso si `is_paid`; `balance_paid_at` = último ingreso
  si `balance_paid`.
- Si hay una devolución/contracargo real y `net_paid <= 0`, `payment_status`
  pasa a `refunded` (salvo estados de conflicto como `approved_conflict`).

### 1.3 Transiciones de estado

| Evento | Turno | Libro | Banderas |
|---|---|---|---|
| Reserva pública sin seña online | `booked` | vacío | `is_paid=False` (o `True` si precio 0) |
| Reserva pública con seña online | `pending_payment` | vacío | `is_paid=False` |
| Pago online aprobado, turno pendiente | `booked`, `payment_status=approved` | +`deposit` (o `full`) con `external_id` | derivadas |
| Pago online aprobado, turno vencido y horario libre | `booked` | +ingreso | derivadas |
| Pago online aprobado, turno cancelado u horario tomado | `cancelled`, `payment_status=approved_conflict` | +ingreso | derivadas; queda devolución pendiente |
| Pago online repetido (mismo `payment_id`) | sin cambio | sin cambio | sin cambio |
| Pago online distinto repetido | sin cambio | +ingreso nuevo | puede quedar `overpaid_amount` |
| Devolución/contracargo online | sin cambio de estado | -egreso por el delta informado | derivadas |
| Devolución manual | sin cambio de estado | -egreso | derivadas |
| Desmarcar cobro | sin cambio de estado | -`reversal` por el último ingreso, tope neto | derivadas |
| Cancelación | `cancelled` | sin cambio | derivadas; `outstanding=0`, neto sigue devolvible |
| Cierre de caja | sin cambio | sin cambio | sin cambio |

Invariantes:

1. No se registra un egreso por encima del neto cobrado (excepto una devolución
   online que llegue antes del ingreso, que se registra igual para no perder el
   dato; el neto puede quedar negativo y se regulariza al llegar el ingreso).
2. Un `external_id` identifica un evento externo; repetirlo no crea otro
   movimiento.
3. Una devolución manual repetida con la misma clave de idempotencia no crea
   otro movimiento.
4. Un turno cancelado no tiene saldo pendiente; su neto es devolvible.
5. Un turno sin movimientos usa las banderas legadas; el primer movimiento crea
   un asiento de apertura por lo ya cobrado y desde ahí manda el libro.

### 1.4 Cambios implementados

- `models.py`: propiedades `net_paid_amount`, `refunded_amount`,
  `overpaid_amount`, `outstanding_amount`, `deposit_pending_amount`; redefinir
  `paid_amount`/`balance_amount` con la regla anterior.
- `services/cash.py`:
  - `ensure_ledger_opening()` para turnos legados.
  - `sync_booking_payment_state()` (banderas derivadas, dentro de la
    transacción y con lock del turno).
  - `record_payment()` con lock del turno, idempotencia por `external_id`,
    asiento de apertura y sincronización.
  - `record_online_deposit()` sin descartar pagos distintos; concilia un cobro
    manual sin ID solo si el importe coincide.
  - `record_online_refund()` con importe parcial acumulado y delta; soporta
    devolución antes del ingreso.
  - `record_manual_refund()` con clave de idempotencia y tope neto.
  - `record_reversal()` con tope neto y reversión del último ingreso.
- `views.py`:
  - `_apply_mp_payment()` transaccional y con lock; extrae el importe
    devuelto de `transaction_amount_refunded`/`refunds`; no resucita un pago
    que ya fue devuelto.
  - `panel_turno_accion` calcula los importes bajo lock desde el libro; un
    segundo POST no duplica; el botón «Cobrar saldo» usa el saldo real.
  - `_decorate_turno_booking` usa `pay_amount` y muestra seña/saldo pendientes
    y sobrepago.
  - Devolución manual con token de idempotencia.
- Templates: importe en el botón, aviso de sobrepago, token oculto.

## 2. Objetivo 4 — Concurrencia de partidos

### 2.1 Comportamiento inicial

- `Match.save()` (`models.py:1325`) hace `full_clean()` y `transaction.atomic()`,
  pero **no toma locks**. El `old = Match.objects.get(pk=...)` se lee antes de
  cualquier lock; dos editores pueden pisarse.
- `register_match_result()` (`tournament_workflow.py:75`) bloquea el torneo
  (`select_for_update`) y re-lee el partido, pero no bloquea la fila del
  partido. Serializa por torneo, aunque un `Match.save()` directo (admin,
  creación) no pasa por ese lock.
- `sync_bracket_progression()` bloquea el torneo y actualiza el partido
  siguiente con `.update()` sin bloquear la fila destino
  (`draws.py:776-813`). Un guardado concurrente del destino puede pisar el
  avance.
- `delete_group_match()` y `remove_team_from_zone()` verifican y borran sin
  lock de torneo (`matches.py:45`, `players.py:471`). Un resultado concurrente
  puede entrar entre la verificación y el borrado.
- `ensure_zone_matches()` y `create_group_match()` calculan `slot` con
  `count()` (`players.py:496`, `matches.py:33`), lo que repite posiciones
  después de borrar partidos.
- `_create_playoff_tree()` bloquea el torneo y borra/crea la llave; un
  `Match.save()` directo del admin no participa del lock.
- El admin ya deriva las correcciones a `register_match_result()`
  (`admin.py:373`), pero las altas usan `obj.save()` directo.

Consecuencias verificables: dos resultados simultáneos pueden quedar en orden
indeterminado (el último gana) sin aviso; un resultado y un borrado pueden
perder el resultado; un avance de llave y una edición del partido destino
pueden dejar participantes viejos; dos generaciones de llave pueden competir.

### 2.2 Mecanismo implementado

1. **Lock único por torneo, siempre primero.** Todas las rutas de escritura de
   `Match` (servicios, admin, borrados, generación de llaves y zonas) toman
   `Tournament.objects.select_for_update()` antes de leer/escribir partidos.
   Torneos distintos no se bloquean entre sí.
2. **Lock de fila del partido después del torneo.** `Match.save()` y
   `register_match_result()` hacen `select_for_update()` sobre el partido
   cuando tiene `pk`. Orden fijo: torneo → partido. Esto evita interbloqueos.
3. **Versión optimista.** Se agrega `Match.version` (entero). `Match.save()`
   compara la versión en memoria con la de la fila bloqueada; si difiere,
   lanza `MatchConflictError` («el partido cambió…») en vez de pisar. Cada
   guardado incrementa la versión. `sync_bracket_progression()` incrementa la
   versión de los partidos que actualiza con `.update(version=F("version")+1)`.
   El formulario de resultados envía la versión y `register_match_result()` la
   valida; dos pestañas con el mismo formulario no pueden pisarse.
4. **Re-chequeo dentro del lock.** Borrados y composición de zonas vuelven a
   leer el estado y verifican `has_real_data` después de tomar el lock.
5. **Slots con `Max("slot")`**, no `count()`, para no repetir posiciones.
6. **Errores controlados** si el partido desapareció (llave regenerada o
   borrada): `ValidationError`, no `DoesNotExist` → 500.

No alcanza con `transaction.atomic()`: el atomic da atomicidad, pero sin locks
dos transacciones leen el mismo estado y la última pisa a la otra.

### 2.3 Pruebas de concurrencia (PostgreSQL real)

- Dos resultados incompatibles sobre el mismo partido con la misma versión:
  uno gana, el otro recibe conflicto; el resultado final es coherente.
- Dos partidos del mismo torneo en paralelo: ambos se guardan, sin deadlock.
- Dos torneos distintos en paralelo: no se bloquean.
- Dos generaciones de llave simultáneas: una sola estructura, sin duplicados.
- Dos altas del mismo cruce: una sola, la otra rechazada.
- Guardado secuencial con versión vieja: rechazado.
- Borrado contra resultado: no se pierde un resultado (se rechaza el borrado o
  el resultado, según el orden, con error controlado).

Se ejecutan con `TransactionTestCase`, conexiones independientes y
`threading.Barrier`; se saltean en SQLite.

## 3. Revisión de throttling

### 3.1 Estado inicial (verificado antes de los cambios)

- No hay límite en `turno_reservar` (POST), `turno_cancelar` (POST) ni
  `signup_request` (POST).
- `public_tournament_signup` limita fallos de código con `LocMemCache`
  (`views.py:2192-2201`) usando el **primer** valor de `X-Forwarded-For`, que
  el cliente puede falsificar, o `REMOTE_ADDR`.
- `_SIGNUP_FAIL_LIMIT=15`, ventana 900 s. El contador vive en el proceso: con
  N workers de gunicorn el límite efectivo se multiplica por N.
- El stack documentado usa `tailscale funnel --bg 8080` → `127.0.0.1:8080`.
  Funnel es L4/TCP: no propaga la IP del visitante. `REMOTE_ADDR` es loopback
  para todos; un `X-Forwarded-For` enviado por el cliente no es confiable.

### 3.2 Decisión implementada

- Implementar `_client_ip()` con `TRUSTED_PROXY_IPS` (IPs/CIDR). Si el proxy
  inmediato no es confiable, se ignora `X-Forwarded-For`. Si lo es, se recorre
  la cadena de derecha a izquierda y se toma la primera IP no confiable (el
  cliente solo puede inyectar al principio).
- **Con Funnel, `TRUSTED_PROXY_IPS` debe quedar vacío.** No se presenta el
  límite por IP como protección individual: el límite de códigos se clave por
  IP + sesión, para que un visitante no bloquee a todos los demás; se documenta
  que un bot que rote cookies puede evadirlo.
- Respaldo para reservas: cuota por teléfono respaldada en la base (no en
  caché), dentro de un lock del complejo:
  - máximo de reservas activas (`pending_payment`/`booked`) por teléfono;
  - máximo de reservas creadas por teléfono en la última hora.
  Esto funciona igual con varios workers y no depende de la IP.
- Se mantiene el límite de intentos de código por sesión; la entropía del
  código (8 caracteres, alfabeto de 32) y la aprobación manual siguen siendo
  la defensa principal.
- No se agrega Redis ni otra infraestructura. Los contadores de caché siguen
  siendo por proceso; se documenta el efecto en el límite efectivo.

### 3.3 Por endpoint

| Endpoint | Identidad | Límite | Multi-worker |
|---|---|---|---|
| `POST /turnos/reservar/` | Teléfono normalizado | 3 activas + 10/hora (configurable) | DB, compartido |
| `GET /turnos/confirmado/<code>/` | Sesión | 60/300 s (`PUBLIC_CODE_*`) | cache por proceso (× workers) |
| `POST /turnos/cancelar/<code>/` | Sesión + posesión del código | 60/300 s (`PUBLIC_CODE_*`) | cache por proceso (× workers) |
| `GET /turnos/<code>/pagar/` y retorno | Sesión + posesión del código | 60/300 s por endpoint | cache por proceso (× workers) |
| `POST /torneos/<slug>/anotarse/` | IP confiable + sesión, solo fallos de código | 15/15 min | cache por proceso (× workers) |
| `POST /registrarse/` | Sesión | 10/900 s (`SIGNUP_REQUEST_*`) | cache por proceso (× workers) |
| `GET /turnos/` | — | Sin límite; solo lectura | N/A |

### 3.4 Verificación de identidad y despliegue

- **Funnel nativo (LXC, el que está en uso):** `tailscale funnel --bg 8080`
  termina TLS y reenvía a `127.0.0.1:8080`; es L4/TCP. Django recibe
  `REMOTE_ADDR=127.0.0.1` (o `::1`) para **todos** los visitantes. No hay IP
  real que extraer y `TRUSTED_PROXY_IPS` debe quedar vacío. El límite de
  códigos se vuelve por sesión (no por IP); la cuota de reservas queda por
  teléfono en la base. No se afirma que exista una protección por IP real.
- **Docker con Caddy:** Caddy reenvía a `app:8000`; Django ve la IP del
  contenedor de Caddy (red privada) y Caddy agrega `X-Forwarded-For`. Para
  obtener la IP del visitante hay que declarar la red de Caddy en
  `TRUSTED_PROXY_IPS` (por ejemplo, el CIDR de la red Docker). Si se deja
  vacío, `_client_ip` ignora el header y todos comparten la IP de Caddy, que es
  el comportamiento seguro por defecto. Los puertos del `app` están publicados
  sólo en `127.0.0.1`, así que no hay acceso directo desde Internet.
- **Multi-worker:** el contador de códigos vive en `LocMemCache`, no en una
  caché compartida. Con 3 workers de gunicorn el límite efectivo por sesión es
  aproximadamente 3×15 intentos por ventana; además, rotar cookies lo evade.
  Es una barrera de fuerza bruta, no una autenticación. La cuota de reservas no
  tiene ese problema porque se consulta en PostgreSQL bajo lock del complejo.
- `_client_ip` recorre `X-Forwarded-For` de derecha a izquierda y toma la
  primera IP fuera de `TRUSTED_PROXY_IPS`; si el proxy inmediato no es
  confiable, ignora el header. Esto impide que un cliente invente una IP a la
  izquierda de la cadena.

## 4. Cierre de implementación

### 4.1 Reglas de negocio: antes y después

| Tema | Antes | Después |
|---|---|---|
| Fuente del dinero | Banderas `is_paid`/`balance_paid` | Libro `CourtPayment`; banderas derivadas cuando hay movimientos |
| Cobro de seña/saldo | Importe desde `price`/`deposit_amount`, sin lock | Importe desde `outstanding_amount`/`deposit_pending_amount`, bajo lock del turno |
| Devolución manual | No actualizaba banderas; dos envíos podían duplicar | Tope neto, sincroniza banderas y token de idempotencia del formulario |
| Devolución online parcial | Registraba el total | Registra el delta acumulado informado por Mercado Pago |
| Devolución online antes del pago | Se perdía | Se registra como egreso; neto negativo temporario; el pago posterior lo regulariza |
| Segundo pago online distinto | Se descartaba | Se registra; si supera el precio queda `overpaid_amount` |
| Pago manual + webhook | Podía duplicar | Se concilia por importe y luego por `external_id` |
| Turno cancelado | Seguía mostrando saldo | `outstanding_amount`/`balance_amount` = 0; el neto sigue devolvible |
| Resultados concurrentes | Último guardado pisaba | Lock torneo → partido + `version`; el segundo recibe conflicto controlado |
| Slots de zona | `count()` podía repetir | `Max("slot") + 1` bajo lock |
| Borrado/composición de zonas | Verificaba fuera del lock | Re-lee y valida dentro del lock del torneo |
| IP del cliente | Primer `X-Forwarded-For` (falsificable) | `TRUSTED_PROXY_IPS`; si el proxy no es confiable se ignora el header |
| Abuso de reservas | Sin límite | Cuota por teléfono en DB (activas + por hora) |
| Fuerza bruta de código | Contador por IP falsificable | IP confiable + sesión; documentado como no-per-IP en Funnel |

### 4.2 Archivos y funciones afectadas

- `app/core/models.py`: `CourtBooking.refresh_from_db`, `_ledger_summary`,
  `_legacy_paid_amount`, `net_paid_amount`, `refunded_amount`,
  `overpaid_amount`, `paid_amount`, `outstanding_amount`,
  `deposit_pending_amount`, `balance_amount`; `MatchConflictError`,
  `Match.version`, `Match.save`.
- `app/core/services/cash.py`: `_net_paid`, `_publish_state`,
  `ensure_ledger_opening`, `sync_booking_payment_state`, `record_payment`,
  `record_reversal`, `record_online_deposit`, `record_online_refund`,
  `refundable_amount`, `record_manual_refund`.
- `app/core/views.py`: `_apply_mp_payment`, `_mp_refunded_total`,
  `_decorate_turno_booking`, `panel_turno_accion`, `panel_turnos_caja`,
  `panel_turnos_caja_devolucion`, `_client_ip`, `_signup_attempts_key`,
  `_normalize_phone`, `_reservation_quota_error`, `turno_reservar`.
- `app/core/services/matches.py`: `create_group_match`, `delete_group_match`.
- `app/core/services/players.py`: `remove_team_from_zone`,
  `ensure_zone_matches`.
- `app/core/services/draws.py`: `sync_bracket_progression` (lock de filas y
  `version`).
- `app/core/services/tournament_workflow.py`: `register_match_result`,
  reexportación de `MatchConflictError`.
- `app/core/services/schedule.py`: prefetch de movimientos en la agenda.
- `app/core/admin.py`: `MatchAdmin` (versión y `expected_version`).
- `README.md`, `app/config/settings/base.py`, `.env.example`: `TRUSTED_PROXY_IPS`,
  `PUBLIC_RESERVATION_MAX_ACTIVE`, `PUBLIC_RESERVATION_MAX_PER_HOUR`.
- Templates `panel/results.html`, `panel/turnos.html`, `panel/turnos_caja.html`.
- Migración nueva `app/core/migrations/0020_match_version.py`.

### 4.3 Pruebas y resultados

Ejecutadas contra PostgreSQL 16.15 descartable (`padel-pg16-test`,
`127.0.0.1:5434`, base `padel_test`). No se usó la base ni el stack de producción.

- `core.tests_objetivo3`: 23 pruebas de consistencia contable, devoluciones
  parciales/totales, duplicados, orden invertido, legado, reversión con tope,
  botón del panel y token de caja.
- `core.tests_objetivo4`: 10 pruebas de concurrencia con `TransactionTestCase`,
  conexiones independientes y `threading.Barrier`: resultados incompatibles,
  dos partidos del mismo torneo, torneos distintos sin bloqueo, generación de
  llave concurrente, alta duplicada, borrado contra resultado, versión obsoleta,
  partido eliminado, avance de llave con incremento de versión y cancelación de
  turno contra webhook.
- `core.tests_throttling`: 17 pruebas de IP confiable, cadena de proxies,
  sesión, cuota por teléfono, normalización de teléfono y límites de consultas
  con código de turno.
- Suite completa `manage.py test core` en PostgreSQL: **435 pruebas, OK** (250 s, 2026-10-21).
- El cierre de esta etapa, con la matriz de pagos, la auditoría de endpoints y la
  prueba de backup/restore, está en `docs/cierre-piloto.md`.
- `manage.py check`: OK.
- `manage.py makemigrations --check --dry-run`: `No changes detected`.
- `manage.py check --deploy` con `config.settings.prod`: sólo
  `security.W009` por el `SECRET_KEY` de desarrollo del entorno; no es un
  hallazgo nuevo del código.

### 4.4 Riesgos residuales y límites

- El cálculo legado por banderas sigue vigente para turnos **sin** movimientos;
  recién el primer movimiento materializa el asiento de apertura. Turnos viejos
  con banderas inconsistentes pueden mostrar una foto inicial imperfecta.
- Una devolución online que llega antes del ingreso deja el neto negativo hasta
  que llega el pago; se decidió no perder el dato. La UI muestra `paid_amount=0`
  y el egreso en caja.
- Los sobrepagos se registran y se muestran, pero no se devuelven
  automáticamente; requieren una devolución manual.
- Un pago online con importe distinto al esperado se registra por el importe
  real y queda `amount_mismatch` para revisión manual. Un pago inferior no
  confirma el turno; uno superior puede confirmarlo pero queda marcado. El
  dinero no se oculta de la caja.
- «Marcar pendiente» usa una reversión contable (no devuelve dinero) y una
  devolución real es un movimiento distinto. Para pagos online, una devolución
  posterior se limita al importe no revertido para no contar dos veces la misma
  salida; aun así son flujos alternativos y la UI advierte cuando la reversión
  es de Mercado Pago.
- `record_manual_refund` sin `idempotency_key` sigue siendo no idempotente para
  llamadas internas. El formulario de caja siempre envía el token; usarlo desde
  otro cliente exige pasar una clave.
- La cuota por teléfono se evade cambiando de número; es un control de abuso, no
  autenticación. Bajo Funnel no hay IP real, así que no existe un límite por IP
  confiable.
- El contador de códigos usa `LocMemCache`: no se comparte entre workers y se
  evade rotando cookies. Se documenta el límite efectivo (≈15 × workers por
  sesión/ventana). No se agregó Redis ni otra infraestructura.
- `signup_request` y los endpoints con código de turno tienen límites por
  sesión con `LocMemCache` (`SIGNUP_REQUEST_MAX_PER_WINDOW` y
  `PUBLIC_CODE_MAX_PER_WINDOW`, respectivamente). No son una protección por IP
  real y se evaden rotando cookies; para turnos, el código de 8 caracteres, el
  CSRF y la revisión manual siguen siendo la defensa principal.
- El webhook de Mercado Pago es `csrf_exempt`, pero con token cargado exige el
  secreto de firma y valida `x-signature` antes de consultar la API. Sin secreto
  responde `503` y el checkout también queda bloqueado; no se agregó un rate
  limit servidor-a-servidor porque Funnel no ofrece una identidad de origen
  confiable.
- La migración `0020_match_version` colisiona con la `0020` del otro clon. No se
  renombró ninguna migración; la estrategia es conservar ambos nombres y agregar
  una migración de unión cuando el archivo del otro clon esté disponible. El
  archivo no estaba en este árbol ni en el LXC, así que la unión real sigue
  bloqueada; ver `docs/cierre-piloto.md`.
- El lock por torneo serializa escrituras del mismo torneo; los torneos
  distintos no se bloquean. `sync_bracket_progression` bloquea las filas de la
  llave de ese torneo, no de otros.
- Los tests de concurrencia se saltean en SQLite; se ejecutaron en PostgreSQL
  real. El stack de producción usa PostgreSQL.
- El cierre de caja sigue siendo una foto: un movimiento concurrente con
  `paid_at` anterior puede quedar fuera. No se bloquea el día por diseño.
- No se modificó la base ni los servicios de producción, no se desplegó y no se
  hizo commit.

### 4.5 Cambios sin commitear

Modificados: `.env.example`, `README.md`, `app/config/settings/base.py`, `app/core/admin.py`,
`app/core/models.py`, `app/core/services/cash.py`, `app/core/services/draws.py`,
`app/core/services/matches.py`, `app/core/services/players.py`,
`app/core/services/schedule.py`, `app/core/services/tournament_workflow.py`,
`app/core/views.py`, `app/templates/panel/results.html`,
`app/templates/panel/turnos.html`, `app/templates/panel/turnos_caja.html`.

Nuevos: `app/core/migrations/0020_match_version.py`,
`app/core/migrations/0021_complex_mp_webhook_secret_encrypted.py`,
`app/core/management/commands/audit_cash.py`, `app/core/tests_objetivo3.py`,
`app/core/tests_objetivo4.py`, `app/core/tests_throttling.py`,
`docs/estabilizacion-objetivos-3-4.md`, `docs/backup-restore.md` y
`docs/cierre-piloto.md`.

Ya existían sin trackear (no son de este trabajo): `docs/bosquejo-turnos.html`,
`docs/bosquejo-turnos.png`, `docs/bosquejo-turnos-mobile.png`.

### 4.6 Comandos de validación

```bash
cd /home/mario/padel-torneos/app
DATABASE_URL='postgres://postgres:testpass@127.0.0.1:5434/padel_test' \
  ../.venv/bin/python manage.py test core
DATABASE_URL='postgres://postgres:testpass@127.0.0.1:5434/padel_test' \
  ../.venv/bin/python manage.py check
DATABASE_URL='postgres://postgres:testpass@127.0.0.1:5434/padel_test' \
  ../.venv/bin/python manage.py makemigrations --check --dry-run
DATABASE_URL='postgres://postgres:testpass@127.0.0.1:5434/padel_test' \
  DJANGO_SETTINGS_MODULE=config.settings.prod \
  ../.venv/bin/python manage.py check --deploy
```

El PostgreSQL descartable se levanta aparte del stack de producción:

```bash
docker run -d --name padel-pg16-test \
  -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=padel_test \
  -p 127.0.0.1:5434:5432 postgres:16
```

