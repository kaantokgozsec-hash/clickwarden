"""`clickwarden configure` — choose the vision provider, review what leaves the computer, accept or decline.

Validation and saving live in plain functions so they can be tested without a display; the
window is a thin layer on top. API keys are typed only here (never through an AI agent or the
MCP tools) and are stored encrypted with Windows DPAPI.
"""

from __future__ import annotations

import json
import threading

from . import providers
from .providers import COORD_SPACES, PRESETS, Settings

HEADERS_WARNING = ("Extra headers are for things like a provider's required app name. Send only what your "
                   "provider documents. Do not make requests look like they come from another company's "
                   "software; provider terms of service apply to your account.")


def form_to_settings(values: dict) -> tuple[Settings, list[str]]:
    """Validate raw form text. Returns settings (key included if typed) and a list of problems."""
    errors: list[str] = []
    url = (values.get("base_url") or "").strip()
    model = (values.get("model") or "").strip()
    host = providers.host_of(url)
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if not host or scheme not in ("http", "https"):
        errors.append("Endpoint must be a full http(s) URL, e.g. https://example.com/v1/chat/completions")
    elif scheme == "http" and not providers.is_local(url):
        errors.append("Remote endpoints must use https:// so your screenshots and key are encrypted in transit.")
    if not model:
        errors.append("Model name is required.")
    headers: dict = {}
    raw_headers = (values.get("headers") or "").strip()
    if raw_headers:
        try:
            parsed = json.loads(raw_headers)
        except ValueError:
            parsed, headers = None, {}
            errors.append('Extra headers must be a JSON object such as {"X-Title": "My app"}.')
        if parsed is not None:
            headers = providers.clean_headers(parsed)
            if not isinstance(parsed, dict) or len(headers) != len(parsed):
                errors.append("Extra headers must be a JSON object of plain string names and values "
                              "(letters, digits and hyphens in names; no control characters).")
    coord = values.get("coord_space") or "pixels"
    if coord not in COORD_SPACES:
        errors.append("Coordinate format must be pixels or norm1000.")
    settings = Settings(preset=values.get("preset") or "custom", base_url=url, model=model, headers=headers,
                        coord_space=coord if coord in COORD_SPACES else "pixels",
                        send_thinking_disabled=bool(values.get("send_thinking_disabled")),
                        include_usage=bool(values.get("include_usage", True)),
                        api_key=(values.get("api_key") or "").strip())
    for name in ("price_in", "price_out"):
        text = str(values.get(name) or "").strip()
        if text:
            try:
                if float(text) < 0:
                    raise ValueError
            except ValueError:
                errors.append(f"{name.replace('_', ' ').title()} must be a non-negative number.")
    return settings, errors


def host_matches(typed: str, settings: Settings) -> bool:
    """The user accepts a cloud destination by typing its host name (a tick box is too easy to click through)."""
    return bool(settings.host) and (typed or "").strip().lower().rstrip("/") == settings.host


def save_form(values: dict, accepted: bool) -> list[str]:
    """Persist validated settings. Remote destinations need `accepted` (the host name typed to confirm)."""
    settings, errors = form_to_settings(values)
    config = providers.load_config()
    if not errors and not settings.local:
        previously = providers.consent_given(settings, config)
        if not accepted and not previously:
            errors.append(f"Type {settings.host} in the acceptance box to confirm your screen contents will be "
                          "sent there, or choose a local provider.")
    if errors:
        return errors
    config.update(preset=settings.preset, base_url=settings.base_url, model=settings.model,
                  headers=settings.headers, coord_space=settings.coord_space,
                  send_thinking_disabled=settings.send_thinking_disabled, include_usage=settings.include_usage)
    for name in ("price_in", "price_out"):
        text = str(values.get(name) or "").strip()
        if text:
            config[name] = float(text)
        else:
            config.pop(name, None)
    if settings.local:
        config.pop("consent", None)
    elif accepted:
        providers.record_consent(config, settings.host)
    providers.save_config(config)
    if settings.api_key:
        providers.save_key(settings.api_key)
    return []


def describe() -> dict:
    """Non-secret summary for `clickwarden configure --show` and diagnostics."""
    s = providers.resolve()
    return {"configured": not s.problem, "problem": s.problem or None, "destination": s.host or None,
            "local_only": s.local, "model": s.model or None, "coord_space": s.coord_space,
            "extra_header_names": sorted(s.headers), "api_key": "stored" if s.api_key else "none",
            "data_sharing_accepted_for": providers.consent_status(s),
            "config_dir": str(providers.config_dir())}


