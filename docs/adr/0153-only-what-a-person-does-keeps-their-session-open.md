# 0153. Only what a person does keeps their session open

- Status: Accepted
- Date: 2026-11-19
- Refines: [0030](0030-tasks-live-updates-and-notifications.md)

## Context

A session ends after a stretch of inactivity, 8 hours by default and often
much less in regulated labs: 21 CFR Part 11 §11.10(d) and EU Annex 11 expect
an unattended workstation to stop being usable. Iki moved the idle timeout
forward on every signed-in request except the notifications stream.

Pages make requests by themselves. They follow runs, batches, workflow runs
and setup builds over event streams that reconnect on their own, poll the
compute dashboard and lists of webhooks, imports and exports, and read again
whatever a live event says changed. A visible page left open, the Compute
page say, kept its session alive until its absolute lifetime, however long
nobody touched it.

## Decision

**Only what the person does is activity.**

- **Event streams are passive.** Every stream view is marked so, and
  opening or reconnecting one never moves the idle timeout.
- **The web app says when it asks by itself.** It notes the person's input
  (pointer, keyboard, wheel and touch) and sends `Iki-Background: 1` on any
  API request made more than a minute after the last. The server leaves
  the idle timeout where it was for those. This covers polls, the reads
  that follow live events, and retries alike, without marking each one.
- **Work the person waits on counts while it runs.** An upload, or a
  bundle being sent for import, keeps them active until it ends, however
  long it takes.
- **Iki's own frames tell the page.** Turning and zooming a structure
  happens in the structure viewer's frame, which the page can't see; the
  frame says so over its message port, at most every 15 seconds.

A request without the header counts as activity, as before: forms and page
loads are always the person's doing, and a client that doesn't know the
header can only keep a session as it would have.

## Consequences

- An unattended tab signs out on time, whatever it polls or follows.
- Someone who reads a long page without touching it for the whole idle
  timeout is signed out. That is what the timeout is for.
- Input inside plugins' component frames isn't seen: they're sandboxed,
  and an untrusted frame mustn't be able to keep a session open. A person
  working only in one for the whole idle timeout is signed out.

## Alternatives considered

- **Marking each automatic request** (polling queries, live-event reads):
  every new poll would need remembering, and one forgotten keeps sessions
  open. Input recency covers them all.
- **A client-side countdown that signs the person out:** the server must
  decide, since a stopped or hidden page wouldn't count down.
