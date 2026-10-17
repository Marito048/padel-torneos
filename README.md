# PadEl Torneos

Sistema multi-complejo de gestión de torneos de pádel.
Stack: Django 5.2 + Postgres + WhiteNoise. En local corre con Docker Compose (app + Postgres + Caddy)
y en el LXC Debian de Proxmox corre nativo (gunicorn + systemd + Postgres del sistema).

## Funcionalidades

- **Multi-complejo**: cada complejo tiene sus torneos, canchas y usuarios autorizados.
  Cada usuario del panel puede crear y editar su propio complejo desde Configuración general;
  el único dato compartido entre complejos es la base de jugadores.
  El acceso a datos se valida siempre server-side (URL/sesión manipuladas no otorgan acceso).
- **Panel de gestión por torneo** con secciones: Resumen, Parejas, Zonas, Partidos,
  Resultados, Clasificación y Llaves eliminatorias. El torneo seleccionado se guarda en sesión.
- **Parejas**: un jugador no puede integrar dos duplas del mismo torneo (validación en UI,
  en servidor y constraint en base de datos). Puede jugar otros torneos. El nombre de la
  dupla se completa solo con los apellidos de los jugadores (por defecto «Apellido1 - Apellido2»)
  y se puede personalizar desde el panel. Las sugerencias de jugadores se filtran por la
  categoría del torneo, con opción para ver todas las categorías.
- **Jugadores reutilizables**: al cargar una pareja se busca en la base de jugadores
  (tolerante a acentos y orden del nombre); los jugadores nuevos se crean desde el panel y
  quedan guardados para los próximos torneos, sin pasar por el admin.
- **Sección Jugadores**: listado con búsqueda y filtros por estado y categoría, alta directa en
  la base,
  edición de datos y contacto, activar/desactivar, **eliminación de fichas sin datos de
  competencia** (bloqueada si el jugador está en un torneo o tiene partidos) y
  **unificación de fichas duplicadas** (mueve duplas, inscripciones, zonas y partidos a la
  ficha que se conserva; se bloquea si ambos jugadores ya comparten torneo o forman dupla
  entre sí).
- **Eliminar torneo**: desde Configuración o la lista de torneos, con confirmación.
  Se borran zonas, partidos, resultados y llaves; las parejas y los jugadores se conservan.
- **Zonas**: cada dupla pertenece a una sola zona; los partidos de zona se generan solos.
  La composición se arma con el **sorteo automático** (auditado) o **a mano** desde la solapa
  Zonas, eligiendo la zona de cada pareja y creando zonas nuevas; cada zona necesita entre 2 y
  las duplas configuradas, las que quedan vacías se descartan y las parejas con resultados
  cargados no se pueden mover.
- **Resultados**: se validan sets/games según el reglamento (6-0 a 6-4, 7-5, 7-6; super
  tie-break a 11 con diferencia de 2). El marcador se escribe siempre con la primera pareja;
  el ganador se deduce del marcador o se elige a mano (si se elige, el marcador se acomoda
  a esa pareja cuando vino espejado). Walkover exige ganador y motivo, sin sets ficticios.
  Queda registrado quién cargó cada resultado.
- **Clasificación**: oficial sólo con todos los partidos de zona válidos; desempates por
  puntos, diferencia de sets, diferencia de games y sorteo auditable (nunca head-to-head).
  Los sorteos quedan registrados en `TieBreakAudit` y no se sobrescriben.
- **Llaves automáticas**: se generan al cerrar la última zona válida (o con el botón
  "Generar llaves"). Árbol completo con descansos (byes) para las mejores clasificadas,
  cruces 1º vs 2º evitando misma zona cuando es posible, sin partido por el tercer puesto.
  Soportan hasta 64 duplas: cuartos, octavos, dieciseisavos y treintaidosavos, con los
  ganadores avanzando automáticamente ronda a ronda hasta la final. Idempotente y nunca
  sobrescribe resultados disputados.
- **Vista de llaves**: el apartado Llaves ofrece un **cuadro gráfico estilo fixture**
  (todas las rondas eliminatorias, sin la fase de grupos, en columnas unidas por líneas, con
  el nombre de cada dupla, la zona y posición de origen de las clasificadas, ganador marcado
  con ✓ y el marcador) y una **lista de partidos** alternativa. Las clasificadas por zona se
  muestran arriba del cuadro.
- **Cruces a mano**: además de la siembra automática, «Armar cruces a mano» permite definir los
  enfrentamientos de la primera ronda entre las clasificadas (los descansos se dejan con un
  lugar libre); el ganador sigue avanzando solo ronda a ronda. La llave armada a mano se
  conserva mientras la clasificación no cambie y no tenga resultados cargados.
