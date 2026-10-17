"""Pointer-sized provenance marker shared by input injection and takeover hooks."""
# Fits both 32-bit and 64-bit ULONG_PTR. This identifies our events, not a
# security boundary; Windows' injected-event flag remains the primary signal.
CLICKWARDEN_EXTRA_INFO = 0x57464C48
