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
        Windows auto-install using rename-and-replace (no batch scripts).

        Windows allows RENAMING a running exe, just not deleting or overwriting it.
        So we:
        1. Rename current exe → .exe.old  (allowed while running)
        2. Copy new exe → original path
        3. Launch the new exe
        4. Exit this process
        On next startup, the .exe.old gets cleaned up.
        """
        import time

        # If not running as a frozen PyInstaller exe, just launch the new one
        if not getattr(sys, 'frozen', False):
            _status("Launching update...")
            subprocess.Popen([new_exe_path], shell=True)
            time.sleep(0.5)
            os._exit(0)

        current_exe = Path(sys.executable)
        backup_exe = current_exe.with_name(current_exe.name + '.old')

        try:
            # Remove leftover backup from a previous update
            if backup_exe.exists():
                try:
                    backup_exe.unlink()
                except Exception:
                    pass

            # Step 1: Rename the running exe (Windows allows this)
            _status("Applying update...")
            current_exe.rename(backup_exe)

            # Step 2: Copy the new exe to the original location
            shutil.copy2(new_exe_path, str(current_exe))

            # Step 3: Clean up the downloaded temp file
            try:
                Path(new_exe_path).unlink()
            except Exception:
                pass

            # Step 4: Launch the new exe and exit
            _status("Restarting with new version...")
            subprocess.Popen([str(current_exe)])
            time.sleep(0.5)
            os._exit(0)

        except Exception as e:
            # If rename succeeded but copy failed, restore the original
            if backup_exe.exists() and not current_exe.exists():
                try:
                    backup_exe.rename(current_exe)
                except Exception:
                    pass
            raise RuntimeError(f"Update failed: {e}")

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
        "