- **Correcciones controladas**: se bloquean si algún partido dependiente ya tiene resultado;
  si no hay datos, la llave se recalcula sola en una transacción.
- **Login con Google (opcional)**: preparado con django-allauth. Se activa cargando
  `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET` en `.env`; aparece el botón "Ingresar con Google".
  El registro abierto está deshabilitado: el acceso se otorga por invitación. El administrador
  general carga el email de Google en **Invitaciones por email** (Configuración general) y,
  cuando la persona ingresa con Google, su cuenta se crea sola: las invitaciones con complejo
  entran directo a él y las que no lo traen piden nombre, apellido y usuario para crear el
  complejo propio. Quien no usa Google puede pedir su cuenta desde **Registrarse** en el login:
  deja su nombre, teléfono, email y los datos de su complejo (nombre, ciudad y dirección); el
  administrador general verifica y la crea desde Configuración general → **Solicitudes de
  registro**, con todos los datos precargados y la opción de crear el complejo en el mismo paso
  y asignarlo. Hay envío opcional de usuario y contraseña por email y aviso automático a los
  administradores cuando entra la solicitud (email y/o WhatsApp, ver más abajo). Cuando la
  contraseña la entregó la administración, el panel recuerda cambiarla al ingresar.
- **Configuración general**: sección propia (fuera del torneo, botón «Usuarios» en el panel y
  «Configuración general» en el listado) para crear y editar los complejos que administra cada
  usuario y para agregar usuarios del panel y modificar los existentes: nombre, email,
  contraseña, activar/desactivar, acceso al panel y complejos.
  Cada encargado administra sólo los usuarios de sus complejos; no puede tocar administradores
  generales, ni desactivarse/quitase permisos a sí mismo; nadie puede quedar sin complejo.
  El administrador general además gestiona las invitaciones por email de Google (con o sin
  complejo asignado), el historial de las ya usadas y las solicitudes de registro.
  Las contraseñas piden un mínimo de 8 caracteres, sin exigencias de mayúsculas ni símbolos;
  cuando las entrega la administración, el panel recuerda cambiarlas al ingresar y la ficha
  del usuario permite enviarlas por email junto con el nombre de usuario.

## Formato de torneo (dominio)

- Zonas (grupos) configurables (por defecto 3 duplas; el panel permite 2..N).
- Cada zona juega round robin; clasifican las mejores `qualifiers_per_zone` (por defecto 2).
- Partido al mejor de `best_of_sets` (2 por defecto); si cada pareja gana un set se define
  con super tie-break a 11 puntos (diferencia de 2; en torneos al mejor de 3 o más también
  vale jugar el set completo).
- Formato `zonas_playoffs` (con llaves) o `round_robin` (sólo zonas).
- Resultado como texto `"6-4 4-6 [11-9]"`; el ganador se deduce del marcador o se
  selecciona (si se selecciona, el marcador se acomoda a la pareja elegida).

## Inicio rápido (dev)

```bash
cp .env.example .env   # editar valores
docker compose up --build
docker compose exec app python manage.py createsuperuser
# http://localhost (caddy) o http://localhost:8000 (app directa)
```

## Tests

```bash
docker compose run --rm -v "$PWD/app:/app" app python manage.py test core
```

Cubren validación de resultados, disponibilidad de jugadores, zonas, partidos, desempates
auditables, tamaños de llave 2..32 (incluye 5, 6, 7, 9, 10, 11, 17 y 24 con descansos,
octavos de 16 y dieciseisavos de 32), progresión automática hasta la final, panel de
resultados con cruces en espera, correcciones y aislamiento por complejo.

## Producción (LXC)

Cambiar `.env` (DEBUG=False, ALLOWED_HOSTS, etc.), apuntar DNS a la IP pública, abrir puertos 80/443.

**Con ngrok** (túnel sin abrir puertos): `ngrok http 80` (o `ngrok http 8000` directo a la app).
Recomendado usar el dominio estático gratuito de ngrok para que la URL no cambie. En modo dev
`ALLOWED_HOSTS=["*"]` deja entrar por el dominio de ngrok y, al ser mismo origen, los formularios
funcionan sin configuración extra.

Para Google OAuth:
1. En Google Cloud Console → APIs y servicios → Credenciales → «Crear ID de cliente OAuth»
   (tipo Aplicación web). Origen autorizado: `https://<tu-dominio>` (el de ngrok o el propio).
   URI de redirección: `https://<tu-dominio>/cuentas/google/login/callback/`.
2. Cargar `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET` en `.env` y reiniciar (`docker compose up -d`).
   Los campos ya están reservados al final de `.env`.
3. El botón "Ingresar con Google" aparece solo cuando ambas credenciales están cargadas.
   Entran los usuarios creados a mano (email coincidente o vinculación desde el panel) y los
   invitados: el administrador general carga el email en Configuración general → Invitaciones
   y la cuenta se crea al ingresar con Google. El registro abierto queda deshabilitado.
