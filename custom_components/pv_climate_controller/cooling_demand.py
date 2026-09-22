"""Echter Kuehlbedarf statt PV-Erlaubnis (Hauswunsch 2026-09-22).

PV ist eine Erlaubnis, kein Grund: gekuehlt wird nur, wenn die Sonne
nennenswert einstrahlt ODER die Aussenluft warm ist. Sonst bleibt es aus
(bei kuehlem Wetter macht das Fenster die Arbeit).
"""

IRRADIANCE_DEMAND_W_M2 = 150.0
OUTDOOR_DEMAND_C = 20.0


def cooling_demand(room) -> bool:
    """True, wenn ein echter Waermegrund vorliegt."""
    irr = getattr(room, "solar_irradiance_w_m2", None)
    if irr is not None and float(irr) >= IRRADIANCE_DEMAND_W_M2:
        return True
    snap = getattr(room, "snapshot", None)
    raw = getattr(snap, "outdoor_temperature", None) if snap is not None else None
    out = getattr(raw, "value", raw)
    if isinstance(out, bool) or not isinstance(out, (int, float)):
        out = None
    if out is not None and float(out) >= OUTDOOR_DEMAND_C:
        return True
    if irr is None and out is None:
        return True
    return False
