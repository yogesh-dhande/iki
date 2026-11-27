# 0222. Facilities choose automatic calibration tracing

- Status: Accepted
- Updated by [0298](0298-a-plugin-can-run-its-dependencies-actions.md): the example's trigger runs Equipment's own trace, with no local copy
- Date: 2026-11-25
- Updates [0194](0194-equipment-use-pins-calibration-and-failures-open-impact-cases.md)

## Context

When an engineer discovers a failed check, the instrument can be flagged
immediately. Its signed service report and measurements may arrive later.
The engineer and QA need the observed outcome, suspect window, original
measurement revision and disposition in either order of events.

## Decision

Equipment keeps the instrument's service and calibration states, signed service
history, use evidence and explicit **Trace failed calibrations** action.
The Service schedule tab shows the failed state and observations, and lets an
investigator launch the trace with the records that cover the investigation.

The [calibration tracing example](../../examples/plugins/calibration_tracing/README.md)
is for facilities whose procedure opens assessments when the calibration flag
changes to Failed. A facility copies and reviews the policy before deployment.
Each participating project enables the trigger. Its local action uses the same
failure/use key as the manual action and writes Equipment's impact records.

## Consequences

A facility can flag a failure immediately, trace the signed evidence when it
is available and reconcile late records. Changing the flag in a standard
Equipment installation leaves the timing of that trace to the investigator.
A facility running the example watches the trigger and run history and can
retry manually. Both routes keep the original signed measurement and its
disposition separately accessible.
