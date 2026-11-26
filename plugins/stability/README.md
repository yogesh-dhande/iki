# Stability studies

A project designs a stability study for one material and lot, schedules draws
from assigned storage tubes, and reviews each test. The study's **Schedule** tab
shows the window, assigned tube and planned amount for each pull. **Trend**
shows the released or signed-off measurements for each condition, test and
method, in their recorded reporting units.

## Configure the workflow

Install the plugin with Lots and Inventory. At deployment, choose
`review_flow: research` (the default) or `review_flow: regulated_qc` in its
settings. The schedule pins this choice for the life of its study.

- A **Stability coordinator** receives the pull assignments, makes each draw and
  reconciles late or missed pulls.
- A **Stability analyst** records the measurements and submits QC results.
- A **Stability reviewer** signs off a Research result or independently reviews a
  QC result.
- **Stability QA** releases the reviewed QC result. QA, the reviewer and the
  submitting analyst are separate people. A record's creator and editors
  cannot independently review or release it in the same cycle.

The amount service checks the assigned tube's lot. A coordinator sampling
quarantined material also needs the lot plugin's QA role to use **QA sampling**.
A hold or expiry continues to refuse withdrawals. Establish the status and
permissions of study material when planning the windows.

## Design, pull and test

1. Create a **Stability study**. Record the material, lot, protocol and revision,
   study start date, coordinator, conditions and time points. Each time point has
   a code, such as `M3`, counted from the start in calendar months, weeks or days.
   Plan each pull as an assignment: a condition, a time point and a replicate,
   with its stored tube and planned amount, by volume (µL, mL) or by mass (µg,
   mg) drawn at the tube's concentration. A tube may supply several time points
   within its assigned condition. Each other condition needs its own tube.
   - A full design plans every condition at every time point, and every test at
     every pull.
   - A reduced design, bracketed or matrixed as ICH Q1D describes, plans only
     some. Under **Test plan** it lists which tests each condition and time point
     gets. Its **Design justification** says why and where the protocol justifies
     it. Scheduling refuses a reduced design without one. Scheduling checks the material declared by the lot type against the
     material on the study. For a strain isolate, choose the same isolate lot as
     both the material and the lot. List the attributes to measure,
     the method and its revision, and the reporting unit. Use a new linked study
     record when the protocol changes.
2. In **Schedule**, run **Schedule stability pulls**. The run generates each
   planned pull with its due date and inclusive opening and closing dates, and
   the tests it gets. A month is a calendar-month anniversary. Shorter months use
   their last date, counting each month from the original start date. A week is
   seven days. A study's design and scheduled pulls stay in its record.
3. Open the assigned tube's **Amount** tab and choose **Draw**. Scan the tube,
   select the pull under **Used in**, take the planned amount, in its unit or as
   the volume it is at the tube's concentration, and choose the
   draw purpose allowed by the lot. Iki records the source lot, exact
   withdrawal, scan, actor and time. Each pull accepts one draw from its
   assigned tube and lot, so retrying a draw leaves the balance intact.
4. Enter the UTC date of the draw on the pull and set it to **Pulled**. Describe
   the deviation in **Window note** if it is early or late. If the window
   closed without a draw, set it to **Missed** and give the reason.
5. From the completed pull run **Record stability measurement** with a test
   the protocol plans for that pull and its observed value. Attach the report. Sign it off
   in Research, or submit, independently review and release it in Regulated
   QC. A correction before final sign-off sends the submission back to Recorded.
   Open the study's **Trend** tab to compare the results over time. It places
   each result at its time point's days from the start, so time points in
   weeks and months share one axis.

## Published design example

[Study design](../../examples/stability-study/study-design.yaml) includes the ICH Q1A(R2) general-case
long-term condition of 25 °C ± 2 °C and 60% RH ± 5% RH and the accelerated condition
of 40 °C ± 2 °C and 75% RH ± 5% RH. Its partial time-point sequence illustrates
the frequency in §§2.1.6–2.1.7.1. The example windows are synthetic. A lab supplies
its qualified protocol, test methods and assigned material.
[ICH Q1A(R2) guideline](https://database.ich.org/sites/default/files/Q1A%28R2%29%20Step4.pdf)

Design decisions: [ADR 0193](../../docs/adr/0193-a-stability-pull-keeps-its-protocol-revision-and-ledger-draw.md),
[ADR 0243](../../docs/adr/0243-a-stability-protocol-plans-its-own-pulls.md)
