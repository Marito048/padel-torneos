# Preparación del piloto controlado — primera versión sin pagos online

Fecha: 2026-10-21. Base de trabajo: `4be83a0c277fb116a452e7abdbaef45c584148bf`.
Todo el trabajo quedó en el árbol local: **no se hizo commit, push, deploy, merge
ni se modificó producción**. Las pruebas se ejecutaron en PostgreSQL 16.15
descartable (`padel-pg16-test`, `127.0.0.1:5434`).

## 1. Decisión y alcance

La primera versión del piloto permite operar **torneos, reservas de canchas y
cobros manuales**. La integración de **Mercado Pago Checkout Pro queda diferida**
para una segunda etapa: está conservada en el código, pero deshabilitada en el
servidor. No se crean preferencias, no se inician pagos online y los retornos o
webhooks no modifican reservas ni caja.

| Escenario | Veredicto | Condición |
| --- | --- | --- |
| Ensayo local | **Apto** | Datos ficticios, base descartable y pagos online apagados. |
| Staging funcional | **Apto con alcance acotado** | Sólo con datos ficticios y este árbol. No sirve como ensayo de un clon de producción mientras falte la migración real. |
| Piloto restringido con datos reales | **No apto todavía** | Bloquean la migración faltante, el backup operativo, la decisión de privacidad y la revisión de datos de producción. |
| Producción | **No apto** | No se debe desplegar ni migrar producción en este estado. |
| Pagos online | **Fuera de alcance** | No se habilitan en esta versión; su reactivación tiene requisitos propios. |

Resumen:

| Pregunta | Respuesta |
| --- | --- |
| ¿Se puede desplegar hoy? | **No**: falta recuperar y unir la migración `0020` ajena. |
| ¿Se puede iniciar un piloto con datos reales? | **No todavía**: faltan backup programado/offsite, decisión de privacidad y conciliación de producción. |
| ¿Se puede ensayar localmente? | **Sí**, con datos ficticios y el interruptor de pagos online apagado. |
| ¿Se puede probar en staging? | **Sí, con alcance funcional y datos ficticios**; no como réplica del clon de producción. |
| ¿Hay pagos online activos? | **No**. |
| ¿Se modificó producción? | **No**. |
| ¿Hay commit, push o merge? | **No**. |

## 2. Método

Se revisaron, en modo lectura y sobre bases descartables:

- todos los puntos de entrada a Mercado Pago: reserva pública, confirmación,
  checkout, retorno, webhook, panel de ajustes, plantillas, modelos, servicios,
  configuración y migraciones;
- el flujo manual de reservas, seña, saldo, devoluciones y reversiones;
- permisos por complejo, endpoints públicos y separación de datos;
- el grafo de migraciones y el bloqueo de la `0020` ajena;
- la suite completa y los controles de Django.

No se usaron credenciales reales de Mercado Pago, no se accedió al sandbox y no
se ejecutaron cobros. No se leyó ni registró ningún secreto real. No se borró ni
se reemplazó la migración faltante.

## 3. Cambios de esta etapa

### 3.1 Interruptor explícito y aplicación en servidor

| Archivo | Cambio |
| --- | --- |
| `app/config/settings/base.py` | Nueva variable `MERCADO_PAGO_ONLINE_PAYMENTS_ENABLED`, con valor por defecto `False`. |
| `app/config/settings/prod.py` | El interruptor queda forzado a `False` en producción, aunque una variable de entorno intente activarlo. |
| `app/core/models.py` | `Complex.online_payments_enabled` devuelve `False` cuando el interruptor global está apagado, antes de mirar token o secreto. |
| `app/core/views.py` | Guardas server-side en checkout, retorno y webhook. Ninguno llama a Mercado Pago ni modifica la reserva con la integración apagada. |
| `app/core/views.py` | El panel de ajustes informa que el pago online está deshabilitado y no exige token ni secreto para guardar u operar. |
| `app/templates/turnos.html` | Aviso de pago online no disponible y flujo de reserva manual. |
| `app/templates/turno_confirmado.html` | Se oculta el botón de pago y el link externo; se conserva el alias/CVU como dato de transferencia manual. |
| `app/templates/panel/turnos_ajustes.html` | Estado «Deshabilitado», explicación de los campos diferidos y ausencia de exigencia de secreto. |
| `.env.example` | Variable documentada con valor `false` y aclaración del forzado en producción. |
| `README.md` y `docs/manual-administradores.html` | Documentación de la primera versión y de la reactivación futura. |

