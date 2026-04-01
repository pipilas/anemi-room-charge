"""
Updater — checks GitHub for new versions, downloads and installs updates.
Works with any Python tkinter app. All app-specific values passed as parameters.

Supports:
  - Windows: downloads .exe, runs it, exits current app
  - macOS:   downloads .dmg, mounts it, copies .app to /Applications, relaunches
"""

import json
import hashlib
import tempfile
import subprocess
import sys
import os
import shutil
import platform
import threading
import urllib.request
import urllib.error
from pathlib import Path

from .version_manager import get_version, compare_versions, should_update

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"


class Updater:
    """
    Universal auto-updater for Python desktop apps.

    Usage:
        updater = Updater(
            github_username="pipilas",
            github_repo="anemi-room-charge",
            app_name="Room Charge & Sales"
        )
        updater.check_and_prompt(parent_window=root)
    """

    def __init__(self, current_version=None, github_username="", github_repo="",
                 app_name="App", version_file=None):
        self.current_version = current_version or get_version(version_file)
        self.github_username = github_username
        self.github_repo = github_repo
        self.app_name = app_name
        self._version_url = (
            f"https://raw.githubusercontent.com/"
            f"{github_username}/{github_repo}/main/version.json"
        )

    def check_for_updates(self):
        """
        Check GitHub for new version. Returns dict with update info.
        Raises ConnectionError on network failure.
        """
        try:
            req = urllib.request.Request(self._version_url, method="GET")
            req.add_header("User-Agent", f"{self.app_name}-Updater")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
            raise ConnectionError(f"Cannot check for updates: {e}")

        latest = data.get("latest_version", "0.0.0")
        minimum = data.get("minimum_version", "0.0.0")
        mandatory = data.get("mandatory", False)

        # Force mandatory if current is below minimum
        if compare_versions(self.current_version, minimum) < 0:
            mandatory = True

        update_available = should_update(self.current_version, latest, minimum)

        # Pick the right download URL for this platform
        if IS_MAC:
            download_url = data.get("download_url_mac", data.get("download_url", ""))
        else:
            download_url = data.get("download_url_windows", data.get("download_url", ""))

        # If no explicit URL, try GitHub Releases API for the latest tag
        if not download_url and update_available:
            download_url = self._find_release_asset(latest)

        return {
            "update_available": update_available,
            "latest_version": latest,
            "current_version": self.current_version,
            "minimum_version": minimum,
            "mandatory": mandatory,
            "release_notes": data.get("release_notes", ""),
            "release_date": data.get("release_date", ""),
            "download_url": download_url,
            "checksum": data.get("checksum_sha256", ""),
        }

    def _find_release_asset(self, version: str) -> str:
        """
        Query GitHub Releases API for the download URL of the .exe or .dmg
        asset matching the given version tag (e.g. 'v1.2.4' or '1.2.4').
        Returns the browser_download_url or empty string if not found.
        """
        tags_to_try = [f"v{version}", version]
        ext = ".dmg" if IS_MAC else ".exe"

        for tag in tags_to_try:
            url = (f"https://api.github.com/repos/"
                   f"{self.github_username}/{self.github_repo}/releases/tags/{tag}")
            try:
                req = urllib.request.Request(url, method="GET")
                req.add_header("User-Agent", f"{self.app_name}-Updater")
                req.add_header("Accept", "application/vnd.github.v3+json")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    release = json.loads(resp.read().decode("utf-8"))
                for asset in release.get("assets", []):
                    name = asset.get("name", "").lower()
                    if name.endswith(ext):
                        return asset.get("browser_download_url", "")
            except Exception:
                continue
        return ""

    def download_update(self, url, checksum="", progress_callback=None):
        """
        Download installer to temp folder, verify SHA256 checksum.
        progress_callback(bytes_downloaded, total_bytes) called periodically.
        Returns path to downloaded file.
        """
        if not url:
            raise ValueError("No download URL provided.")

        filename = url.split("/")[-1] or "update_installer"
        download_path = Path(tempfile.gettempdir()) / filename

        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", f"{self.app_name}-Updater")
            resp = urllib.request.urlopen(req, timeout=120)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise ConnectionError(f"Download failed: {e}")

        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 65536

        sha = hashlib.sha256()
        with open(download_path, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    try:
                        progress_callback(downloaded, total)
                    except Exception:
                        pass

        resp.close()

        # Verify checksum
        if checksum:
            actual = sha.hexdigest()
            if actual.lower() != checksum.lower():
                try:
                    download_path.unlink()
                except Exception:
                    pass
                raise ValueError(
                    f"Checksum mismatch.\n"
                    f"Expected: {checksum}\n"
                    f"Got: {actual}\n"
                    f"Download may be corrupted — please try again."
                )

        return str(download_path)

    def install_update(self, installer_path, status_callback=None):
        """
        Install the update and relaunch.
        - Windows: runs the .exe installer, exits current app
        - macOS:   mounts DMG, copies .app to /Applications, relaunches
        status_callback(message) is called with progress messages.
        """
        if not os.path.exists(installer_path):
            raise FileNotFoundError(f"Installer not found: {installer_path}")

        def _status(msg):
            if status_callback:
                try:
                    status_callback(msg)
                except Exception:
                    pass

        if IS_WIN:
            self._install_windows(installer_path, _status)

        elif IS_MAC:
            self._install_mac(installer_path, _status)

        else:
            # Linux fallback
            _status("Opening installer...")
            subprocess.Popen(["xdg-open", installer_path])
            os._exit(0)

    def _install_windows(self, new_exe_path, _status):
        """
        Windows auto-install using a .bat script.
        Can't overwrite a running .exe, so:
        1. Write a batch script that waits for this process to exit
        2. Replaces the old .exe with the new one
        3. Relaunches from the original location
        4. Cleans up temp files
        """
        import time

        # sys.executable = the currently running .exe (original location)
        current_exe = os.path.realpath(sys.executable)
        my_pid = os.getpid()

        _status("Preparing update...")

        bat_content = f'''@echo off
:: Wait for the current app to exit (check every second, up to 30s)
set /a count=0
:waitloop
tasklist /FI "PID eq {my_pid}" 2>NUL | find /I "{my_pid}" >NUL
if errorlevel 1 goto :done_waiting
set /a count+=1
if %count% geq 30 goto :done_waiting
timeout /t 1 /nobreak >NUL
goto :waitloop
:done_waiting

:: Extra pause to ensure all file handles are released
timeout /t 3 /nobreak >NUL

:: Delete the old exe (retry up to 3 times)
set /a retries=0
:delete_retry
del /f /q "{current_exe}" >NUL 2>&1
if not exist "{current_exe}" goto :delete_ok
set /a retries+=1
if %retries% geq 3 goto :delete_ok
timeout /t 2 /nobreak >NUL
goto :delete_retry
:delete_ok

:: Copy the new exe to the original location
copy /y "{new_exe_path}" "{current_exe}" >NUL 2>&1
if errorlevel 1 (
    :: Copy failed — try move instead
    move /y "{new_exe_path}" "{current_exe}" >NUL 2>&1
)

:: Verify the copy succeeded and the file is valid
if not exist "{current_exe}" (
    echo Update failed: new exe not found at destination.
    pause
    goto :cleanup
)

:: Wait for the file to be fully written to disk
timeout /t 2 /nobreak >NUL

:: Relaunch the updated app
start "" "{current_exe}"

:cleanup
:: Clean up downloaded file
del /f /q "{new_exe_path}" >NUL 2>&1

:: Delete this batch file
(goto) 2>nul & del /f /q "%~f0"
'''

        bat_path = os.path.join(tempfile.gettempdir(), "anemi_update.bat")
        with open(bat_path, "w") as f:
            f.write(bat_content)

        _status("Installing update — app will restart...")

        # Launch the batch script hidden and detached
        subprocess.Popen(
            ["cmd", "/c", bat_path],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            close_fds=True,
        )

        time.sleep(0.5)
        os._exit(0)

    def _install_mac(self, dmg_path, _status):
        """
        macOS auto-install — all done in-process (no shell script).
        1. Mount the DMG
        2. Find the .app inside
        3. Copy it to /Applications (replacing old version)
        4. Unmount the DMG
        5. Relaunch from /Applications
        """
        mount_point = None

        try:
            # ── Step 1: Mount DMG ────────────────────────────────────────
            _status("Mounting disk image...")
            result = subprocess.run(
                ["hdiutil", "attach", dmg_path,
                 "-nobrowse", "-noverify", "-noautoopen"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to mount DMG: {result.stderr}")

            # Parse mount point from hdiutil output (tab-separated)
            for line in result.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 3:
                    mount_point = parts[-1].strip()

            if not mount_point or not os.path.isdir(mount_point):
                raise RuntimeError("Could not determine DMG mount point.")

            # ── Step 2: Find the .app ────────────────────────────────────
            _status("Finding application...")
            app_name = None
            for item in os.listdir(mount_point):
                if item.endswith(".app"):
                    app_name = item
                    break

            if not app_name:
                raise RuntimeError("No .app found inside the DMG.")

            source_app = os.path.join(mount_point, app_name)
            dest_app = os.path.join("/Applications", app_name)

            # ── Step 3: Remove old version and copy new one ──────────────
            _status("Installing update...")
            if os.path.exists(dest_app):
                shutil.rmtree(dest_app)

            shutil.copytree(source_app, dest_app)
            _status("Update installed!")

            # ── Step 4: Unmount DMG ──────────────────────────────────────
            try:
                subprocess.run(
                    ["hdiutil", "detach", mount_point, "-quiet"],
                    timeout=30, capture_output=True
                )
            except Exception:
                pass  # Non-critical

            # Clean up downloaded DMG
            try:
                os.remove(dmg_path)
            except Exception:
                pass

            # ── Step 5: Relaunch ─────────────────────────────────────────
            _status("Relaunching...")
            import time
            time.sleep(1)  # brief pause to let OS release locks
            subprocess.Popen(["open", "-n", "-a", dest_app])
            time.sleep(0.5)
            os._exit(0)  # force-kill current process

        except Exception as e:
            # Try to unmount on error
            if mount_point:
                try:
                    subprocess.run(
                        ["hdiutil", "detach", mount_point, "-quiet"],
                        timeout=15, capture_output=True
                    )
                except Exception:
                    pass
            raise RuntimeError(f"macOS install failed: {e}")

    def is_mandatory_update(self, server_data):
        """Returns True if the update is mandatory or below minimum version."""
        if server_data.get("mandatory", False):
            return True
        minimum = server_data.get("minimum_version", "0.0.0")
        if compare_versions(self.current_version, minimum) < 0:
            return True
        return False

    def silent_auto_update(self):
        """
        Fully silent auto-update. Call BEFORE launching the main app.
        - Checks GitHub for a new version
        - If found, downloads and installs silently
        - Relaunches the new version, exits current process
        - If no update or any error, returns silently so the app can start normally
        """
        try:
            result = self.check_for_updates()
        except Exception:
            return  # No internet or error — skip silently

        if not result.get("update_available"):
            return  # Already up to date

        url = result.get("download_url", "")
        checksum = result.get("checksum", "")

        if not url:
            return  # No download URL — skip

        try:
            # Download
            installer_path = self.download_update(url, checksum)
            # Install and relaunch (this calls os._exit on success)
            self.install_update(installer_path)
        except Exception:
            return  # Download or install failed — skip, launch current version

    def check_and_prompt(self, parent_window=None):
        """
        Check for updates in a background thread.
        If update found, shows the update dialog.
        Never blocks or slows down app launch.
        If no internet or check fails, silently skips.
        """
        def _worker():
            try:
                result = self.check_for_updates()
            except Exception:
                return  # Silently skip

            if not result.get("update_available"):
                return

            # Schedule dialog on main thread
            if parent_window:
                delay = 50 if result.get("mandatory") else 500
                try:
                    parent_window.after(delay, lambda: _show(result))
                except Exception:
                    pass

        def _show(result):
            try:
                from .update_dialog import show_update_dialog
                show_update_dialog(parent_window, self, result)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()
