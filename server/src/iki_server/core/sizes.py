"""Sizes in bytes, as people read them."""

_UNITS = (("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024))


def human_size(size: int) -> str:
    """``2147483648`` → ``2 GiB``; ``3328599654`` → ``3.1 GiB``; ``12`` → ``12 bytes``."""
    for unit, factor in _UNITS:
        if size >= factor:
            if size % factor == 0:
                return f"{size // factor} {unit}"
            return f"{size / factor:.1f} {unit}"
    return "1 byte" if size == 1 else f"{size} bytes"
