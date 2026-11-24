"""The namespace reserved for machine-readable Iki record identities."""

PREFIX = "IKI:"
RESERVED_VENDOR_MESSAGE = "cannot start with IKI: because that prefix identifies an Iki record."


def has_iki_prefix(text: str) -> bool:
    """Scanner input is trimmed; a differently cased prefix still belongs to Iki."""
    return text.lstrip()[: len(PREFIX)].upper() == PREFIX