def launch():  # pragma: no cover - interactive window
    import tkinter as tk
    from tkinter import messagebox, ttk

    config = providers.load_config()
    root = tk.Tk()
    root.title("Clickwarden — vision provider and data sharing")
    root.minsize(660, 780)
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    labels = {name: preset["label"] for name, preset in PRESETS.items()}
    by_label = {label: name for name, label in labels.items()}
    var = {"preset": tk.StringVar(value=labels.get(config.get("preset", "custom"), labels["custom"])),
           "base_url": tk.StringVar(value=config.get("base_url", "")),
           "model": tk.StringVar(value=config.get("model", "")),
           "api_key": tk.StringVar(), "coord_space": tk.StringVar(value=config.get("coord_space", "pixels")),
           "send_thinking_disabled": tk.BooleanVar(value=bool(config.get("send_thinking_disabled"))),
           "include_usage": tk.BooleanVar(value=bool(config.get("include_usage", True))),
           "price_in": tk.StringVar(value=str(config.get("price_in", ""))),
           "price_out": tk.StringVar(value=str(config.get("price_out", "")))}
    typed = tk.StringVar()
    status = tk.StringVar()

    def row(index, text, widget):
        ttk.Label(frame, text=text).grid(row=index, column=0, sticky="nw", pady=3, padx=(0, 10))
        widget.grid(row=index, column=1, sticky="ew", pady=3)
        return widget

    provider_box = row(0, "Provider", ttk.Combobox(frame, textvariable=var["preset"], values=list(labels.values()),
                                                   state="readonly"))
    note = ttk.Label(frame, text="", wraplength=470, foreground="#555")
    note.grid(row=1, column=1, sticky="w")
    row(2, "Endpoint URL", ttk.Entry(frame, textvariable=var["base_url"]))
    row(3, "Model", ttk.Entry(frame, textvariable=var["model"]))
    key_entry = row(4, "API key", ttk.Entry(frame, textvariable=var["api_key"], show="•"))
    key_note = ttk.Label(frame, text="", foreground="#555")
    key_note.grid(row=5, column=1, sticky="w")
    row(6, "Coordinate format", ttk.Combobox(frame, textvariable=var["coord_space"], values=list(COORD_SPACES),
                                             state="readonly"))
    ttk.Label(frame, text="Use norm1000 only for models that answer on a 0-1000 grid (some Qwen-VL versions).",
              foreground="#555", wraplength=470).grid(row=7, column=1, sticky="w")
    headers_box = tk.Text(frame, height=3, width=40)
    headers_box.insert("1.0", json.dumps(config.get("headers", {})) if config.get("headers") else "")
    row(8, "Extra headers (JSON)", headers_box)
    ttk.Label(frame, text=HEADERS_WARNING, foreground="#8a5a00", wraplength=470).grid(row=9, column=1, sticky="w")
    row(10, "Provider option", ttk.Checkbutton(frame, text='Send "thinking": {"type": "disabled"} (only if your provider accepts it)',
                                              variable=var["send_thinking_disabled"]))
    row(11, "", ttk.Checkbutton(frame, text="Ask the server to report token usage (turn off if it errors)",
                                variable=var["include_usage"]))
    prices = ttk.Frame(frame)
    ttk.Entry(prices, textvariable=var["price_in"], width=9).pack(side="left")
    ttk.Label(prices, text=" in / ").pack(side="left")
    ttk.Entry(prices, textvariable=var["price_out"], width=9).pack(side="left")
    ttk.Label(prices, text=" out  USD per 1M tokens (optional, for cost estimates)").pack(side="left")
    row(12, "Prices", prices)

    notice = tk.Text(frame, height=11, wrap="word", background="#f6f6f6")
    notice.grid(row=13, column=0, columnspan=2, sticky="nsew", pady=(10, 4))
    frame.rowconfigure(13, weight=1)
    consent_frame = ttk.Frame(frame)
    consent_frame.grid(row=14, column=0, columnspan=2, sticky="ew")
    consent_label = ttk.Label(consent_frame, text="", wraplength=600, justify="left")
    consent_label.pack(anchor="w")
    consent_entry = ttk.Entry(consent_frame, textvariable=typed)
    consent_entry.pack(anchor="w", fill="x", pady=(2, 0))
    ttk.Label(frame, textvariable=status, wraplength=600, foreground="#a00").grid(row=15, column=0, columnspan=2, sticky="w")

    def current_values():
        values = {name: v.get() for name, v in var.items()}
        values["preset"] = by_label.get(values["preset"], "custom")
        values["headers"] = headers_box.get("1.0", "end").strip()
        return values

    def refresh(*_):
        settings, _errors = form_to_settings(current_values())
        text = providers.disclosure_text(settings if settings.base_url else Settings(base_url="https://(not set)"))
        notice.config(state="normal")
        notice.delete("1.0", "end")
        notice.insert("1.0", text)
        notice.config(state="disabled")
        key_note.config(text="A key is already stored; leave blank to keep it." if providers.load_key() or
                        providers.env("API_KEY") else "")
        if settings.local:
            consent_entry.state(["disabled"])
            consent_label.config(text="Local server: nothing leaves this computer, no acceptance needed.")
        else:
            consent_entry.state(["!disabled"])
            if providers.consent_given(settings, config):
                consent_label.config(text=f"You already accepted sending screens to {settings.host}. "
                                          "Use Revoke consent to withdraw it.")
            else:
                consent_label.config(text=f"To accept, type the host name {settings.host or '(set an endpoint first)'} "
                                          "below. This confirms you read the notice and that your screen contents "
                                          "will be sent there.")

    def choose_preset(*_):
        preset = PRESETS.get(by_label.get(var["preset"].get(), "custom"), PRESETS["custom"])
        var["base_url"].set(preset["base_url"])
        var["model"].set(preset["model"])
        var["coord_space"].set(preset["coord_space"])
        note.config(text=preset["note"])
        typed.set("")
        refresh()

    def save():
        settings, _problems = form_to_settings(current_values())
        errors = save_form(current_values(), host_matches(typed.get(), settings))
        if errors:
            status.set("\n".join(errors))
            return
        status.set("")
        messagebox.showinfo("Clickwarden", "Saved. Restart any running Clickwarden/MCP process to use the new settings.")
        root.destroy()

    def test():
        settings, errors = form_to_settings(current_values())
        settings.api_key = settings.api_key or providers.env("API_KEY") or providers.load_key()
        if errors:
            status.set("\n".join(errors))
            return
        if not settings.local and not (host_matches(typed.get(), settings) or providers.consent_given(settings, config)):
            status.set(f"Accept the data-sharing notice first: type {settings.host} in the acceptance box.")
            return
        if not messagebox.askokcancel("Test connection", f"This sends one blank 64x64 image and the word OK to "
                                      f"{settings.host}. It may use a tiny amount of paid quota. Continue?"):
            return
        status.set("Testing…")

        def work():
            result = providers.test_connection(settings)
            root.after(0, lambda: status.set("Connection works: " + result["reply"] if result["ok"]
                                             else "Test failed: " + str(result["error"])))
        threading.Thread(target=work, daemon=True).start()

    def remove_key():
        providers.save_key("")
        refresh()

    def revoke():
        had = providers.revoke_consent()
        config.pop("consent", None)
        typed.set("")
        status.set("Consent revoked. Clickwarden will not send screens to a cloud provider until you accept again."
                   if had else "No saved consent to revoke.")
        refresh()

    buttons = ttk.Frame(frame)
    buttons.grid(row=16, column=0, columnspan=2, sticky="e", pady=(8, 0))
    ttk.Button(buttons, text="Revoke consent", command=revoke).pack(side="left", padx=4)
    ttk.Button(buttons, text="Remove stored key", command=remove_key).pack(side="left", padx=4)
    ttk.Button(buttons, text="Test connection", command=test).pack(side="left", padx=4)
    ttk.Button(buttons, text="Save", command=save).pack(side="left", padx=4)
    ttk.Button(buttons, text="Close", command=root.destroy).pack(side="left", padx=4)
    provider_box.bind("<<ComboboxSelected>>", choose_preset)
    var["base_url"].trace_add("write", refresh)
    refresh()
    note.config(text=PRESETS.get(config.get("preset", "custom"), PRESETS["custom"])["note"])
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    import sys
    args = sys.argv[1:] if argv is None else argv
    if "--show" in args:
        print(json.dumps(describe(), indent=2))
        return 0
    if "--revoke-consent" in args:
        print(json.dumps({"revoked": providers.revoke_consent()}))
        return 0
    launch()
    return 0
