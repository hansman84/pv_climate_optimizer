"""PV-Budget fuer die Kuehlung (Eigenverbrauch zaehlt mit)."""


def pv_available_for_cooling_w(energy) -> float:
    """PV-Leistung, die der Kuehlung zur Verfuegung steht."""
    if energy is None:
        return 0.0
    pv = max(0.0, float(getattr(energy, "pv_power_w", None) or 0.0))
    export = max(0.0, float(getattr(energy, "export_power_w", None) or 0.0))
    own = max(0.0, float(getattr(energy, "outdoor_unit_power_w", None) or 0.0))
    own += max(0.0, float(getattr(energy, "heat_pump_power_w", None) or 0.0))
    if pv <= 0.0:
        return export
    return max(0.0, min(export + own, pv))
