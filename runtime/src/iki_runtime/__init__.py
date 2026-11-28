"""What Iki's plugins' actions share beyond reading and writing a run: readers for the
files labs' instruments and pipelines write (ADR 0288).

- :mod:`iki_runtime.text`: a file's text or bytes, decompressed by its first bytes.
- :mod:`iki_runtime.fastq`: FASTQ reads.
- :mod:`iki_runtime.airr`: AIRR rearrangements.

Each Iki release ships this package at its own version, and an environment that declares
``iki_runtime: true`` has the release's copy installed. A lab's own image installs
``iki-plugin-runtime`` from PyPI at its Iki's version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("iki-plugin-runtime")
except PackageNotFoundError:  # pragma: no cover - run from a source tree
    __version__ = "0+unknown"
