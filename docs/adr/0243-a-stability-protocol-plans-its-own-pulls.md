# 0243. A stability protocol plans its own pulls

- Status: Accepted
- Date: 2026-11-26
- Refines: [0193](0193-a-stability-pull-keeps-its-protocol-revision-and-ledger-draw.md)

## Context

A stability study scheduled exactly one pull for every condition at every whole month.
Each pull was one volume from one tube and got every test. Under ICH Q1A(R2), the
protocol sets the conditions, frequency and tests. ICH Q1D allows a reduced design,
bracketed or matrixed, that tests only some combinations when the protocol justifies it.

Labs also count short studies, such as in-use or photostability holds, in days or
weeks, and pull replicate containers at one time point. Some plan a draw by the mass a
test needs rather than by volume. The old schedule refused all of these, so a lab had
either to plan fake pulls or to keep its real design outside Iki.

## Decision

**The protocol's assignments are the pulls.** Each assignment names a condition, a time
point and a replicate, with its tube and planned amount. Scheduling creates exactly those
pulls. Every condition and time point declared must be used, since an unused one is a
mistake in the design. Only one pull may be planned per condition, time point and
replicate.

**A study's test plan says which tests each pull gets.** Left empty, every pull gets
every test. Recording a measurement refuses a test the protocol doesn't plan for that
pull.

**A reduced design says why.** A design that skips a combination of condition, time
point or test needs its design justification, such as "matrixing per protocol §4.2".
Scheduling refuses it otherwise. Iki checks that the justification is there, not
whether it holds. The protocol's reviewers judge that, as ICH Q1D leaves the design to
the applicant's justification.

**Time points count in days, weeks or calendar months.** Each has a code and an
interval in its unit. Months keep their calendar anniversary, clamped at a shorter
month's end. Each pull and measurement keeps its days from the start, so a trend puts
weeks and months on one axis and labels it with the time points' codes.

**A pull's planned amount is in the unit its protocol gives.** A pull plans a volume
(µL, mL) or a mass (µg, mg). A mass is drawn as a volume at the tube's concentration,
as any draw by mass is. To allow this, a type's `draw_target` can name a float `amount`
with a `unit` field beside it, as well as a quantity in one fixed unit. The draw must
still be exactly the planned amount, once, from the assigned tube and lot.

**A test is numeric or conforms.** A numeric test reports a value in its unit. A
conforming test, such as an appearance, reports whether it conforms and what was seen, as
release testing's conforming criteria do (ADR 0244). A rule keeps a measurement to its
test's kind. The trend plots numbers and lists conforming results.

## Consequences

The full-grid, monthly, one-tube-per-condition design still schedules as it did. Its
pulls carry their time point's code, its interval and their replicate 1. Every
regression of ADR 0193 holds:

- the pinned protocol revision and review flow;
- the declared material check;
- one draw per pull, of exactly the planned amount;
- the windows, and the missed and late reasons.

The sources are still tubes, whose ledgers are in µL. Drawing straight from a counted
container, such as vials or blister packs, needs a stored container with a count ledger
that holds a lot. Inventory has none yet. A study that groups several lots is still
one study per lot, linked by its protocol.