4. Si la URL de ngrok cambia, actualizá el URI de redirección en Google Cloud Console
   (con el dominio estático de ngrok esto no pasa).

## Email saliente (opcional)

El aviso «tu cuenta está lista» (usuario + contraseña temporal) usa el email de Django. Sin
configuración, los mensajes se imprimen en la consola del servicio
(`journalctl -u padel-torneos`) y no salen a Internet. Para que lleguen de verdad, cargar en
`.env` y reiniciar:

```bash
EMAIL_HOST=smtp.gmail.com           # cualquier SMTP sirve
EMAIL_PORT=587
EMAIL_HOST_USER=tu-cuenta@gmail.com
EMAIL_HOST_PASSWORD=clave-de-aplicacion   # Gmail: Cuenta → Seguridad → Contraseñas de aplicación
DEFAULT_FROM_EMAIL=Pádel Server <tu-cuenta@gmail.com>
```

### Avisos de nuevas solicitudes de registro

Cuando entra una solicitud desde «Registrarse» (con los datos de la persona y del complejo), el
sistema avisa por:

- **Email** a los administradores generales activos con email cargado, más la dirección extra
  `SIGNUP_ALERT_EMAIL` si está definida. Sin SMTP configurado el aviso queda impreso en el
  journal del servicio (`journalctl -u padel-torneos`).
- **WhatsApp** vía [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/):
  guardar el contacto **+34 644 51 95 23** en WhatsApp, mandarle el mensaje
  «I allow callmebot to send me messages» y anotar la apikey que contesta. Después cargar en
  `.env` `WHATSAPP_ALERT_PHONE=+549341XXXXXXX` (con código de país) y `WHATSAPP_ALERT_APIKEY=...`,
  y reiniciar. Es un servicio gratuito de terceros; si preferís algo oficial está la API de
  WhatsApp Business (Meta) o Twilio, con más trámite.

En cualquier caso la solicitud siempre queda listada en Configuración general → Solicitudes de
registro.

## Alojamiento en el LXC (192.168.0.200)

Además del entorno Docker local, el proyecto quedó alojado en el LXC Debian (`padel@192.168.0.200`)
en `/home/padel/padel-torneos`, pero **sin Docker**: el LXC no tiene `nesting` habilitado en
Proxmox, así que corre nativo con el Postgres 17 del sistema, un virtualenv (`.venv`) y gunicorn
bajo systemd.

- URL en la red local: `http://192.168.0.200:8080` (el puerto 80 lo usa el nginx del LXC, que no
  se toca). También entra por Tailscale si se usa el IP del tailnet.
- Servicio: `sudo systemctl status|restart|stop padel-torneos` (habilitado al boot).
- Código: `/home/padel/padel-torneos/app`; venv en `/home/padel/padel-torneos/.venv`.
- Base de datos: `padel_torneos` en el Postgres del sistema (rol `padel_torneos`; la conexión
  está en `.env` → `DATABASE_URL`). La base `padel` pre-existente (tablas `clubes`/`torneos`,
  sin datos) quedó intacta.
- Actualizar código desde la PC de desarrollo:
  `rsync -az --exclude=__pycache__ --exclude='*.pyc' ~/padel-torneos/ padel@192.168.0.200:padel-torneos/`
  y después `sudo systemctl restart padel-torneos`.
- Docker (`docker.io` + `docker-compose`) quedó instalado pero deshabilitado. Si algún día se
  habilita `nesting=1,keyctl=1` en Proxmox, se puede volver al flujo `docker compose` de local.
- Datos iniciales: copia restaurada del dump
  `backups/padel_backup_20261016_003339_handoff_lxc.sql` (3 usuarios, 2 torneos, 106 jugadores,
  12 parejas). Desde ese momento cada entorno tiene su propia base: lo que se cargue en uno
  **no** aparece en el otro.
- Para exponer el LXC con ngrok: instalar ngrok ahí y correr `ngrok http 8080`.

## Backups

Los respaldos SQL viven en `backups/`. Antes de las migraciones estructurales se generaron
`padel_backup_20261015_172715.sql`, `padel_backup_20261015_213908.sql`,
`padel_backup_20261015_221006.sql` y, tras el deploy de cruces manuales/complejos
propios (migración `0007_tournament_manual_bracket`), `padel_backup_20261015_232943.sql`
(todos verificados con sha256). Para el traspaso al LXC se generaron
`padel_backup_20261016_002145_handoff_lxc.sql` y, con el estado final antes de restaurar,
`padel_backup_20261016_003339_handoff_lxc.sql` (este último es el que quedó cargado en el LXC).
