"""What every sandboxed frame's page runs before its own code (ADR 0102): plugins'
components (ADR 0084) and Iki's viewers (ADR 0085)."""

import base64
import hashlib
from pathlib import Path

#: Takes WebRTC away, the one way out of a frame its policy can't close.
HARDEN = (Path(__file__).parent / "frame_harden.js").read_text()

#: The tag, first in the frame's head: a classic script runs as the page is read,
#: before any module.
HARDEN_TAG = f"<script>{HARDEN}</script>\n"

#: The script's source in the frame's policy, by its hash.
HARDEN_SOURCE = (
    "'sha256-" + base64.b64encode(hashlib.sha256(HARDEN.encode()).digest()).decode() + "'"
)
