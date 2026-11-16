# 0102. Frames take away what no policy covers, and exfiltration is tested in three browsers

- Status: Accepted
- Date: 2026-11-16
- Refines: [0084](0084-plugin-components-run-in-sandboxed-frames.md), [0085](0085-types-configure-ikis-own-viewers.md)

## Context

Plugins' components (ADR 0084), and Iki's structure viewer (ADR 0085), run
in sandboxed frames whose policy refuses every request: no network, images
or fonts only as `data:` and `blob:`, no forms, no frames, no navigating
the page. A component is a plugin's own code, and the viewer runs Mol*, so
both are code Iki didn't write, beside data that may be confidential.

A suite that tries every way out, in Chromium, Firefox and WebKit, found
one the policy doesn't cover: **WebRTC**. A peer connection sends STUN and
TURN traffic to whatever server the code names, and no CSP directive or
Permissions-Policy feature governs it in any of the three. Chromium and
WebKit sent STUN requests to a server the test ran. The server's name (a
DNS lookup) and a TURN user name can carry anything. Taking the
constructors away isn't enough on its own: a frame inside the frame, given
`srcdoc`, inherits the policy, so it may load the component's module, and
its globals are fresh.

## Decision

**Every sandboxed frame runs a script first** (`web/frame_harden.js`): a
classic inline script at the top of the head, allowed by its hash, which
runs before any module. It:

- removes WebRTC's constructors (`RTCPeerConnection` and the rest),
  unconfigurably;
- removes any frame, object or embed added to the document, or to any
  shadow root, at the next microtask, before a `srcdoc` document loads.
  `attachShadow` is wrapped so each root is watched, and `setHTMLUnsafe`
  and `parseHTMLUnsafe`, which parse declarative shadow roots, are
  refused;
- blanks the frame if a frame of its own exists anyway (`window.length`),
  which the page closes, as for any frame that navigates.

Nothing Iki frames needs WebRTC or frames: the policy already refuses every
frame's address.

**An exfiltration suite runs on every change, in all three browsers**
(`test_browser_exfiltration`). A server on another origin records every
request, and a UDP socket every datagram. Code in a component's frame
tries:

- fetch, XHR, beacons, sockets, event streams;
- images, styles, stylesheets, fonts, prefetch and preload links;
- scripts, modules, workers, media, frames, objects;
- forms, pop-ups, navigating the page;
- WebRTC, from the frame and from frames of its own.

Navigating its own frame elsewhere is tried too. Iki's pages show values
written to attack them (markup, script, event handlers, `javascript:`
links, style imports) in the grid, an entity's page, its history and a
search. The attacker must hear nothing, and the page must log no CSP
violation or error.

## Consequences

- A component can't use WebRTC, or frames of its own, even ones the policy
  would load.
- Navigating a frame elsewhere is refused by the page's `frame-src`. Chromium
  and WebKit show the refusal as an error page, which the page closes;
  Firefox leaves the frame as it was.
- The script shares the frame with the code it guards, so it's a layer
  before the code runs, not a wall: what it takes away, the code can't get
  back, because the script ran first and made it unconfigurable. Network
  egress controls (UDP to the internet blocked, as most companies' are)
  remain the backstop for WebRTC.
