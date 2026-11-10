# -*- coding: utf-8 -*-
"""Cierre mensual de radicacion.

El ultimo dia habil de cada mes, a partir de la hora de corte (12:00 por
defecto), el portal deja de recibir facturas hasta el primer dia del mes
siguiente: contabilidad cierra el mes con lo radicado hasta ese momento.

Dias habiles: lunes a viernes que no sean festivo en Colombia. Los festivos se
calculan aqui (Ley 51 de 1983, "Ley Emiliani", y los que dependen de la
Pascua) en vez de pedir que alguien los cargue cada ano: todas las instancias
del grupo son colombianas (DECISIONS.md #9). Funciones puras, sin ORM.
"""

from datetime import date, datetime, time, timedelta

import pytz

DEFAULT_TZ = "America/Bogota"
DEFAULT_CUTOFF_HOUR = 12.0

# Festivos de fecha fija que no se mueven.
_FIXED = ((1, 1), (5, 1), (7, 20), (8, 7), (12, 8), (12, 25))
# Festivos que se trasladan al lunes siguiente si no caen en lunes.
_EMILIANI = ((1, 6), (3, 19), (6, 29), (8, 15), (10, 12), (11, 1), (11, 11))


def easter_sunday(year):
    """Domingo de Pascua (algoritmo anonimo gregoriano)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - nombre del algoritmo
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _next_monday(day):
    return day + timedelta(days=(7 - day.weekday()) % 7)


def colombian_holidays(year):
    """Conjunto de fechas festivas en Colombia para el ano dado."""
    holidays = {date(year, month, day) for month, day in _FIXED}
    holidays |= {_next_monday(date(year, month, day)) for month, day in _EMILIANI}
    easter = easter_sunday(year)
    holidays.add(easter - timedelta(days=3))  # Jueves santo
    holidays.add(easter - timedelta(days=2))  # Viernes santo
    holidays.add(_next_monday(easter + timedelta(days=39)))  # Ascension
    holidays.add(_next_monday(easter + timedelta(days=60)))  # Corpus Christi
    holidays.add(_next_monday(easter + timedelta(days=68)))  # Sagrado Corazon
    return holidays


def is_business_day(day):
    return day.weekday() < 5 and day not in colombian_holidays(day.year)


def last_business_day(year, month):
    """Ultimo dia habil del mes."""
    if month == 12:
        day = date(year, 12, 31)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    while not is_business_day(day):
        day -= timedelta(days=1)
    return day


def cutoff_moment(local_day, cutoff_hour=DEFAULT_CUTOFF_HOUR):
    """Fecha y hora local del corte del mes al que pertenece ``local_day``."""
    hours = int(cutoff_hour)
    minutes = int(round((cutoff_hour - hours) * 60))
    return datetime.combine(
        last_business_day(local_day.year, local_day.month), time(hours, minutes)
    )


def radication_closed(now_utc, tz_name=DEFAULT_TZ, cutoff_hour=DEFAULT_CUTOFF_HOUR):
    """Indica si el portal esta cerrado por el corte de fin de mes.

    :param now_utc: ``datetime`` ingenuo en UTC (como ``fields.Datetime.now()``)
    :return: ``(cerrado, corte, reapertura)``; ``corte`` es el momento local del
             corte de este mes y ``reapertura`` el primer dia del mes siguiente.
    """
    try:
        tz = pytz.timezone(tz_name or DEFAULT_TZ)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone(DEFAULT_TZ)
    local_now = pytz.utc.localize(now_utc).astimezone(tz).replace(tzinfo=None)
    cutoff = cutoff_moment(local_now.date(), cutoff_hour)
    if local_now.month == 12:
        reopen = date(local_now.year + 1, 1, 1)
    else:
        reopen = date(local_now.year, local_now.month + 1, 1)
    return local_now >= cutoff, cutoff, reopen
