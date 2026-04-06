"""
Universal Auto-Update System for ANEMI Room Charge Importer.
Drop this package into any tkinter app for GitHub-based updates.
"""

from .updater import Updater
from .update_dialog import show_update_dialog
from .version_manager import get_version, compare_versions, should_update

__all__ = [
    "Updater",
    "show_update_dialog",
    "get_version",
    "compare_versions",
    "should_update",
]
