"""Shared, nonactivating desktop notch. All methods belong to its Tk thread."""
import ctypes
import tkinter as tk
import win32gui


class NotchPanel:
    BG = "#0b0d12"
    BAR = 56  # collapsed height; fits the large STOP button

    def __init__(self, objective, pause, resume, cancel, stop=None, destination=None):
        """destination: optional (text, colour) shown permanently, e.g. where screenshots are sent."""
        self.root = root = tk.Tk()
        root.withdraw()
        root.title("Clickwarden — desktop controls")
        root.overrideredirect(True)
        root.configure(bg=self.BG)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        self.expanded = False
        self.paused = False
        self.pause, self.resume = pause, resume
        self.width = min(720, root.winfo_screenwidth())
        self.x = (root.winfo_screenwidth() - self.width) // 2
        self.y = 0
        row = tk.Frame(root, bg=self.BG, height=self.BAR)
        row.pack(fill="x", padx=14)
        row.pack_propagate(False)
        self.dot = tk.Label(row, text="●", fg="#62ddd1", bg=self.BG, font=("Segoe UI", 12))
        self.dot.pack(side="left", padx=(0, 8))
        tk.Label(row, text="Clickwarden", fg="white", bg=self.BG,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 10))
        self.destination = destination
        if destination:
            self.destination_label = tk.Label(row, text=destination[0], fg=destination[1], bg=self.BG,
                                              font=("Segoe UI", 9, "bold"))
            self.destination_label.pack(side="left", padx=(0, 10))
        self.buttons = {}
        def button(parent, label, callback, color="#20242d", font=("Segoe UI", 9), padx=8, pady=3,
                   active="#38404d"):
            b = tk.Button(parent, text=label, command=callback, bg=color, fg="white",
                          activebackground=active, activeforeground="white", relief="flat",
                          bd=0, highlightthickness=0, font=font, padx=padx, pady=pady,
                          takefocus=False)
            self.buttons[label] = b
            return b
        self.expand_button = button(row, "⌄", self.toggle)
        self.expand_button.pack(side="right", padx=(6, 0))
        # Always visible, large and red: ends the session and releases input at once.
        button(row, "STOP", stop or cancel, "#c8283f", ("Segoe UI", 12, "bold"), 22, 8,
               "#e63950").pack(side="right", padx=(6, 0))
        button(row, "Cancel", cancel, "#772e3e").pack(side="right", padx=(6, 0))
        self.pause_button = button(row, "Pause", self.toggle_pause)
        self.pause_button.pack(side="right", padx=(6, 0))
        self.status = tk.Label(row, text="Preparing…", fg="#c4cedb", bg=self.BG,
                               anchor="w", font=("Segoe UI", 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.details = tk.Frame(root, bg=self.BG)
        tk.Label(self.details, text=objective[:240] or "Computer-use session", fg="white",
                 bg=self.BG, wraplength=self.width-36, justify="left",
                 font=("Segoe UI", 10)).pack(anchor="w", padx=18, pady=(5, 8))
        self.detail_status = tk.Label(self.details, text="", fg="#b4c2d3", bg=self.BG,
                                     wraplength=self.width-36, justify="left", font=("Segoe UI", 9))
        self.detail_status.pack(anchor="w", padx=18)
        controls = tk.Frame(self.details, bg=self.BG)
        controls.pack(fill="x", padx=18, pady=10)
        button(controls, "Resume", resume).pack(side="left")
        if stop:
            button(controls, "Stop session", stop).pack(side="right")
        if destination:
            tk.Label(self.details, text=destination[0], bg=self.BG, fg=destination[1],
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=18, pady=(0, 4))
        tk.Label(self.details, text="Moving or clicking your mouse pauses it · Cancel stops the current action · "
                                    "STOP or Ctrl+Alt+F12 ends everything immediately",
                 bg=self.BG, fg="#8593a6", wraplength=self.width-36, justify="left",
                 font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(0, 12))
        root.protocol("WM_DELETE_WINDOW", stop or cancel)
        root.geometry(f"{self.width}x{self.BAR}+{self.x}+{self.y}")
        root.update_idletasks()
        self.hwnd = win32gui.GetParent(root.winfo_id()) or root.winfo_id()
        win32gui.SetWindowLong(self.hwnd, -20, win32gui.GetWindowLong(self.hwnd, -20) | 0x08000000 | 0x80)
        if not ctypes.windll.user32.SetWindowDisplayAffinity(self.hwnd, 0x11):
            root.destroy()
            raise RuntimeError("Cannot exclude control notch from capture")
        self.resize()
        root.deiconify()
        root.update()

    def resize(self):
        self.root.update_idletasks()
        height = self.BAR + (self.details.winfo_reqheight() if self.expanded else 0)
        # Preserve relocation performed by target avoidance.
        x, y, _, _ = win32gui.GetWindowRect(self.hwnd)
        self.root.geometry(f"{self.width}x{height}{x:+d}{y:+d}")
        self.root.update_idletasks()
        region = win32gui.CreateRoundRectRgn(0, -20, self.width + 1, height + 1, 32, 32)
        win32gui.SetWindowRgn(self.hwnd, region, True)

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.details.pack(fill="x")
        else:
            self.details.pack_forget()
        self.expand_button.configure(text="⌃" if self.expanded else "⌄")
        self.resize()

    def toggle_pause(self):
        (self.resume if self.paused else self.pause)()

    def update(self, message, details="", paused=False, color=None):
        self.paused = paused
        self.pause_button.configure(text="Resume" if paused else "Pause")
        self.status.configure(text=message.replace("\n", " "))
        self.dot.configure(fg=color or ("#ffcf70" if paused else "#62ddd1"))
        text = message + ("\n" + details if details else "")
        if self.detail_status.cget("text") != text:
            self.detail_status.configure(text=text)
            if self.expanded:
                self.resize()
