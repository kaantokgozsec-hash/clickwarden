"""Bounded in-process UI Automation read of the keyboard-focused element.

Raw COM vtable calls (no comtypes). ``focused_editability`` reads control type and read-only flags
only, never names or values, and never invokes, focuses or types. A hung provider yields
``editable=None`` after the deadline instead of blocking input forever.

``focused_text`` is the one exception: it reads the focused control's text so typing can be checked,
but only inside this process. The caller compares it; it is never returned to an agent, logged or
stored, and password fields are never read.
"""
import ctypes
import threading
import time
from ctypes import wintypes

EDIT, COMBO_BOX, DOCUMENT, PANE = 50004, 50003, 50030, 50033
# Focused controls that cannot take free text. List/tree items stay "unknown":
# type-ahead selection in Explorer-style lists is legitimate typing.
NON_TEXT = {50000: "button", 50002: "check_box", 50005: "hyperlink", 50006: "image",
            50011: "menu_item", 50013: "radio_button", 50019: "tab_item", 50020: "text"}
ROLES = {EDIT: "edit", COMBO_BOX: "combo_box", DOCUMENT: "document", PANE: "pane",
         50032: "window", 50026: "group", 50007: "list_item", 50024: "tree_item", **NON_TEXT}
PROPERTIES = {"control_type": 30003, "password": 30019, "value_pattern": 30043,
              "read_only": 30046, "pid": 30002}
_CLSID_CUIAutomation = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
_IID_IUIAutomation = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"
_VT_I4, _VT_BOOL = 3, 11


class _GUID(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                ("data4", ctypes.c_ubyte * 8)]


class _VARIANT(ctypes.Structure):
    # Large enough for the x64 VARIANT; only VT_I4/VT_BOOL payloads are read.
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("value", ctypes.c_longlong), ("extra", ctypes.c_void_p)]


def _method(pointer, index, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(pointer):
    if pointer:
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(pointer)


def _property(element, property_id):
    value = _VARIANT()
    _method(element, 10, ctypes.c_int, ctypes.POINTER(_VARIANT))(element, property_id, ctypes.byref(value))
    try:
        if value.vt == _VT_I4:
            return ctypes.c_int32(value.value & 0xFFFFFFFF).value
        if value.vt == _VT_BOOL:
            return (value.value & 0xFFFF) != 0
        return None
    finally:
        ctypes.oledll.oleaut32.VariantClear(ctypes.byref(value))


def read_focused():
    """Return raw focused-element properties; raises OSError on COM failure."""
    ole32 = ctypes.oledll.ole32
    initialized = ctypes.windll.ole32.CoInitializeEx(None, 0) >= 0  # MTA; S_FALSE still needs balancing
    automation = element = ctypes.c_void_p()
    try:
        clsid, iid = _GUID(), _GUID()
        ole32.CLSIDFromString(_CLSID_CUIAutomation, ctypes.byref(clsid))
        ole32.IIDFromString(_IID_IUIAutomation, ctypes.byref(iid))
        automation = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(automation))
        element = ctypes.c_void_p()
        _method(automation, 8, ctypes.POINTER(ctypes.c_void_p))(automation, ctypes.byref(element))
        if not element:
            return {}
        return {name: _property(element, pid) for name, pid in PROPERTIES.items()}
    finally:
        _release(element)
        _release(automation)
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


_PATTERN_VALUE, _PATTERN_TEXT = 10002, 10014
MAX_READ_CHARS = 200_000


def _bstr_out(pointer):
    """Copy and free a BSTR returned through a pointer-to-pointer."""
    if not pointer.value:
        return ""
    try:
        return ctypes.wstring_at(pointer.value)
    finally:
        ctypes.windll.oleaut32.SysFreeString(ctypes.c_void_p(pointer.value))


