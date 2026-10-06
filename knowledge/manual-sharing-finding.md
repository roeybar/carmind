# Finding: warning-light knowledge is shared per make and powertrain, not per model

## The test
Hypothesis: the content carmind cares about (dashboard lights, warnings, drivability
procedures) is shared across models within one make and powertrain ("Kia petrol", "Kia
hybrid", "Kia electric"), not written per model.

Test: two different Skoda petrol manuals, the Kamiq (small SUV, MQB-A0) and the Octavia
(large hatch, MQB), were diffed. Different platforms on purpose, same make and fuel.

## Result
- Raw verbatim line overlap is only ~23%, but that is wording, formatting and equipment noise.
- The warning-light concept inventory is 22 of 23 shared (96%). The one difference (glow
  plug) is a diesel-variant text artifact, not a real divergence.

So the hypothesis holds: warning-light and drivability meanings are essentially make-wide,
even across platforms within a make. The wording differs; the knowledge doesn't.

## What drives the difference: powertrain
- **diesel** adds glow plug, DPF/particulate filter, AdBlue/SCR.
- **electric** drops engine/EPC, oil pressure/level, coolant, glow plug, and adds HV
  battery, charging, regeneration, ready light.
- **hybrid/PHEV** is a blend of both.

The model layer carries only small equipment differences (does this trim have ACC, matrix
lights, auto-hold) and the truly model-specific quirks (e.g. an aftermarket immobilizer
sequence), which are not in OEM manuals anyway.

## Impact on acquisition
Manuals are acquired per make x powertrain cell rather than per model. One handbook often
covers several variants (petrol + diesel, or petrol + mild hybrid), so a fleet with dozens of
models collapses to a much smaller set of manuals.

## Impact on the schema and extraction
`dashboard_indicator`, `drivability_gate` and `procedure` are stored at (make, powertrain)
scope, a "platform sheet", not per model. A vehicle record points at its platform sheet plus a
thin model overlay. This:
- cuts manual acquisition and extraction cost by roughly the number of models per cell,
- means a new fleet model usually needs no new extraction (it inherits its sheet),
- keeps provenance intact (the sheet cites the manual it was extracted from).

The synthetic example rulebook (`example-icon-rulebook.json` in this repo) is make-agnostic
and sits above this as the layer shown first. The full stack is:
fleet rulebook -> make x powertrain platform sheet -> model overlay -> per-car context flags.
