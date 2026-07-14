# Per-device PV budget and learned electrical demand

## Goal

The current `sensor.export_power_raw` is the **whole-house net export**. It
should remain the authoritative boundary: climate control may consume only a
conservative portion of it. A room's `cooling_power_entity_id` is thermal
output in BTU/h, so it must never be used as electrical demand in watts.

The extension should estimate, per confirmed indoor unit, the electrical
increment that is needed to run at the requested HVAC mode and target. It
must not pretend to know the value until enough observations exist.

## Safe staged design

1. **Direct metering preferred.** Configure an optional electrical-power
   sensor (W) for each indoor unit or its circuit. This becomes the primary
   consumption source.
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
