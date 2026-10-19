"""Turnos de cancha: horarios configurables por complejo y disponibilidad.

Reglas:
- Cada complejo define horarios por tipo de día (hábil, sábado, domingo) con
  primer turno, cierre y duración del turno (1,5 h por defecto).
- Las pausas cortan la secuencia: al terminar la pausa, los turnos continúan
  con la misma duración (media hora o una hora de pausa no cambia el ritmo).
- Las fechas especiales (feriados, eventos) pisan la configuración del día:
  pueden cerrar el complejo o fijar un horario propio.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, time as time_cls, timedelta

from django.utils import timezone

from core.models import CourtBooking, CourtDateOverride, CourtSchedule

DEFAULT_START = time_cls(9, 0)
DEFAULT_END = time_cls(23, 0)
DEFAULT_SLOT_MINUTES = 90

# Fecha ancla para sumar minutos a un ``time`` (los turnos no cruzan medianoche).
_ANCHOR = date_cls(2000, 1, 1)


def day_type_for(day: date_cls) -> str:
    """Tipo de configuración que le corresponde a una fecha."""
    weekday = day.weekday()
    if weekday == 5:
        return "saturday"
    if weekday == 6:
        return "sunday"
    return "weekday"


def _dt(value: time_cls) -> datetime:
    return datetime.combine(_ANCHOR, value)


class DayConfig:
    """Configuración efectiva de un día concreto (con feriados aplicados)."""

    __slots__ = ("is_open", "start", "end", "slot_minutes", "pauses", "override", "schedule")

    def __init__(
        self,
        *,
        is_open: bool,
        start: time_cls,
        end: time_cls,
        slot_minutes: int,
        pauses: list[tuple[time_cls, time_cls]],
        override: CourtDateOverride | None,
        schedule: CourtSchedule | None,
    ) -> None:
        self.is_open = is_open
        self.start = start
        self.end = end
        self.slot_minutes = slot_minutes
        self.pauses = pauses
        self.override = override
        self.schedule = schedule


def day_config(complex_obj, day: date_cls) -> DayConfig:
    """Combina horario del tipo de día + fecha especial (si existe)."""
    override = CourtDateOverride.objects.filter(complex=complex_obj, date=day).first()
    if override is not None and override.is_closed:
        schedule = CourtSchedule.objects.filter(complex=complex_obj, day_type=day_type_for(day)).first()
        return DayConfig(
            is_open=False,
            start=schedule.start_time if schedule else DEFAULT_START,
            end=schedule.end_time if schedule else DEFAULT_END,
            slot_minutes=schedule.slot_minutes if schedule else DEFAULT_SLOT_MINUTES,
            pauses=[],
            override=override,
            schedule=schedule,
        )

    schedule = CourtSchedule.objects.filter(complex=complex_obj, day_type=day_type_for(day)).first()
    start = schedule.start_time if schedule else DEFAULT_START
    end = schedule.end_time if schedule else DEFAULT_END
    slot_minutes = schedule.slot_minutes if schedule else DEFAULT_SLOT_MINUTES
    is_open = schedule.is_open if schedule else True
    if override is not None:
        if override.start_time:
            start = override.start_time
        if override.end_time:
            end = override.end_time
    pauses: list[tuple[time_cls, time_cls]] = []
    if schedule is not None and is_open:
        pauses = [
            (pause.start_time, pause.end_time)
            for pause in schedule.pauses.all()
            if pause.end_time > start and pause.start_time < end
        ]
    return DayConfig(
        is_open=is_open,
        start=start,
        end=end,
        slot_minutes=slot_minutes,
        pauses=pauses,
        override=override,
        schedule=schedule,
    )


def slots_for_day(complex_obj, day: date_cls) -> list[tuple[time_cls, time_cls]]:
    """Turnos del día: secuencia de duración fija, salteando las pausas."""
    config = day_config(complex_obj, day)
    if not config.is_open:
        return []

    duration = timedelta(minutes=config.slot_minutes)
    cursor = _dt(config.start)
    closing = _dt(config.end)
    pauses = [(_dt(p_start), _dt(p_end)) for p_start, p_end in sorted(config.pauses)]

    slots: list[tuple[time_cls, time_cls]] = []
    while cursor + duration <= closing:
        candidate_end = cursor + duration
        overlap = next(((p_start, p_end) for p_start, p_end in pauses if cursor < p_end and candidate_end > p_start), None)
        if overlap is not None:
            # La pausa corta la secuencia: el siguiente turno arranca al terminar.
            cursor = overlap[1] if overlap[1] > cursor else cursor + duration
            continue
        slots.append((cursor.time(), candidate_end.time()))
        cursor = candidate_end
    return slots


def slot_for(complex_obj, day: date_cls, start: time_cls) -> tuple[time_cls, time_cls] | None:
    """Devuelve el turno (inicio, fin) si esa hora existe en la grilla del día."""
    for slot_start, slot_end in slots_for_day(complex_obj, day):
        if slot_start == start:
            return slot_start, slot_end
    return None


def is_slot_past(day: date_cls, start: time_cls, now=None) -> bool:
    """True si el turno ya empezó (hora local) o la fecha pasó."""
    now = now or timezone.localtime()
    if day != now.date():
        return day < now.date()
    return start <= now.time()


def bookings_for_day(complex_obj, day: date_cls, *, include_cancelled: bool = False) -> list[CourtBooking]:
    qs = CourtBooking.objects.filter(complex=complex_obj, date=day).select_related("court")
    if not include_cancelled:
        qs = qs.exclude(status="cancelled")
    return list(qs)


def month_calendar(
    complex_obj,
    month: date_cls,
    *,
    today: date_cls,
    last_day: date_cls,
    selected: date_cls | None = None,
) -> list[list[dict]]:
    """Grilla del mes (semanas que empiezan el lunes) con el estado de cada día.

    Cada celda trae: ``date``, ``in_month``, ``selectable`` (dentro de la ventana
    de reserva), ``is_open`` (el complejo atiende), ``is_today`` e ``is_selected``.
    """
    first = month.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    last = next_month - timedelta(days=1)

    schedules = {schedule.day_type: schedule for schedule in CourtSchedule.objects.filter(complex=complex_obj)}
    overrides = {
        override.date: override
        for override in CourtDateOverride.objects.filter(complex=complex_obj, date__range=(first, last))
    }

    def is_open_on(day: date_cls) -> bool:
        override = overrides.get(day)
        if override is not None:
            return not override.is_closed
        schedule = schedules.get(day_type_for(day))
        return schedule.is_open if schedule else True

    weeks: list[list[dict]] = []
    cursor = first - timedelta(days=first.weekday())
    while cursor <= last:
        week = []
        for _ in range(7):
            in_month = cursor.month == first.month and cursor.year == first.year
            selectable = in_month and today <= cursor <= last_day
            week.append(
                {
                    "date": cursor,
                    "number": cursor.day,
                    "in_month": in_month,
                    "selectable": selectable,
                    "is_open": is_open_on(cursor),
                    "is_today": cursor == today,
                    "is_selected": cursor == selected,
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)
    return weeks


def board_for_day(complex_obj, day: date_cls, *, now=None) -> dict:
    """Tablero listo para plantillas: filas por horario con una celda por cancha."""
    config = day_config(complex_obj, day)
    slots = slots_for_day(complex_obj, day)
    courts = list(complex_obj.courts.filter(is_active=True).order_by("name"))
    bookings = {(booking.court_id, booking.start_time): booking for booking in bookings_for_day(complex_obj, day)}
    now = now or timezone.localtime()

    rows = []
    for start, end in slots:
        past = is_slot_past(day, start, now)
        cells = []
        for court in courts:
            booking = bookings.get((court.id, start))
            cells.append(
                {
                    "court": court,
                    "booking": booking,
                    "available": booking is None and not past,
                    "past": past and booking is None,
                }
            )
        rows.append({"start": start, "end": end, "cells": cells, "past": past})

    return {
        "config": config,
        "slots": slots,
        "courts": courts,
        "rows": rows,
        "bookings": [booking for booking in bookings.values() if booking.status != "cancelled"],
    }
