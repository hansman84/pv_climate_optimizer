# Per-device PV budget and learned electrical demand

## Implemented first stage: shared outdoor-unit meter

`sensor.total_backup_power` is the confirmed meter for the complete
multi-split outdoor unit. Configure it under **PV & Energiepolitik → Leistung
der Klima-Außeneinheit**. The controller records a value only after the same
set of cooling rooms has run for five minutes and at most once every five
minutes. It retains a bounded history of 24 values per active-room set.

For a candidate room, it compares the medians of the stable sets *without*
and *with* that room. The difference plus a 15% margin is its learned
incremental demand. The `… – gelernter PV-Bedarf` sensor exposes
`PV-Mindestüberschuss + incremental demand`; until both sets have three valid
samples its value remains unavailable and reports `insufficient_history`.

This phase is diagnostic and Shadow-only for all rooms other than the already
separately approved Wohnzimmer pilot. It deliberately does not infer a value
from BTU/h cooling sensors and does not send new climate commands.

## Goal

The current `sensor.export_power_raw` is the **whole-house net export**. It
should remain the authoritative boundary: climate control may consume only a
conservative portion of it. A room's `cooling_power_entity_id` is thermal
output in BTU/h, so it must never be used as electrical demand in watts.

The extension should estimate, per confirmed indoor unit, the electrical
increment that is needed to run at the requested HVAC mode and target. It
must not pretend to know the value until enough observations exist.

## Safe staged design

1. **Shared meter preferred.** Use the confirmed total outdoor-unit meter.
   Per-indoor-unit circuit meters remain optional refinements, not a
   prerequisite for the conservative active-set method.
2. **Observed fallback.** Where no meter exists, learn only from stable
   windows: no other climate command, no large household-load transition,
   valid net-export reading, and at least five minutes before and after a
   confirmed climate-state transition. Estimate `delta_w` from the median
   export drop, not from a single sample.
3. **Conservative envelope.** Store a separate estimate for each unit and
   operating bucket (`cool`, target-temperature band, fan band). Keep the
   80th-percentile observed input plus a safety margin. Until a bucket has
   sufficient samples, label it `insufficient_history` and use an explicit
   configured fallback or do not start an additional unit.
4. **Budget allocation.** Available budget is `max(0, normalized_house_export
   - reserve_w)`. A start is allowed only when it covers the estimate for the
   candidate unit plus the reserve. Existing running units retain their
   protected minimum runtime; a new room never steals budget by stopping a
   manually controlled room.
5. **Learning guardrails.** Exclude intervals with unavailable entities,
   stale EMS, manual overrides, compressor startup spikes, or another pilot
   transition. Persist only aggregates and sample counts, never raw household
   histories.

## Example: Speis plus Wohnzimmer

- Speis is already cooling at 24 °C. Its confirmed electrical estimate is
  420 W.
- House export is 1,250 W and the safety reserve is 250 W.
- Available climate budget is 1,000 W; after Speis, 580 W remains.
- Wohnzimmer's learned 22 °C estimate is 650 W. The controller does **not**
  start it yet.
- Once export reaches at least 1,320 W (420 + 650 + 250), the controller may
  request Wohnzimmer cooling, subject to EMS, stabilisation, and rate limits.

This is a planning extension for a later multi-zone approval gate. It does
not change the current living-room-only pilot or issue any new climate calls.
