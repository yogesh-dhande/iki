# 0264. What a hold covers is the lab's procedure

- Status: Accepted
- Date: 2026-11-26
- Refines: [0211](0211-lots-inventory-samples-and-requests.md)
- Builds on: [0260](0260-a-labs-storage-layout-and-containers-are-its-own.md)

## Context

A container on hold can't be drawn from, and its hold and the lifting of it
each record a reason. Those are the physical record and stay the same for
every lab. What else is held when a container is withdrawn differs. One lab
holds every aliquot and transfer made from it. Another holds only the portions
made before the fault arose, after an investigation, and records that
investigation as the reason. A third holds only the container. `iki/inventory`
shipped the first as a trigger and the action it runs, with the source's reason
on every derived hold.

## Decision

**The incumbent procedure is a named, opt-in profile.** The trigger **Hold all
stock derived from withdrawn stock** holds every container split or
transferred from the source in the project, up to ten steps on, with the
source's reason. Like every trigger, it is off until a project admin turns it
on ([ADR 0080](0080-triggers-run-actions-when-entities-change.md)).

**Case by case is the action.** **Hold stock derived from a withdrawn
container** holds the containers selected, with an optional reason of its own;
without one it gives the source's.

Both keep what makes a derived hold trustworthy. Each names the source's HRID.
A container already on hold keeps its own hold and reason. Nothing is released
automatically. The run checks in one transaction that neither the source nor a
derived container changed since it read them.

## Consequences

A project chooses one of three procedures by turning on the trigger or not and
by whether people run the action. A lab whose scope is another rule, such as
only aliquots or only the portions made before a fault, selects them in the action. A
second shipped trigger per scope would be added only when labs share that scope
as a procedure.
