"""Sunrise and sunset times, without any dependencies.

The standard "sunrise equation" (as used by NOAA's calculator, simplified):
good to about a minute anywhere people live, which is plenty for "turn the
porch lights on 15 minutes after sunset".  Returns ``None`` on days the sun
does not rise or set (polar summer and winter).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

_J2000 = 2451545.0
_UNIX_EPOCH_JD = 2440587.5


def _to_unix(jd: float) -> float:
    return (jd - _UNIX_EPOCH_JD) * 86400.0


def sun_times(day: date, lat: float, lon: float) -> Tuple[Optional[datetime], Optional[datetime]]:
    """``(sunrise, sunset)`` as timezone-aware UTC datetimes for the local ``day``.

    ``lat``/``lon`` are decimal degrees, east and north positive.
    """
    # Julian day number at local solar noon of that calendar day.
    noon_utc = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc) - timedelta(hours=lon / 15.0)
    jd = noon_utc.timestamp() / 86400.0 + _UNIX_EPOCH_JD
    n = math.ceil(jd - _J2000 + 0.0008 - 0.5)
    j_star = n - lon / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)
    c = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    j_transit = _J2000 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if cos_w < -1 or cos_w > 1:
        return None, None
    w = math.degrees(math.acos(cos_w))
    rise = datetime.fromtimestamp(_to_unix(j_transit - w / 360.0), tz=timezone.utc)
    sets = datetime.fromtimestamp(_to_unix(j_transit + w / 360.0), tz=timezone.utc)
    return rise, sets