No se agregaron modelos, campos ni migraciones. No se eliminaron los modelos,
campos cifrados, servicios ni migraciones de Mercado Pago.

### 3.2 Comportamiento con la opción deshabilitada

- La reserva pública se crea en modo manual, sin retención por pago online.
- El endpoint de checkout no crea ninguna preferencia y muestra la confirmación
  con un aviso; no se puede iniciar un pago entrando directo a la URL.
- El retorno del checkout no consulta la API, no aplica pagos y no ejecuta el
  mantenimiento de vencimientos, por lo que tampoco cambia el estado de la
  reserva.
- El webhook responde `200` con `ignored=online_payments_disabled` para evitar
  reintentos, pero no consulta la API, no crea movimientos y no confirma ni
  revive reservas.
- La interfaz informa que el pago online no está disponible.
- El alias/CVU puede mostrarse como dato de transferencia manual; el link de
  pago externo se oculta.
- Un complejo sin Mercado Pago sigue operando igual. Tampoco hace falta token
  ni secreto para operar manualmente.
- `CourtPayment`, la auditoría `audit_cash`, los cobros manuales, las
  devoluciones y las reversiones mantienen su comportamiento y sus reglas de
  idempotencia.

### 3.3 Entradas revisadas

| Punto de entrada | Comportamiento actual |
| --- | --- |
| Reserva pública `/turnos/` | `Complex.online_payments_enabled=False`; reserva manual. |
| Confirmación del turno | Sin botón de checkout; aviso de pago online no disponible; alias manual visible. |
| Checkout `/turnos/<code>/pagar/` | Guarda antes de crear la preferencia; muestra la confirmación sin llamar a Mercado Pago. |
| Retorno del checkout | Guarda antes de consultar el pago; renderiza la confirmación sin aplicar movimientos ni cancelar por vencimiento. |
| Webhook por complejo | Guarda antes de buscar el complejo, validar firma o consultar la API. |
| Panel «Precio y avisos» | Informa el estado deshabilitado; guardar token/secreto no habilita cobros. |
| Plantillas públicas | No se muestra el link externo de pago; el alias queda como dato manual. |
| Servicios y modelos | Conservados; no se usan desde endpoints públicos mientras el interruptor esté apagado. |

## 4. Migraciones: bloqueo duro sin resolver

### Estado real

| Entorno | Última migración `core` | Observación |
| --- | --- | --- |
| LXC de producción | `0019_backfill_court_payments` | Consulta de sólo lectura; no se migró. |
| Árbol local | `0021_complex_mp_webhook_secret_encrypted` | Una sola hoja; suite y restore locales. |
| Base descartable | `0021` | Historial confirmado. |

Falta `app/core/migrations/0020_tournament_registration_code_unique_and_more.py`.
La única copia encontrada está en `/tmp/opencode/padel-migration-recon/` y es
una **réplica sintética**, no el archivo real; no se usó para crear un merge.

### Por qué sigue bloqueando

El otro clon mantiene dos ramas `0020`. Sin una migración de unión, Django
aborta con:

```text
CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph
```

La unión debe depender de **ambas** `0020` y de `0021`; no se debe renombrar
ninguna migración ya aplicada. Antes de crearla hay que comparar el archivo real
y comprobar que las operaciones no se pisen.

### Secuencia segura

1. Obtener el archivo real del clon donde se aplicó.
2. Comparar operaciones contra `0020_match_version` y `0021`.
3. Crear `0022_merge_...` con dependencias de las tres hojas.
4. Probar en bases nuevas: sólo una rama aplicada, sólo la otra, ambas y base
   completa; luego `migrate --check` y `showmigrations`.
5. Recién después planificar producción, con backup verificado y ventana.

No se agregó la unión mientras falte el archivo: una dependencia a un módulo
inexistente deja el grafo inválido. **Este bloqueo no se considera resuelto por
el cambio de alcance.**

## 5. Auditoría de seguridad y operación manual

### 5.1 Permisos y aislamiento

