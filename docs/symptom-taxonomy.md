# carmind - symptom taxonomy (draft)

> The fixed vocabulary that a planned extraction step would map manual facts *into*. It has to
> be settled first, so that every manual normalizes to one set instead of inventing labels.
>
> A "symptom" is something a call-center operator can observe or be told over the phone. It
> is not a diagnosis and not a fleet disposition. Each symptom routes to one or more of our three
> outputs: **meaning** (indicator lookup), **operator_fix** (procedure), **drivability** (gate).
>
> 13 top-level categories (match the schema enum). `id` = stable kebab key.

---

## 1. `wont_start` - vehicle won't crank / won't power up
- `no-crank-dead` - nothing happens, no lights -> battery/power
- `no-crank-clicks` - clicks, won't turn over
- `cranks-no-fire` - turns over, won't catch
- `immobilizer-block` - anti-theft / code not accepted (aftermarket or factory immobilizer)
- `steering-lock` - key/start won't turn, wheel locked
- `gear-not-in-park` - start inhibited, not in P/N
- `brake-not-pressed` - push-start needs brake fully depressed
- `key-fob-dead` - fob battery flat, no recognition (-> procedure: fob-touch start)
- `ev-no-power` - EV won't power up / no "ready"

## 2. `wont_turn_off` - engine/system won't shut down
- `engine-wont-stop` - start/stop won't shut off (e.g. **brake-to-floor** requirement)
- `accessory-stays-on` - electronics won't power down
- `auto-stop-confusion` - idle stop-start mistaken for a fault (informational)

## 3. `wont_lock` - locking / access
- `wont-lock-running` - can't lock while engine on (verify off)
- `central-lock-fail` - central locking unresponsive
- `locked-out` - keys/fob locked inside
- `wont-unlock` - won't open

## 4. `warning_light` - dashboard indicator illuminated
> Primary path = **indicator lookup** (meaning + color + severity), then maybe procedure/gate.
- `engine-mil` - check-engine / MIL (steady vs flashing matters)
- `oil-pressure` - oil light (steady vs flashing)
- `coolant-temp` - overheat / coolant
- `battery-charge` - charging-system light
- `abs` - ABS light
- `brake-system` - brake / handbrake warning
- `airbag-srs` - airbag/SRS (red vs amber)
- `tpms` - tire-pressure light
- `power-steering` - EPS light
- `traction-esp` - traction/ESP/skid light
- `glow-plug` - diesel preheat
- `adblue-urea` - AdBlue/DEF warning
- `transmission` - gearbox / overheat
- `service-due` - maintenance reminder (informational)
- `generic-warning` - unknown symbol (-> request photo, icon-match)

## 5. `noise` - abnormal sound
- `brake-noise` - squeal/grind on braking
- `knocking` - engine knock/tap
- `whine-hum` - drivetrain/bearing
- `rattle` - loose/underbody
- `belt-squeal` - accessory belt

## 6. `fluid` - leak / level
- `oil-leak`
- `coolant-loss`
- `fuel-smell`
- `brake-fluid`
- `unknown-puddle`

## 7. `tire` - wheels / tires
- `flat-puncture` - single flat
- `multi-flat` - 2+ damaged
- `tpms-low` - pressure warning only
- `blowout`
- `wheel-damage` - curb/pothole, rim

## 8. `electrical` - non-warning-light electrical
- `lights-out` - head/tail/indicator bulb (LED != field-replaceable)
- `wipers-fail`
- `windows-stuck`
- `no-infotainment`
- `fob-key-battery` - remote dead (overlaps `wont_start`)

## 9. `drivetrain` - power delivery / handling
- `no-power` - won't pull / stutters / limp mode
- `stalls-driving` - cuts out in motion
- `gear-stuck` - won't shift / stuck in gear
- `vibration` - under drive or braking
- `ev-wont-pull` - low charge, won't move (-> drivability gate)

## 10. `hvac` - climate
- `ac-not-cooling`
- `heater-fail`
- `defrost-fail` - safety-relevant (visibility)

## 11. `glass` - windows / windshield / mirrors
- `windshield-crack` - spreading vs stable (photo)
- `windshield-shatter`
- `window-wont-close`
- `mirror-fault`

## 12. `keys` - keys / fobs / immobilizer hardware
- `key-lost`
- `key-broken`
- `fob-unresponsive`
- `immobilizer-code` - code entry needed (overlaps `wont_start.immobilizer-block`)

## 13. `other` - catch-all (must be triaged, never the default)
- `accident-damage` - collision (drivability + photo)
- `flood-water` - water ingress
- `break-in` - attempted theft / forced entry
- `smell-smoke` - burning smell / smoke (urgent)
- `unknown` - operator can't classify -> escalate / request photo

---

## Notes
- **Overlaps are intentional** (e.g. immobilizer appears under `wont_start` and `keys`).
  Symptoms can carry secondary categories; extraction would pick the primary.
- **Severity & color live on the indicator**, not the symptom - a symptom is the entry point.
- **Drivability is never inferred from the symptom alone** - it comes from a per-vehicle
  `drivability_gate` fact in the manual. Symptom only routes there.
- Anything an operator can't place -> `other.unknown` -> request photo / escalate. The taxonomy
  must never silently absorb an unknown into a wrong bucket.
