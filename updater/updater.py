"""
Updater — checks GitHub for new versions, downloads and installs updates.
Works with any Python tkinter app.  All app-specific values passed as parameters.

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
            app_name="ANEMI Room Charge"
        )
        updater.check_and_prompt(parent_window=root)
    """

    def __init__(self, current_version=None, github_username="",
                 github_repo="", app_name="App", version_file=None):
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
        Check GitHub for new version.
        Returns dict with update info.
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
        # Try both 'v1.2.4' and '1.2.4' tag formats
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
            _status("Launching installer...")
            subprocess.Popen([installer_path], shell=True)
            import time
            time.sleep(0.5)
            os._exit(0)
        elif IS_MAC:
            self._install_mac(installer_path, _status)
        else:
            # Linux fallback
            _status("Opening installer...")
            subprocess.Popen(["xdg-open", installer_path])
            os._exit(0)

    def _install_mac(self, dmg_path, _status):
        """
        macOS auto-install using a background shell script.

        The script runs AFTER the app exits, so there are no file-lock
        issues when replacing the .app bundle in /Applications.

        Flow:
        1. Write a temp shell script that will:
           a. Wait for this process to exit
           b. Mount the DMG
           c. Copy the .app to /Applications
           d. Unmount the DMG
           e. Relaunch the new .app
           f. Clean up
        2. Launch the script in the background
        3. Exit the current app immediately
        """
        import tempfile

        _status("Preparing update...")

        # Build a shell script that does the heavy lifting after we quit
        script_content = f'''#!/bin/bash
# Wait for the current app to fully exit
sleep 2

# Mount the DMG
MOUNT_OUTPUT=$(hdiutil attach "{dmg_path}" -nobrowse -noverify -noautoopen 2>&1)
if [ $? -ne 0 ]; then
    osascript -e 'display alert "Update Failed" message "Could not mount the disk image. Please install manually from the Downloads folder."'
    exit 1
fi

# Find mount point (last column of last line)
MOUNT_POINT=$(echo "$MOUNT_OUTPUT" | tail -1 | awk '{{for(i=3;i<=NF;i++) printf "%s ", $i; print ""}}' | sed 's/ *$//')

if [ ! -d "$MOUNT_POINT" ]; then
    osascript -e 'display alert "Update Failed" message "Could not find mounted volume."'
    exit 1
fi

# Find the .app inside
APP_NAME=$(ls "$MOUNT_POINT" | grep '\\.app$' | head -1)
if [ -z "$APP_NAME" ]; then
    hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null
    osascript -e 'display alert "Update Failed" message "No application found in the disk image."'
    exit 1
fi

SOURCE="$MOUNT_POINT/$APP_NAME"
DEST="/Applications/$APP_NAME"

# Remove old version and copy new one
rm -rf "$DEST" 2>/dev/null
cp -R "$SOURCE" "$DEST"

if [ $? -ne 0 ]; then
    hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null
    osascript -e 'display alert "Update Failed" message "Could not copy to Applications. Try dragging the app manually."'
    open "$MOUNT_POINT"
    exit 1
fi

# Unmount and clean up
hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null
rm -f "{dmg_path}" 2>/dev/null

# Relaunch
sleep 1
open -n -a "$DEST"

# Delete this script
rm -f "$0"
'''

        # Write script to temp file
        script_path = os.path.join(tempfile.gettempdir(), "anemi_update.sh")
        with open(script_path, "w") as f:
            f.write(script_content)
        os.chmod(script_path, 0o755)

        _status("Installing update — app will restart...")

        # Launch the script in background and exit
        subprocess.Popen(
            ["/bin/bash", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        import time
        time.sleep(0.5)
        os._exit(0)

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