| Control | Resultado |
| --- | --- |
| Vistas del panel | Requieren login; turnos, caja y ajustes usan `require_complex_access` o `_manageable_complexes`. |
| Pruebas de acceso cruzado | Cubiertas: staff de otro complejo recibe 403 en reservas, caja, devoluciones y ajustes. |
| Torneos | `_tournament_or_404` exige acceso por complejo; los torneos heredados sin complejo quedan para staff de plataforma. |
| Admin de canchas, turnos, caja y torneos | Filtrado por complejo; `CourtPayment` y `CashClose` son de sólo lectura en admin. |
| Jugadores | **La base es global por diseño.** `panel_players` y `PlayerAdmin` listan y permiten editar fichas de todos los complejos, incluidos teléfono y email. |
| Consecuencia | Aceptable sólo si el piloto tiene un operador/equipo de confianza y un único complejo. Si hay complejos independientes, es un riesgo de privacidad que debe resolverse antes del piloto. |

La decisión de privacidad sigue pendiente y no se implementó un cambio de
modelo mientras falte la migración ajena. Las opciones documentadas antes
(single-tenant de confianza, ocultamiento por relación o pertenencia por
complejo) no fueron decididas por negocio.

### 5.2 Cobros manuales y caja

- `CourtPayment` sigue siendo la fuente de verdad cuando hay movimientos; las
  banderas legadas sólo aportan una apertura cuando existe evidencia.
- Los cobros manuales usan lock del turno y registran medio, importe, usuario y
  nota.
- Las devoluciones manuales son idempotentes con el token del formulario y no
  superan el neto cobrado.
- Las reversiones no borran el movimiento original.
- El cierre de caja es una foto y no bloquea movimientos posteriores.
- `audit_cash --json` sigue siendo de sólo lectura y reporta incoherencias,
  movimientos huérfanos y contracargos sin conciliar.

### 5.3 Integración de Mercado Pago conservada

La integración sigue disponible para la segunda etapa y conserva las reglas ya
probadas:

- el pago online exige `external_reference`, ID y moneda configurada;
- el webhook consulta siempre la API y valida firma;
- con token y sin secreto, checkout y webhook fallan cerrado;
- una devolución o un contracargo repetido no duplica movimientos;
- los importes reales se registran y las diferencias quedan para revisión.

En esta versión esas rutas no se alcanzan desde endpoints públicos porque el
interruptor global está apagado. Los tests de la integración se ejecutan con un
override explícito para comprobar que el código sigue funcionando cuando se
reactive.

### 5.4 Endpoints públicos y abuso

| Endpoint | Control actual | Evaluación |
| --- | --- | --- |
| Reserva pública | Cuota por teléfono en DB, validación de horario y unicidad. | Aceptable para piloto; se evade cambiando de teléfono. |
| Confirmación por código | Código de 8 caracteres sobre alfabeto de 32 (~40 bits) y límite por sesión de 60 consultas/300 s. | Fuerza bruta inviable; si el código se filtra funciona como bearer. Aceptable con difusión limitada y logs protegidos. |
| Cancelación pública | POST con CSRF, código y ventana temporal; mismo límite por sesión. | Aceptable. |
| Checkout y retorno | Guardas de pago online; no hay llamada a Mercado Pago. | Sin superficie de cobro en esta versión. |
| Webhook | Guarda temprana; responde `200` sin procesar. | Sin efecto contable ni sobre reservas. |

No hay IP real del visitante con Tailscale Funnel. `TRUSTED_PROXY_IPS` debe
quedar vacío; los límites por sesión/cookie no son una defensa por IP. El límite
de código usa la caché local de cada worker y se puede evadir rotando cookies o
cambiando de worker; la entropía del código, la difusión controlada, los límites
de reservas por teléfono y la revisión manual siguen siendo la defensa
principal.

### 5.5 Secretos, HTTPS y cookies

- Producción define `DEBUG=False`, redirección HTTPS, HSTS, cookies `Secure`,
  `X_FRAME_OPTIONS=DENY` y `SECURE_REFERRER_POLICY`.
- `SECRET_KEY` vacío/corto no se acepta; en producción debe ser largo, aleatorio
  y estable. Cambiarlo invalida los tokens cifrados.
- `DATABASE_URL` es obligatoria en producción; no cae silenciosamente a SQLite.
- Los formularios de token/secreto no vuelven a mostrar los valores; el admin
  los excluye y se guardan cifrados para la segunda etapa.
- `CSRF_TRUSTED_ORIGINS` debe coincidir con el dominio HTTPS real.
- No se exige HTTPS para operar manualmente en local, pero cualquier staging o
  producción debe publicarse con HTTPS.

### 5.6 Logs y PII

