"""
Update Dialog — clean tkinter popups for update available / downloading / installing.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import platform

IS_MAC = platform.system() == "Darwin"
FONT = "Helvetica Neue" if IS_MAC else "Segoe UI"

# ── Colours ────────────────────────────────────────────────────────────────
BG_PAGE   = "#F0F2F5"
BG_CARD   = "#FFFFFF"
BORDER    = "#E5E7EB"
FG        = "#1C1C1E"
FG_SEC    = "#374151"
FG_MUTED  = "#6B7280"
ACCENT    = "#4F46E5"
ACCENT_HV = "#4338CA"
SUCCESS   = "#059669"
DANGER    = "#EF4444"
DANGER_HV = "#DC2626"
GREY_BTN  = "#F3F4F6"
GREY_BTN_FG = "#374151"
GREY_BTN_HV = "#E5E7EB"


def show_update_dialog(parent, updater, update_info):
    """
    Show the appropriate update dialog based on whether mandatory or not.
    parent: tk parent window
    updater: Updater instance
    update_info: dict from check_for_updates()
    """
    dialog = UpdateAvailableDialog(parent, updater, update_info)
    dialog.grab_set()
    dialog.focus_force()


class UpdateAvailableDialog(tk.Toplevel):
    """Update available popup — shows version info, release notes, action buttons."""

    def __init__(self, parent, updater, info):
        super().__init__(parent)
        self.updater = updater
        self.info = info
        self.mandatory = info.get("mandatory", False)

        self.title("Update Required" if self.mandatory else "Update Available")
        self.configure(bg=BG_CARD)
        self.geometry("420x380")
        self.resizable(False, False)
        self.transient(parent)

        if self.mandatory:
            self.protocol("WM_DELETE_WINDOW", self._on_mandatory_close)

        self._build_ui()

    def _on_mandatory_close(self):
        """Clicking X on mandatory update exits the app."""
        self.master.destroy()

    def _build_ui(self):
        pad = 28

        # ── Header ─────────────────────────────────────────────────────────
        if self.mandatory:
            hdr_bg = DANGER
            hdr_text = "Required Update"
        else:
            hdr_bg = ACCENT
            hdr_text = "Update Available"

        hdr = tk.Frame(self, bg=hdr_bg, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text=hdr_text, bg=hdr_bg, fg="#FFFFFF",
                 font=(FONT, 14, "bold")).pack(side="left", padx=pad, pady=10)

        body = tk.Frame(self, bg=BG_CARD)
        body.pack(fill="both", expand=True, padx=pad, pady=(16, 0))

        latest = self.info.get("latest_version", "?")
        current = self.info.get("current_version", "?")
        app_name = self.updater.app_name

        if self.mandatory:
            tk.Label(body, text=f"You must update to continue using\n{app_name}.",
                     bg=BG_CARD, fg=FG, font=(FONT, 11),
                     justify="left", wraplength=360).pack(anchor="w", pady=(0, 6))
            tk.Label(body, text=f"Version {latest} is required.",
                     bg=BG_CARD, fg=FG_SEC, font=(FONT, 10, "bold")).pack(
                anchor="w", pady=(0, 10))
        else:
            tk.Label(body,
                     text=f"{app_name} v{latest} is available",
                     bg=BG_CARD, fg=FG, font=(FONT, 12, "bold")).pack(
                anchor="w", pady=(0, 2))
            tk.Label(body, text=f"You have v{current}",
                     bg=BG_CARD, fg=FG_MUTED, font=(FONT, 10)).pack(
                anchor="w", pady=(0, 10))

        # ── Release notes ─────────────────────────────────────────────────
        notes = self.info.get("release_notes", "")
        if notes:
            tk.Label(body, text="What's new:", bg=BG_CARD, fg=FG_SEC,
                     font=(FONT, 10, "bold")).pack(anchor="w", pady=(0, 4))

            notes_frame = tk.Frame(body, bg="#F9FAFB", highlightbackground=BORDER,
                                    highlightthickness=1)
            notes_frame.pack(fill="x", pady=(0, 10))

            for line in notes.replace("\\n", "\n").strip().split("\n"):
                line = line.strip()
                if line.startswith("- "):
                    line = "\u2022 " + line[2:]
                elif line and not line.startswith("\u2022"):
                    line = "\u2022 " + line
                if line:
                    tk.Label(notes_frame, text=line, bg="#F9FAFB", fg=FG_SEC,
                             font=(FONT, 10), anchor="w", wraplength=340,
                             justify="left").pack(fill="x", padx=10, pady=1)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=BG_CARD)
        btn_frame.pack(fill="x", padx=pad, pady=(0, 20))

        # Update Now button
        update_btn = tk.Frame(btn_frame, bg=ACCENT, cursor="hand2")
        if self.mandatory:
            btn_text = "  Update Now \u2014 Required  "
            update_btn.pack(fill="x", pady=(0, 6))
        else:
            btn_text = "  Update Now  "
            update_btn.pack(side="left", padx=(0, 8))

        update_lbl = tk.Label(update_btn, text=btn_text, bg=ACCENT, fg="#FFFFFF",
                               font=(FONT, 11, "bold"), padx=20, pady=8,
                               cursor="hand2")
        update_lbl.pack(fill="x" if self.mandatory else None)

        for w in (update_btn, update_lbl):
            w.bind("<Button-1>", lambda e: self._start_download())
            w.bind("<Enter>", lambda e: (update_btn.config(bg=ACCENT_HV),
                                          update_lbl.config(bg=ACCENT_HV)))
            w.bind("<Leave>", lambda e: (update_btn.config(bg=ACCENT),
                                          update_lbl.config(bg=ACCENT)))

        # Remind Me Later (only for non-mandatory)
        if not self.mandatory:
            later_btn = tk.Frame(btn_frame, bg=GREY_BTN, cursor="hand2",
                                  highlightbackground=BORDER, highlightthickness=1)
            later_btn.pack(side="left")
            later_lbl = tk.Label(later_btn, text="  Remind Me Later  ",
                                  bg=GREY_BTN, fg=GREY_BTN_FG,
                                  font=(FONT, 11), padx=12, pady=8,
                                  cursor="hand2")
            later_lbl.pack()

            for w in (later_btn, later_lbl):
                w.bind("<Button-1>", lambda e: self.destroy())
                w.bind("<Enter>", lambda e: (later_btn.config(bg=GREY_BTN_HV),
                                              later_lbl.config(bg=GREY_BTN_HV)))
                w.bind("<Leave>", lambda e: (later_btn.config(bg=GREY_BTN),
                                              later_lbl.config(bg=GREY_BTN)))

        if self.mandatory:
            tk.Label(btn_frame, text="(Cannot skip this update)",
                     bg=BG_CARD, fg=FG_MUTED, font=(FONT, 9)).pack(pady=(4, 0))

    def _start_download(self):
        """Switch to download + install progress dialog."""
        self.destroy()
        DownloadAndInstallDialog(self.master, self.updater, self.info)


class DownloadAndInstallDialog(tk.Toplevel):
    """Download + auto-install progress popup."""

    def __init__(self, parent, updater, info):
        super().__init__(parent)
        self.updater = updater
        self.info = info
        self.parent_window = parent

        self.title("Updating...")
        self.configure(bg=BG_CARD)
        self.geometry("420x250")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # Prevent close during update

        self._build_ui()
        self._start()

    def _build_ui(self):
        pad = 28

        hdr = tk.Frame(self, bg=ACCENT, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        self.hdr_label = tk.Label(hdr, text="Downloading Update...", bg=ACCENT,
                                   fg="#FFFFFF", font=(FONT, 13, "bold"))
        self.hdr_label.pack(side="left", padx=pad, pady=8)

        body = tk.Frame(self, bg=BG_CARD)
        body.pack(fill="both", expand=True, padx=pad, pady=16)

        latest = self.info.get("latest_version", "?")
        tk.Label(body, text=f"{self.updater.app_name} v{latest}",
                 bg=BG_CARD, fg=FG, font=(FONT, 11, "bold")).pack(
            anchor="w", pady=(0, 12))

        # Progress bar
        style = ttk.Style()
        style.configure("Update.Horizontal.TProgressbar",
                         troughcolor="#E5E7EB", background=ACCENT, thickness=18)
        self.progress = ttk.Progressbar(body, orient="horizontal",
                                         mode="determinate", length=360,
                                         style="Update.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(0, 6))

        self.pct_label = tk.Label(body, text="0%", bg=BG_CARD, fg=FG_SEC,
                                   font=(FONT, 10))
        self.pct_label.pack(anchor="w")

        self.size_label = tk.Label(body, text="", bg=BG_CARD, fg=FG_MUTED,
                                    font=(FONT, 9))
        self.size_label.pack(anchor="w")

        self.status_label = tk.Label(body, text="Downloading...", bg=BG_CARD,
                                      fg=FG_MUTED, font=(FONT, 10))
        self.status_label.pack(anchor="w", pady=(6, 0))

        # Error buttons frame (hidden initially)
        self.btn_frame = tk.Frame(self, bg=BG_CARD)

    def _start(self):
        url = self.info.get("download_url", "")
        checksum = self.info.get("checksum", "")

        def _worker():
            try:
                # ── Phase 1: Download ────────────────────────────────────
                path = self.updater.download_update(
                    url, checksum, progress_callback=self._on_progress)

                # ── Phase 2: Install ─────────────────────────────────────
                self.after(0, lambda: self._switch_to_install_phase())

                self.updater.install_update(
                    path,
                    status_callback=lambda msg: self.after(
                        0, lambda m=msg: self.status_label.config(text=m))
                )

            except ValueError as e:
                self.after(0, lambda: self._on_error(str(e)))
            except Exception as e:
                self.after(0, lambda: self._on_error(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _switch_to_install_phase(self):
        """Update UI to show installation progress."""
        self.hdr_label.config(text="Installing Update...")
        self.progress["value"] = 100
        self.pct_label.config(text="100%")
        self.status_label.config(text="Installing... please wait", fg=ACCENT)

        # Change progress bar color to green
        style = ttk.Style()
        style.configure("Update.Horizontal.TProgressbar",
                         background=SUCCESS)

    def _on_progress(self, downloaded, total):
        """Called from download thread — schedules UI update on main thread."""
        def _update():
            if total > 0:
                pct = min(100, int(downloaded / total * 100))
                self.progress["value"] = pct
                self.pct_label.config(text=f"{pct}%")
                dl_mb = downloaded / (1024 * 1024)
                tot_mb = total / (1024 * 1024)
                self.size_label.config(text=f"{dl_mb:.1f} MB / {tot_mb:.1f} MB")
            else:
                dl_mb = downloaded / (1024 * 1024)
                self.size_label.config(text=f"{dl_mb:.1f} MB downloaded")
        try:
            self.after(0, _update)
        except Exception:
            pass

    def _on_error(self, msg):
        """Show error and allow retry or close."""
        self.status_label.config(text="", fg=FG_MUTED)
        self.pct_label.config(text="")
        self.size_label.config(text="")
        self.progress["value"] = 0

        # Reset header
        self.hdr_label.config(text="Update Failed")
        style = ttk.Style()
        style.configure("Update.Horizontal.TProgressbar", background=DANGER)

        error_msg = str(msg)
        if "checksum" in error_msg.lower() or "corrupted" in error_msg.lower():
            display = "Download corrupted \u2014 please try again."
        elif "install" in error_msg.lower():
            display = f"Installation failed: {error_msg[:120]}"
        else:
            display = f"Update failed: {error_msg[:120]}"

        self.status_label.config(text=display, fg=DANGER)

        # Allow closing now
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.btn_frame.pack(pady=(0, 10))

        retry_btn = tk.Label(self.btn_frame, text="  Retry  ", bg=ACCENT,
                              fg="#FFFFFF", font=(FONT, 10, "bold"), padx=14,
                              pady=6, cursor="hand2")
        retry_btn.pack(side="left", padx=4)
        retry_btn.bind("<Button-1>", lambda e: self._retry())

        close_btn = tk.Label(self.btn_frame, text="  Close  ", bg=GREY_BTN,
                              fg=GREY_BTN_FG, font=(FONT, 10), padx=14, pady=6,
                              cursor="hand2")
        close_btn.pack(side="left", padx=4)
        close_btn.bind("<Button-1>", lambda e: self.destroy())

    def _retry(self):
        """Retry the full download + install."""
        self.destroy()
        DownloadAndInstallDialog(self.master, self.updater, self.info)
