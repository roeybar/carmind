# carmind - resolution order (engine precedence)

Draft design. This fixes the order in which the engine turns (observation + severity +
context) into a recommendation. As more domains are worked through (overheating, steering,
warning-light combinations) the steps stay the same and the rules under them grow.
Schema: `schema/knowledge.schema.json`. Examples: `docs/example-*.json`.

Implemented today in `serve/app/tools.py`: step 1 plus a severity-to-verdict mapping
(`drivability_assessment`), and step 2 for context flags sent with a request
(`apply_context`; the request field is `context_flags`, see `serve/README.md`). Triage,
trees and the legal overlay are target design, not code.

---

## Inputs to a single query

- **Vehicle**, resolved to car-type facts (`drivetrain`, `tire_provision`, EV flags, manual_refs).
- **Observation**: symptom, dashboard icon or verbatim dash message, from the customer.
- **Context flags**: `repeat_problem`, `recent_long_service`, `near_service_interval`,
  `fresh_handover`, `no_pre_delivery_test`, `after_service_hours`. Supplied by the fleet
  operator per query and never stored.
- **Service-window config**: the fleet operator's resource availability (e.g. tire-shop hours).

## Output is two stacked layers

1. **The fleet's own procedure** (`fleet_procedure`, `owner=fleet_operator`), rendered first,
   verbatim and attributed. carmind does not author it.
2. **carmind assessment**: the result of the walk below.

---

## The walk (first step that terminates wins)

0. **Resolve car-type facts.** Load the vehicle's manufacturer facts before anything else
   (e.g. `tire_provision` must be known before "no spare" can be judged).

1. **Manufacturer hard stop.** Indicator `hard_stop=true` / `severity=stop_now`
   ("Stop immediately, service vehicle") gives `not_drivable`, then tow. All triage is
   skipped and no field fix is attempted.

2. **Context-rule override.** A `context_rule` fires (e.g. `repeat_problem` -> tow;
   `recent_long_service` + serious symptom -> tow). History overrides current-symptom
   optimism. Context can only make the result more conservative; it never green-lights an
   unsafe car.

3. **Triage (linear non-faults).** Run the symptom's ordered `triage_check` chain: the cheap
   "the car is actually fine" gate (door ajar, fob battery, broken start sequence). If it
   resolves, guide the fix and stop; no tow.

4. **Diagnostic tree (branching).** Walk the symptom's `diagnostic_tree`. Discriminating
   questions fork to outcomes. Car-type facts are checked inside (e.g. `tire_provision` before
   "no spare"). High-liability trees need a conservative `default_branch` and `signed_off`.

5. **Drivability trio.** Judge to the legal/safety threshold, not perfection: braking,
   acceleration, steering. Result: `drivable` / `drivable_caution` / `not_drivable`.

6. **Legal-disqualifier overlay.** Any `basis=legal` condition (burnt required light, broken
   mirror glass, plate) means the car is currently illegal to drive. But:
   - field-fixable (lamp, wiper): `field_fix` resolves it, drivable after the fix.
   - not field-fixable: `not_drivable`.
   A legal disqualifier is not automatically a tow.

7. **Multi-fault bundling.** On a car that drives, collect all `field_fix` items and
   `schedule` items into one roadside dispatch plus scheduled work. Don't tow a drivable car
   that can be fixed at the roadside.

8. **Time / resource gate.** Apply `after_service_hours` and the service-window config. This
   may turn a `schedule` (tire center) into a `tow` (no afternoon tire shops). It comes last
   because it depends on the outcome above.

9. **Final recommendation:** `drive_ok` / `field_fix` (+`schedule`) / `schedule` /
   `not_drivable` -> tow / `escalate`. The fleet operator executes the dispatch.

---

## Invariants (hold across every domain)

- Hard stops and legal disqualifiers are checked before any "fine" path.
- Uncertainty routes away from driving. A node the customer can't cleanly answer takes the
  conservative default (escalate / not_drivable), never "probably fine". This matters most on
  high-liability trees (braking, steering).
- Context can only tighten, never loosen. A flag may turn a field fix into a tow; it may
  never turn an unsafe car into a drive.
- Never emit "safe to drive" without a positive drivability basis (manufacturer or legal).
- Car-type facts are checked before an absence is treated as a fault (the spare case).
- Premade, vetted forms carry the safety-critical questions; LLM-generated forms are only for
  the edge-case tail.

---

## Open

- Domains not yet worked through: overheating, steering, warning-light combinations.
- The legal roadworthiness ruleset (`basis=legal`) needs hand-authoring from the Israeli
  traffic regulations.
- How call-center know-how gets captured as playbook rules (interview, seed sheet).
- Shape of the service-window config (hours per resource type).
- Form rendering: premade HTML library structure and when to fall back to a generated form.