- No se registra el cuerpo de error de Mercado Pago.
- No se registra la URL de CallMeBot (contenía API key y texto del cliente).
- La reserva se registra por ID, no por nombre ni código.
- El código de turno puede aparecer en logs de acceso HTTP y los backups
  contienen PII y credenciales cifradas; ambos deben tener acceso restringido y
  retención definida.

## 6. Pruebas ejecutadas

Comando de la suite completa:

```bash
cd /home/mario/padel-torneos/app
DATABASE_URL='postgres://postgres:testpass@127.0.0.1:5434/padel_test' \
  ../.venv/bin/python manage.py test core
```

Resultado: **441 pruebas, OK**, 206 s, PostgreSQL 16.15. Incluye las pruebas de
concurrencia de PostgreSQL y las regresiones nuevas.

Regresiones específicas de esta etapa, en
`core.tests_turnos.OnlinePaymentsDisabledTests`:

1. La reserva pública queda manual aunque el complejo tenga token y secreto
   configurados.
2. El checkout directo no crea preferencia ni movimientos, y no cancela una
   retención vencida.
3. El retorno no consulta ni aplica un pago, y tampoco cancela una retención
   vencida.
4. El webhook responde `200` ignorado y no toca la reserva ni la caja.
5. El panel informa el estado deshabilitado y no exige secreto.
6. El cobro manual y la devolución idempotente siguen funcionando.

Además, la suite existente de Mercado Pago se ejecuta con
`MERCADO_PAGO_ONLINE_PAYMENTS_ENABLED=True` mediante override, para verificar
que la integración conservada no se rompió y que puede reactivarse con pruebas
controladas.

Otros controles:

| Control | Resultado |
| --- | --- |
| `manage.py check` | Sin errores. |
| `manage.py makemigrations --check --dry-run` | `No changes detected`. |
| `manage.py check --deploy` con `config.settings.prod` y clave fuerte | Sin advertencias. |
| `python -m compileall -q app` | Sin errores. |
| `git diff --check` | Sin errores de whitespace. |

La suite **no** prueba una cuenta real de Mercado Pago ni una restauración de
producción. Tampoco se repitió el simulacro de backup/restore en esta etapa; la
evidencia siguiente corresponde al trabajo previo y sigue siendo válida como
procedimiento local.

## 7. Backup/restore verificado previamente

Se creó una base fuente descartable `padel_pilot_source`, se migró y se cargaron
datos de dominio; se hizo un dump lógico comprimido y se restauró en una base
**nueva** `padel_pilot_restore`.

- Dump: **148.843 bytes**; TOC legible: 385 líneas.
- Restauración sin errores.
- Origen y destino: 39 tablas, 59 FKs, 157 índices, 56 migraciones.
- Conteos idénticos: 1 usuario, 1 complejo, 1 cancha, 1 turno, 2 movimientos de
  caja, 1 cierre, 1 torneo, 2 jugadores, 1 dupla, 1 solicitud de registro.
- Integridad: 0 pagos huérfanos, 0 importes no positivos, 0 señas mayores al
  precio, 0 reversiones duplicadas.
- `manage.py check` y `migrate --check` sobre la restaurada: OK.
- `audit_cash --json`: 1 turno revisado, 0 hallazgos.
- Token y secreto de Mercado Pago ficticios se descifraron en la restaurada con
  el mismo `SECRET_KEY`.

Esto valida el procedimiento lógico, no un backup continuo ni un RPO/RTO de
producción. El detalle de comandos y limitaciones está en
`docs/backup-restore.md`.

## 8. Riesgos y bloqueos restantes

### Bloqueantes para el piloto con datos reales

1. **Migración:** falta el archivo real de la `0020` ajena y la unión del grafo.
2. **Backup operativo:** no hay backup programado, retención, copia externa ni
   PITR verificados.
3. **Privacidad:** la base de jugadores es global y expone teléfonos/emails
   entre complejos. Si el piloto incluye operadores independientes, hay que
   decidir e implementar una separación antes de abrir.
4. **Datos de producción:** falta ejecutar `audit_cash --json` de sólo lectura y
   acordar la conciliación manual de hallazgos históricos.
5. **Despliegue/reversión:** no hay ensayo sobre una copia de producción ni
   ventana y runbook aprobados.

### Riesgos del alcance actual

- Los complejos sin token operan normalmente, pero el alias/CVU se muestra como
  dato manual; el link externo de pago queda oculto. Es una decisión deliberada
  para no iniciar pagos online desde la interfaz.
