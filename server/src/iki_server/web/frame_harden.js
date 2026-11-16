// Run first in every sandboxed frame (ADR 0102), as a classic script, so before any
// module, the frame's own code included. The frame's policy closes every way out a
// request takes. WebRTC is the one way no policy covers: its STUN and TURN traffic goes
// to whatever server the code names. So it's taken away here, and so are frames of the
// frame's own, whose globals would be fresh: nothing Iki frames needs either.
(() => {
  for (const name of [
    "RTCPeerConnection",
    "webkitRTCPeerConnection",
    "RTCDataChannel",
    "RTCIceCandidate",
    "RTCSessionDescription",
    "RTCRtpTransceiver",
    "RTCRtpSender",
    "RTCRtpReceiver",
    "RTCCertificate",
  ]) {
    if (name in globalThis) {
      Object.defineProperty(globalThis, name, {
        value: undefined,
        writable: false,
        configurable: false,
        enumerable: false,
      });
    }
  }

  // A frame inside this one, with srcdoc, would load after the current task: removing
  // it at the next microtask, which the observer's callback is, comes first.
  const FRAMES = "iframe, frame, object, embed, portal, fencedframe";
  const remove = (node) => {
    if (!(node instanceof Element)) return;
    if (node.matches(FRAMES)) node.remove();
    for (const inner of node.querySelectorAll(FRAMES)) inner.remove();
  };
  // A frame somewhere the observer can't see: blank the frame. That's navigating away,
  // so the page closes it, as for any frame that tries to.
  const check = () => {
    if (window.length > 0) {
      document.documentElement.replaceChildren();
      location.replace("about:blank");
    }
  };
  const observer = new MutationObserver((changes) => {
    for (const change of changes) for (const node of change.addedNodes) remove(node);
    check();
  });
  const watch = (root) => observer.observe(root, { childList: true, subtree: true });
  watch(document);
  // Shadow roots hide their insides from the document's observer: watch each one.
  const attachShadow = Element.prototype.attachShadow;
  Object.defineProperty(Element.prototype, "attachShadow", {
    value: function (init) {
      const root = attachShadow.call(this, init);
      watch(root);
      return root;
    },
    writable: false,
    configurable: false,
  });
  // Declarative shadow roots, which only these parse from a string, aren't attached
  // through attachShadow.
  for (const [owner, name] of [
    [Element.prototype, "setHTMLUnsafe"],
    [ShadowRoot.prototype, "setHTMLUnsafe"],
    [Document, "parseHTMLUnsafe"],
  ]) {
    if (name in owner) {
      Object.defineProperty(owner, name, {
        value: () => {
          throw new DOMException(`${name} isn't available in Iki's frames.`, "NotSupportedError");
        },
        writable: false,
        configurable: false,
      });
    }
  }
  setInterval(check, 100);
})();
