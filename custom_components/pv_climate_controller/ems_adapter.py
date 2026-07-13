"""Generic EMS request/grant safety contract.

The adapter does not know or configure Loxone. It only accepts a configured
Home Assistant state value supplied by the integration runtime.
"""

from __future__ import annotations

from .models import EMSGrant


def parse_grant(value: object, age_s: float | None, stale_after_s: float) -> EMSGrant:
    """Fail closed for missing, invalid, negative, or stale capacity grants."""
    if age_s is None or age_s > stale_after_s:
        return EMSGrant(0, False, "ems_grant_stale", "EMS-Freigabe fehlt oder ist veraltet.")
    try:
        numeric = int(float(str(value)))
    except (TypeError, ValueError):
        return EMSGrant(0, False, "ems_grant_invalid", "EMS-Freigabe ist ungültig.")
    if numeric < 0:
        return EMSGrant(0, False, "ems_grant_invalid", "EMS-Freigabe darf nicht negativ sein.")
    return EMSGrant(numeric, True, "ems_grant_ok", f"EMS gibt {numeric} Klimastufe(n) frei.")


def requested_stages(demand: bool) -> int:
    """Gate E supports one explicitly configured zone only."""
    return 1 if demand else 0