- Una reserva `pending_payment` preexistente de una etapa anterior no será
  confirmada por webhook ni por retorno mientras el interruptor esté apagado.
  Debe revisarse manualmente; el vencimiento puede cancelarla.
- El límite por sesión es evadible rotando cookies y no reemplaza una defensa
  por IP. Funnel no entrega IP real.
- El cierre de caja es una foto; un movimiento cargado tarde puede quedar fuera
  de la foto y aparecer en el reporte actual.
- No hay monitoreo centralizado ni alertas automáticas de caja.

### Riesgos diferidos de Mercado Pago

No son bloqueantes para esta primera versión, pero sí para reactivarla:

- no hay E2E con credenciales `TEST-` reales;
- no hay conciliación batch con Mercado Pago;
- no se verificó el secreto de webhook en la configuración real;
- el token y el secreto están en la base y en el dump;
- un cambio de `SECRET_KEY` obliga a recargar credenciales.

## 9. Criterios de aceptación

### Para ensayo local

- [x] Suite completa en PostgreSQL descartable.
- [x] Interruptor apagado por defecto y forzado en producción.
- [x] Checkout, retorno y webhook sin efectos con la integración apagada.
- [x] Reserva manual, cobro, devolución y reversión probados.
- [x] Sin credenciales reales ni datos de producción.

### Para staging funcional

- [ ] Datos ficticios y base descartable.
- [ ] `MERCADO_PAGO_ONLINE_PAYMENTS_ENABLED=false`.
- [ ] Smoke tests de reserva manual, caja y permisos.
- [ ] Backup/restore del entorno de staging documentado.
- [ ] No usar un clon de producción mientras falte el merge de migraciones.

### Para piloto restringido con datos reales

- [ ] Migración real recuperada, comparada y unión probada.
- [ ] Backup programado, offsite y restore verificado con RPO/RTO acordados.
- [ ] `audit_cash` de producción revisado y conciliación acordada.
- [ ] Decisión de privacidad; si hay más de un operador independiente,
      separación de contactos implementada.
- [ ] Ensayo de despliegue y reversión sobre copia de producción.
- [ ] Runbook de incidentes, contactos y criterio de stop aprobados.
- [ ] Cobros manuales y devoluciones verificados con el personal del complejo.

### Para producción

No se declara apto mientras falten los puntos anteriores y la migración real.
El cambio de alcance **no elimina** esos bloqueos.

## 10. Reactivación segura de Mercado Pago (segunda etapa)

1. Recuperar el archivo real de `0020_tournament_registration_code_unique_and_more.py`,
   comparar operaciones y crear la unión `0022_merge_...`.
2. Probar el grafo en bases nuevas y en una copia de producción.
3. Cerrar backup programado, offsite y simulacro de restore.
4. Configurar token y secreto de webhook por complejo con credenciales `TEST-`.
5. Ejecutar E2E: aprobado, pendiente, rechazado, devolución, contracargo,
   reintento de webhook y conciliación de caja.
6. Retirar el forzado de `config.settings.prod`, habilitar el interruptor en un
   release controlado y mantener `MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=true`.
7. Verificar que un complejo con token y sin secreto siga bloqueado en modo
   fail-closed.
8. Revisar reservas `pending_payment` antiguas antes de encender la integración.
9. Definir cómo apagar la integración de inmediato si aparece un incidente: el
   interruptor debe volver a `False` y las reservas pendientes revisarse a mano.

## 11. Estado de Git y cierre

- Rama `main`, HEAD `4be83a0c277fb116a452e7abdbaef45c584148bf`.
- Cambios y archivos nuevos **sin commit y sin push**.
- No se hizo deploy, no se migró producción y no se ejecutaron cobros reales.
- `docs/bosquejo-turnos.html`, `docs/bosquejo-turnos.png` y
  `docs/bosquejo-turnos-mobile.png` no fueron modificados.
- La migración faltante sigue ausente y el grafo local sigue siendo
  `0019 → 0020_match_version → 0021`.

**Dictamen:** apto para **ensayo local** y para **staging funcional con datos
ficticios**. **No apto todavía para un piloto restringido con datos reales ni
para producción** mientras falten la migración real, el backup operativo y la
decisión de privacidad. Los pagos online quedan **fuera de alcance** y su
reactivación requiere el procedimiento de la sección 10.