def _get_pattern(element, pattern_id):
    pattern = ctypes.c_void_p()
    try:
        _method(element, 16, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(element, pattern_id, ctypes.byref(pattern))
    except OSError:
        return None
    return pattern if pattern else None


def read_focused_text():
    """Text of the focused control, or None when it exposes none (or is a password field)."""
    ole32 = ctypes.oledll.ole32
    initialized = ctypes.windll.ole32.CoInitializeEx(None, 0) >= 0
    automation = element = ctypes.c_void_p()
    pattern = None
    try:
        clsid, iid = _GUID(), _GUID()
        ole32.CLSIDFromString(_CLSID_CUIAutomation, ctypes.byref(clsid))
        ole32.IIDFromString(_IID_IUIAutomation, ctypes.byref(iid))
        automation = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(automation))
        element = ctypes.c_void_p()
        _method(automation, 8, ctypes.POINTER(ctypes.c_void_p))(automation, ctypes.byref(element))
        if not element or _property(element, PROPERTIES["password"]) is True:
            return None
        pattern = _get_pattern(element, _PATTERN_TEXT)
        if pattern:
            document = ctypes.c_void_p()
            _method(pattern, 7, ctypes.POINTER(ctypes.c_void_p))(pattern, ctypes.byref(document))
            try:
                if document:
                    out = ctypes.c_void_p()
                    _method(document, 12, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                        document, MAX_READ_CHARS, ctypes.byref(out))
                    return _bstr_out(out)
            finally:
                _release(document)
            return None
        pattern = _get_pattern(element, _PATTERN_VALUE)
        if pattern:
            out = ctypes.c_void_p()
            _method(pattern, 4, ctypes.POINTER(ctypes.c_void_p))(pattern, ctypes.byref(out))
            return _bstr_out(out)
        return None
    finally:
        _release(pattern)
        _release(element)
        _release(automation)
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


def _normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def focused_text(timeout=.5):
    """Bounded read of the focused control's text; None when unavailable or the provider hangs."""
    box = {}

    def probe():
        try:
            box["text"] = read_focused_text()
        except Exception:
            box["text"] = None

    worker = threading.Thread(target=probe, daemon=True, name="clickwarden-text-probe")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None
    text = box.get("text")
    return None if text is None else _normalize(text)


def verify_typed(before, typed, read=None, deadline=.6, interval=.05, clock=time.monotonic, sleep=time.sleep):
    """Did *typed* land? "matched", "mismatch" or "unavailable". Decided locally; no text leaves this function.

    ``before`` is what the control held before typing (None when unreadable). The typed text must appear
    at least once more than it did before, which also holds when a selection was replaced.
    """
    read = read or focused_text
    wanted = _normalize(typed)
    if before is None or not wanted:
        return "unavailable"
    before = _normalize(before)
    baseline = before.count(wanted)
    end = clock() + deadline
    while True:
        after = read()
        if after is None:
            return "unavailable"
        after = _normalize(after)
        if after.count(wanted) > baseline:
            return "matched"
        if clock() >= end:
            return "mismatch"
        sleep(interval)


def classify(props):
    """True when text clearly has a target, False when clearly not, None when unsure."""
    kind = props.get("control_type")
    writable = props.get("value_pattern") is True and props.get("read_only") is False
    read_only = props.get("value_pattern") is True and props.get("read_only") is True
    if props.get("password") is True:
        return True
    if kind == EDIT:
        return not read_only
    if kind == COMBO_BOX:
        return True if writable else None
    if kind == DOCUMENT:
        return True if writable else False if read_only else None
    if kind in NON_TEXT:
        return False
    return None


def focused_editability(timeout=.6):
    """Compact verdict on whether typed text has an editable target."""
    box = {}

    def probe():
        try:
            box["props"] = read_focused()
            # Chromium builds its tree on the first UIA query and answers that
            # one with its host pane; the real focused element follows at once.
            if box["props"].get("control_type") == PANE:
                time.sleep(.06)
                box["props"] = read_focused()
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {exc}"[:200]

    worker = threading.Thread(target=probe, daemon=True, name="clickwarden-focus-probe")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return {"editable": None, "role": "unknown", "source": "uia_timeout"}
    if "error" in box:
        return {"editable": None, "role": "unknown", "source": "uia_error", "error": box["error"]}
    props = box.get("props") or {}
    if not props:
        return {"editable": None, "role": "none", "source": "uia"}
    kind = props.get("control_type")
    return {"editable": classify(props), "role": ROLES.get(kind, str(kind)), "source": "uia",
            "read_only": props.get("read_only") if props.get("value_pattern") else None,
            "pid": props.get("pid")}
