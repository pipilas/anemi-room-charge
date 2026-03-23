#!/usr/bin/env python3
"""
Toast Sales Importer — Stamhad Software
========================================
Downloads nightly Toast POS export files directly via SFTP using an SSH
private key, and displays the CSV contents in a table viewer.

Single-file, production-ready.  Python 3 + tkinter + paramiko.

Install dependencies:  pip install paramiko reportlab
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import platform
import sys
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import tkinter as tk

# ── Auth module ──────────────────────────────────────────────────────────────
try:
    import auth_manager as auth
    AUTH_OK = True
except ImportError:
    AUTH_OK = False

# ── Paramiko availability check ──────────────────────────────────────────────
# Install with:  pip install paramiko
try:
    import paramiko
    PARAMIKO_OK = True
except ImportError:
    PARAMIKO_OK = False

# ── ReportLab availability check ─────────────────────────────────────────────
# Install with:  pip install reportlab
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch, mm
    from reportlab.pdfgen import canvas as rl_canvas
    REPORTLAB_OK = True
except ImportError:
    REPORTLAB_OK = False

# ═══════════════════════════════════════════════════════════════════════════════
#  VERSION & UPDATE CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
APP_VERSION = "1.0.7"
GITHUB_USERNAME = "pipilas"
GITHUB_REPO = "anemi-room-charge"

# ── Updater (auto-download .app / .exe from GitHub Releases) ────────────
try:
    from updater import Updater as _Updater
    _app_updater = _Updater(
        current_version=APP_VERSION,
        github_username=GITHUB_USERNAME,
        github_repo=GITHUB_REPO,
        app_name="ANEMI Room Charge",
    )
    UPDATER_OK = True
except Exception:
    _app_updater = None
    UPDATER_OK = False

# ═══════════════════════════════════════════════════════════════════════════════
#  CHARGE RECEIPT DATA & LOGIC  (Room Charge / Hotel Charge / Voucher)
# ═══════════════════════════════════════════════════════════════════════════════

# Other-Type values we track (lowercase → display label)
TRACKED_CHARGE_TYPES: dict[str, str] = {
    "room charge": "Room Charge",
    "hotel charge": "Hotel Charge",
    "voucher": "Voucher",
}

# Badge colors per charge type for the UI
CHARGE_TYPE_COLORS: dict[str, str] = {
    "Room Charge":  "#4F46E5",  # Indigo (ACCENT)
    "Hotel Charge": "#D97706",  # Amber
    "Voucher":      "#7C3AED",  # Purple
}

@dataclass
class ReceiptItem:
    name: str
    qty: float
    net_price: float
    tax: float

@dataclass
class RoomChargeReceipt:
    date_folder: str          # e.g. "20260318"
    check_id: str
    check_number: str
    tab_name: str             # e.g. "Room 606" or ""
    server: str
    table: str
    paid_date: str
    subtotal: float           # sum of net prices
    tax_total: float          # sum of taxes
    tip: float
    total: float              # payment total from PaymentDetails
    charge_type: str = "Room Charge"  # "Room Charge", "Hotel Charge", or "Voucher"
    items: list[ReceiptItem] = field(default_factory=list)

    @property
    def display_date(self) -> str:
        """Return a human-readable date + time like 'March 18, 2026  9:29 AM'.
        Falls back to folder date if paid_date is unavailable."""
        # Try to parse the paid_date field first (e.g. "3/18/26 9:29 AM")
        if self.paid_date:
            for fmt in ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p",
                        "%m/%d/%y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p"):
                try:
                    dt = datetime.datetime.strptime(self.paid_date.strip(), fmt)
                    date_part = dt.strftime("%B %d, %Y")
                    time_part = dt.strftime("%I:%M %p").lstrip("0")
                    return f"{date_part}  {time_part}"
                except ValueError:
                    continue
        # Fallback: folder date without time
        try:
            d = datetime.datetime.strptime(self.date_folder, "%Y%m%d")
            return d.strftime("%B %d, %Y")
        except ValueError:
            return self.date_folder


def find_room_charges(export_dir: Path) -> list[RoomChargeReceipt]:
    """Scan all date folders under *export_dir* for tracked charge payments
    (Room Charge, Hotel Charge, Voucher) and build receipts by
    cross-referencing ItemSelectionDetails."""
    receipts: list[RoomChargeReceipt] = []

    for day_dir in sorted(export_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        pay_file = day_dir / "PaymentDetails.csv"
        item_file = day_dir / "ItemSelectionDetails.csv"
        if not pay_file.exists() or not item_file.exists():
            continue

        date_folder = day_dir.name

        # ── 1. Find tracked charge payments ──────────────────────────────
        matched_checks: dict[str, tuple[dict, str]] = {}  # check_id -> (row, charge_type_label)
        with open(pay_file, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                other_type = (row.get("Other Type") or "").strip().lower()
                label = TRACKED_CHARGE_TYPES.get(other_type)
                if label:
                    cid = row.get("Check Id", "").strip()
                    if cid:
                        matched_checks[cid] = (row, label)

        if not matched_checks:
            continue

        # ── 2. Collect items for those checks ────────────────────────────
        items_by_check: dict[str, list[ReceiptItem]] = {k: [] for k in matched_checks}
        tab_by_check: dict[str, str] = {}
        with open(item_file, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                cid = row.get("Check Id", "").strip()
                if cid not in items_by_check:
                    continue
                voided = (row.get("Void?") or "").strip().lower()
                if voided == "true":
                    continue
                try:
                    qty = float(row.get("Qty") or 0)
                    net = float(row.get("Net Price") or 0)
                    tax = float(row.get("Tax") or 0)
                except ValueError:
                    qty, net, tax = 0.0, 0.0, 0.0
                items_by_check[cid].append(ReceiptItem(
                    name=(row.get("Menu Item") or "").strip(),
                    qty=qty,
                    net_price=net,
                    tax=tax,
                ))
                tab = (row.get("Tab Name") or "").strip()
                if tab:
                    tab_by_check[cid] = tab

        # ── 3. Build receipt objects ─────────────────────────────────────
        for cid, (pay, charge_label) in matched_checks.items():
            items = items_by_check.get(cid, [])
            subtotal = sum(i.net_price for i in items)
            tax_total = sum(i.tax for i in items)
            try:
                tip = float(pay.get("Tip") or 0)
            except ValueError:
                tip = 0.0
            try:
                total = float(pay.get("Total") or 0)
            except ValueError:
                total = 0.0

            receipts.append(RoomChargeReceipt(
                date_folder=date_folder,
                check_id=cid,
                check_number=(pay.get("Check #") or "").strip(),
                tab_name=tab_by_check.get(cid, (pay.get("Tab Name") or "").strip()),
                server=(pay.get("Server") or "").strip(),
                table=(pay.get("Table") or "").strip(),
                paid_date=(pay.get("Paid Date") or "").strip(),
                subtotal=subtotal,
                tax_total=tax_total,
                tip=tip,
                total=total,
                charge_type=charge_label,
                items=items,
            ))

    return receipts


def generate_receipt_pdf(receipt: RoomChargeReceipt, out_path: Path):
    """Render a single room-charge receipt as a professional PDF."""
    w, h = letter  # 612 x 792 points
    c = rl_canvas.Canvas(str(out_path), pagesize=letter)

    # ── Margins & positions ──────────────────────────────────────────────
    left = 60
    right = w - 60
    content_w = right - left
    top = h - 50
    y = top

    # ── Helper: horizontal rule ──────────────────────────────────────────
    def hline(y_pos, weight=0.5):
        c.setStrokeColorRGB(0.78, 0.80, 0.82)  # BORDER #E5E7EB
        c.setLineWidth(weight)
        c.line(left, y_pos, right, y_pos)

    # ── Header ───────────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 18)
    c.setFillColorRGB(0.106, 0.165, 0.290)  # BG_NAV #1B2A4A
    c.drawString(left, y, "ANEMI")
    y -= 18
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.420, 0.443, 0.502)  # FG_SEC #6B7280
    c.drawString(left, y, "Restaurant")
    y -= 30

    # ── Charge type title ─────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(0.310, 0.275, 0.898)  # ACCENT #4F46E5
    c.drawString(left, y, f"{receipt.charge_type.upper()} RECEIPT")
    y -= 24

    hline(y)
    y -= 18

    # ── Meta info ────────────────────────────────────────────────────────
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.110, 0.110, 0.118)  # FG #1C1C1E
    meta_lines = [
        ("Date:", receipt.display_date),
        ("Check #:", receipt.check_number or "—"),
    ]
    if receipt.tab_name:
        meta_lines.append(("Room / Tab:", receipt.tab_name))
    if receipt.server:
        meta_lines.append(("Server:", receipt.server))
    if receipt.table:
        meta_lines.append(("Table:", receipt.table))

    for label, value in meta_lines:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(left, y, label)
        c.setFont("Helvetica", 10)
        c.drawString(left + 90, y, value)
        y -= 16

    y -= 8
    hline(y)
    y -= 20

    # ── Column headers ───────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0.216, 0.255, 0.318)  # FG_HDR #374151
    c.drawString(left, y, "Item")
    c.drawRightString(right - 120, y, "Qty")
    c.drawRightString(right - 50, y, "Price")
    c.drawRightString(right, y, "Tax")
    y -= 6
    hline(y, weight=1)
    y -= 16

    # ── Item rows (alternating background) ───────────────────────────────
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.110, 0.110, 0.118)
    row_h = 18
    for i, item in enumerate(receipt.items):
        # Alternating row bg
        if i % 2 == 1:
            c.saveState()
            c.setFillColorRGB(0.933, 0.945, 0.957)  # ROW_B #EEF1F4
            c.rect(left - 4, y - 4, content_w + 8, row_h, fill=True, stroke=False)
            c.restoreState()
            c.setFillColorRGB(0.110, 0.110, 0.118)

        name = item.name if len(item.name) <= 40 else item.name[:37] + "..."
        c.drawString(left, y, name)
        c.drawRightString(right - 120, y, f"{item.qty:g}")
        c.drawRightString(right - 50, y, f"${item.net_price:,.2f}")
        c.drawRightString(right, y, f"${item.tax:,.2f}")
        y -= row_h

        # Page break if running out of room
        if y < 120:
            c.showPage()
            y = top
            c.setFont("Helvetica", 10)
            c.setFillColorRGB(0.110, 0.110, 0.118)

    # ── Totals ───────────────────────────────────────────────────────────
    y -= 6
    hline(y, weight=1)
    y -= 20

    totals_x_label = right - 150
    totals_x_value = right

    def _total_line(label, amount, bold=False):
        nonlocal y
        font = "Helvetica-Bold" if bold else "Helvetica"
        c.setFont(font, 11 if bold else 10)
        c.setFillColorRGB(0.110, 0.110, 0.118)
        c.drawRightString(totals_x_label, y, label)
        c.drawRightString(totals_x_value, y, f"${amount:,.2f}")
        y -= 18

    _total_line("Subtotal:", receipt.subtotal)
    _total_line("Tax:", receipt.tax_total)
    if receipt.tip > 0:
        _total_line("Tip:", receipt.tip)
    y -= 2
    hline(y + 12, weight=0.5)
    _total_line("Total:", receipt.total, bold=True)

    # ── Footer ───────────────────────────────────────────────────────────
    y -= 20
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.420, 0.443, 0.502)
    c.drawCentredString(w / 2, y, f"Payment Method: {receipt.charge_type}")
    y -= 14
    c.drawCentredString(w / 2, y, "Thank you for dining with us!")

    # ── Watermark-style footer ───────────────────────────────────────────
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.700, 0.720, 0.740)
    c.drawCentredString(w / 2, 30,
                        "Generated by Toast Sales Importer — Stamhad Software")

    c.save()


# ── Deduplication ledger ────────────────────────────────────────────────────

LEDGER_FILENAME = ".generated_receipts.json"

def _load_ledger(receipts_dir: Path) -> set[str]:
    """Load the set of already-generated receipt keys (date_checkid)."""
    ledger_path = receipts_dir / LEDGER_FILENAME
    if ledger_path.exists():
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            return set(data)
        except Exception:
            return set()
    return set()


def _save_ledger(receipts_dir: Path, keys: set[str]):
    """Persist the ledger of generated receipt keys."""
    ledger_path = receipts_dir / LEDGER_FILENAME
    ledger_path.write_text(json.dumps(sorted(keys), indent=2), encoding="utf-8")


def _receipt_key(r: RoomChargeReceipt) -> str:
    """Unique key for deduplication: date + check_id."""
    return f"{r.date_folder}_{r.check_id}"


# ── Combined PDF with summary page ─────────────────────────────────────────

def generate_combined_pdf(receipts: list[RoomChargeReceipt], out_path: Path):
    """Create a single PDF with a summary first page followed by all receipts."""
    w, h = letter
    c = rl_canvas.Canvas(str(out_path), pagesize=letter)
    left = 60
    right = w - 60
    content_w = right - left

    def hline(y_pos, weight=0.5):
        c.setStrokeColorRGB(0.78, 0.80, 0.82)
        c.setLineWidth(weight)
        c.line(left, y_pos, right, y_pos)

    # ══════════════════════════════════════════════════════════════════════
    #  PAGE 1: SUMMARY
    # ══════════════════════════════════════════════════════════════════════
    y = h - 50

    # Header
    c.setFont("Helvetica-Bold", 22)
    c.setFillColorRGB(0.106, 0.165, 0.290)
    c.drawString(left, y, "ANEMI")
    y -= 18
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.420, 0.443, 0.502)
    c.drawString(left, y, "Restaurant")
    y -= 36

    c.setFont("Helvetica-Bold", 16)
    c.setFillColorRGB(0.310, 0.275, 0.898)
    c.drawString(left, y, "CHARGE SUMMARY")
    y -= 28

    hline(y)
    y -= 24

    # Date range — show full month (1st to last day) if all receipts
    # fall within a single month; otherwise show actual date range
    if receipts:
        dates = sorted(set(r.date_folder for r in receipts))
        try:
            first_dt = datetime.datetime.strptime(dates[0], "%Y%m%d")
            last_dt = datetime.datetime.strptime(dates[-1], "%Y%m%d")
            # Check if all receipts are in the same month
            if first_dt.year == last_dt.year and first_dt.month == last_dt.month:
                import calendar as cal_mod
                _, last_day = cal_mod.monthrange(first_dt.year, first_dt.month)
                month_start = first_dt.replace(day=1)
                month_end = first_dt.replace(day=last_day)
                date_range = (f"{month_start.strftime('%B %d, %Y')} — "
                              f"{month_end.strftime('%B %d, %Y')}")
            else:
                d_start = first_dt.strftime("%B %d, %Y")
                d_end = last_dt.strftime("%B %d, %Y")
                date_range = f"{d_start} — {d_end}"
        except ValueError:
            date_range = f"{dates[0]} — {dates[-1]}"
    else:
        date_range = "—"

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.110, 0.110, 0.118)
    c.drawString(left, y, "Period:")
    c.setFont("Helvetica", 11)
    c.drawString(left + 90, y, date_range)
    y -= 18
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Total Charges:")
    c.setFont("Helvetica", 11)
    c.drawString(left + 90, y, str(len(receipts)))
    y -= 30

    hline(y)
    y -= 24

    # Grand totals
    grand_subtotal = sum(r.subtotal for r in receipts)
    grand_tax = sum(r.tax_total for r in receipts)
    grand_tip = sum(r.tip for r in receipts)
    grand_total = sum(r.total for r in receipts)

    c.setFont("Helvetica-Bold", 13)
    c.setFillColorRGB(0.216, 0.255, 0.318)
    c.drawString(left, y, "TOTALS")
    y -= 24

    summary_rows = [
        ("Sales (Subtotal):", grand_subtotal, False),
        ("Tax:", grand_tax, False),
        ("Tips:", grand_tip, False),
    ]
    for label, amount, bold in summary_rows:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 12)
        c.setFillColorRGB(0.110, 0.110, 0.118)
        c.drawString(left + 20, y, label)
        c.drawRightString(right, y, f"${amount:,.2f}")
        y -= 22

    y -= 4
    hline(y + 12, weight=1)
    y -= 6
    c.setFont("Helvetica-Bold", 14)
    c.setFillColorRGB(0.106, 0.165, 0.290)
    c.drawString(left + 20, y, "Grand Total:")
    c.drawRightString(right, y, f"${grand_total:,.2f}")
    y -= 36

    hline(y)
    y -= 28

    # Per-charge breakdown table
    c.setFont("Helvetica-Bold", 13)
    c.setFillColorRGB(0.216, 0.255, 0.318)
    c.drawString(left, y, "CHARGE BREAKDOWN")
    y -= 22

    # Table header
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.216, 0.255, 0.318)
    c.drawString(left, y, "Date")
    c.drawString(left + 60, y, "Type")
    c.drawString(left + 130, y, "Check #")
    c.drawString(left + 185, y, "Tab")
    c.drawRightString(right - 150, y, "Subtotal")
    c.drawRightString(right - 90, y, "Tax")
    c.drawRightString(right - 40, y, "Tip")
    c.drawRightString(right, y, "Total")
    y -= 6
    hline(y, weight=1)
    y -= 16

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.110, 0.110, 0.118)
    row_h = 18
    for i, r in enumerate(receipts):
        if y < 80:
            # Watermark on this page
            c.setFont("Helvetica", 7)
            c.setFillColorRGB(0.700, 0.720, 0.740)
            c.drawCentredString(w / 2, 30,
                                "Generated by Toast Sales Importer — Stamhad Software")
            c.showPage()
            y = h - 50
            c.setFont("Helvetica", 9)
            c.setFillColorRGB(0.110, 0.110, 0.118)

        # Alternating row bg
        if i % 2 == 1:
            c.saveState()
            c.setFillColorRGB(0.933, 0.945, 0.957)
            c.rect(left - 4, y - 4, content_w + 8, row_h, fill=True, stroke=False)
            c.restoreState()
            c.setFillColorRGB(0.110, 0.110, 0.118)
            c.setFont("Helvetica", 9)

        try:
            d = datetime.datetime.strptime(r.date_folder, "%Y%m%d")
            short_date = d.strftime("%m/%d/%y")
        except ValueError:
            short_date = r.date_folder

        tab = r.tab_name if len(r.tab_name) <= 14 else r.tab_name[:11] + "..."
        type_short = r.charge_type.replace(" Charge", "")
        c.drawString(left, y, short_date)
        c.drawString(left + 60, y, type_short)
        c.drawString(left + 130, y, f"#{r.check_number}")
        c.drawString(left + 185, y, tab or "—")
        c.drawRightString(right - 150, y, f"${r.subtotal:,.2f}")
        c.drawRightString(right - 90, y, f"${r.tax_total:,.2f}")
        c.drawRightString(right - 40, y, f"${r.tip:,.2f}")
        c.drawRightString(right, y, f"${r.total:,.2f}")
        y -= row_h

    # Summary page watermark
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.700, 0.720, 0.740)
    c.drawCentredString(w / 2, 30,
                        "Generated by Toast Sales Importer — Stamhad Software")

    # ══════════════════════════════════════════════════════════════════════
    #  PAGES 2+: Individual receipts
    # ══════════════════════════════════════════════════════════════════════
    for receipt in receipts:
        c.showPage()
        y = h - 50

        # Header
        c.setFont("Helvetica-Bold", 18)
        c.setFillColorRGB(0.106, 0.165, 0.290)
        c.drawString(left, y, "ANEMI")
        y -= 18
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.420, 0.443, 0.502)
        c.drawString(left, y, "Restaurant")
        y -= 30

        c.setFont("Helvetica-Bold", 14)
        c.setFillColorRGB(0.310, 0.275, 0.898)
        c.drawString(left, y, f"{receipt.charge_type.upper()} RECEIPT")
        y -= 24

        hline(y)
        y -= 18

        # Meta info
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.110, 0.110, 0.118)
        meta_lines = [
            ("Date:", receipt.display_date),
            ("Check #:", receipt.check_number or "—"),
        ]
        if receipt.tab_name:
            meta_lines.append(("Room / Tab:", receipt.tab_name))
        if receipt.server:
            meta_lines.append(("Server:", receipt.server))
        if receipt.table:
            meta_lines.append(("Table:", receipt.table))

        for label, value in meta_lines:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(left, y, label)
            c.setFont("Helvetica", 10)
            c.drawString(left + 90, y, value)
            y -= 16

        y -= 8
        hline(y)
        y -= 20

        # Column headers
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.216, 0.255, 0.318)
        c.drawString(left, y, "Item")
        c.drawRightString(right - 120, y, "Qty")
        c.drawRightString(right - 50, y, "Price")
        c.drawRightString(right, y, "Tax")
        y -= 6
        hline(y, weight=1)
        y -= 16

        # Items
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.110, 0.110, 0.118)
        for i, item in enumerate(receipt.items):
            if i % 2 == 1:
                c.saveState()
                c.setFillColorRGB(0.933, 0.945, 0.957)
                c.rect(left - 4, y - 4, content_w + 8, row_h, fill=True, stroke=False)
                c.restoreState()
                c.setFillColorRGB(0.110, 0.110, 0.118)
                c.setFont("Helvetica", 10)

            name = item.name if len(item.name) <= 40 else item.name[:37] + "..."
            c.drawString(left, y, name)
            c.drawRightString(right - 120, y, f"{item.qty:g}")
            c.drawRightString(right - 50, y, f"${item.net_price:,.2f}")
            c.drawRightString(right, y, f"${item.tax:,.2f}")
            y -= row_h

            if y < 120:
                c.showPage()
                y = h - 50
                c.setFont("Helvetica", 10)
                c.setFillColorRGB(0.110, 0.110, 0.118)

        # Totals
        y -= 6
        hline(y, weight=1)
        y -= 20

        totals_x_label = right - 150
        totals_x_value = right

        def _total_line(label, amount, bold=False):
            nonlocal y
            font = "Helvetica-Bold" if bold else "Helvetica"
            c.setFont(font, 11 if bold else 10)
            c.setFillColorRGB(0.110, 0.110, 0.118)
            c.drawRightString(totals_x_label, y, label)
            c.drawRightString(totals_x_value, y, f"${amount:,.2f}")
            y -= 18

        _total_line("Subtotal:", receipt.subtotal)
        _total_line("Tax:", receipt.tax_total)
        if receipt.tip > 0:
            _total_line("Tip:", receipt.tip)
        y -= 2
        hline(y + 12, weight=0.5)
        _total_line("Total:", receipt.total, bold=True)

        # Footer
        y -= 20
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.420, 0.443, 0.502)
        c.drawCentredString(w / 2, y, f"Payment Method: {receipt.charge_type}")
        y -= 14
        c.drawCentredString(w / 2, y, "Thank you for dining with us!")

        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.700, 0.720, 0.740)
        c.drawCentredString(w / 2, 30,
                            "Generated by Toast Sales Importer — Stamhad Software")

    c.save()


def generate_all_receipts(export_dir: Path, log_fn=None) -> tuple[int, Path]:
    """Find all charges and generate PDF receipts (with deduplication).

    Skips receipts already generated (tracked in a JSON ledger).
    Also generates a combined PDF with a summary first page.

    Returns (count_new_generated, receipts_folder).
    """
    all_receipts = find_room_charges(export_dir)
    if not all_receipts:
        return 0, export_dir

    out_dir = export_dir / "charge_receipts"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load dedup ledger
    ledger = _load_ledger(out_dir)
    original_ledger_size = len(ledger)

    new_count = 0
    skipped = 0
    for r in all_receipts:
        key = _receipt_key(r)
        if key in ledger:
            skipped += 1
            if log_fn:
                log_fn(
                    f"[{r.date_folder}] {r.charge_type} — Check #{r.check_number} "
                    f"{'(' + r.tab_name + ') ' if r.tab_name else ''}"
                    f"— already generated, skipping", "info")
            continue

        safe_tab = r.tab_name.replace("/", "-").replace(" ", "_") if r.tab_name else "no_tab"
        type_tag = r.charge_type.lower().replace(" ", "_")
        fname = f"{r.date_folder}_{type_tag}_check{r.check_number}_{safe_tab}.pdf"
        out_path = out_dir / fname
        try:
            generate_receipt_pdf(r, out_path)
            new_count += 1
            ledger.add(key)
            if log_fn:
                log_fn(
                    f"[{r.date_folder}] {r.charge_type}: Check #{r.check_number} "
                    f"{'(' + r.tab_name + ') ' if r.tab_name else ''}"
                    f"— {len(r.items)} items, ${r.total:,.2f}  \u2713",
                    "ok")
        except Exception as exc:
            if log_fn:
                log_fn(f"[{r.date_folder}] Failed to generate receipt for "
                       f"Check #{r.check_number}: {exc}", "err")

    # Save updated ledger
    if len(ledger) > original_ledger_size:
        _save_ledger(out_dir, ledger)

    if skipped and log_fn:
        log_fn(f"\n{skipped} duplicate receipt{'s' if skipped != 1 else ''} skipped.",
               "info")

    # Generate monthly combined PDFs (group receipts by month)
    if all_receipts:
        by_month: dict[str, list[RoomChargeReceipt]] = {}
        for r in all_receipts:
            try:
                dt = datetime.datetime.strptime(r.date_folder, "%Y%m%d")
                month_key = dt.strftime("%m-%Y")  # e.g. "03-2026"
            except ValueError:
                month_key = "unknown"
            by_month.setdefault(month_key, []).append(r)

        for month_key, month_receipts in by_month.items():
            combined_path = out_dir / f"{month_key}.pdf"
            try:
                generate_combined_pdf(month_receipts, combined_path)
                if log_fn:
                    log_fn(
                        f"\nMonthly PDF ({month_key}): {len(month_receipts)} receipt(s) → "
                        f"{month_key}.pdf  \u2713", "ok")
            except Exception as exc:
                if log_fn:
                    log_fn(f"\nFailed to generate {month_key}.pdf: {exc}", "err")

    return new_count, out_dir


# ═══════════════════════════════════════════════════════════════════════════════
#  SFTP CONFIGURATION — ANEMI Data Exports
# ═══════════════════════════════════════════════════════════════════════════════
SFTP_HOST       = "s-9b0f88558b264dfda.server.transfer.us-east-1.amazonaws.com"
SFTP_PORT       = 22
SFTP_USERNAME   = "MamakaMeditteraneanDataExports"
SFTP_EXPORT_ID  = "287721"
def _app_dir() -> Path:
    """Return the real app directory (not the PyInstaller temp dir)."""
    if getattr(sys, 'frozen', False):
        # Bundled app — use the directory containing the executable
        exe = Path(sys.executable).resolve()
        if platform.system() == "Darwin":
            # .app/Contents/MacOS/exe → go up 3 to folder containing .app
            return exe.parent.parent.parent.parent
        return exe.parent
    return Path(__file__).resolve().parent

def _default_data_dir() -> Path:
    """User-writable data directory for keys, exports, etc."""
    if platform.system() == "Darwin":
        d = Path.home() / "Library" / "Application Support" / "AnemiRoomCharge"
    elif platform.system() == "Windows":
        d = Path(os.environ.get("APPDATA", Path.home())) / "AnemiRoomCharge"
    else:
        d = Path.home() / ".anemi-room-charge"
    d.mkdir(parents=True, exist_ok=True)
    return d

SFTP_DEFAULT_KEY = _default_data_dir() / "keys" / "toast_rsa_key"
SFTP_DEFAULT_OUT = _default_data_dir() / "toast_exports"

# Files needed for receipt generation (always downloaded)
SFTP_REQUIRED_FILES = [
    "ItemSelectionDetails.csv",
    "PaymentDetails.csv",
    "CheckDetails.csv",
]

# ═══════════════════════════════════════════════════════════════════════════════
#  PATHS & SETTINGS PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════
APP_DIR    = _default_data_dir()
CONFIG_DIR = APP_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"


def _ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> dict:
    """Load persisted settings (SSH key path, output folder)."""
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_settings(data: dict):
    """Persist settings to disk."""
    _ensure_dirs()
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
#  DESIGN SYSTEM — Stamhad Payroll v1.2
# ═══════════════════════════════════════════════════════════════════════════════

# ── Platform font ─────────────────────────────────────────────────────────────
IS_MAC = platform.system() == "Darwin"
FONT = ("Helvetica Neue" if IS_MAC
        else ("Ubuntu" if platform.system() == "Linux" else "Segoe UI"))

# ── Colour Palette ────────────────────────────────────────────────────────────
BG_PAGE      = "#F0F2F5"
BG_CARD      = "#FFFFFF"
BG_NAV       = "#1B2A4A"
BG_INPUT     = "#FFFFFF"
BORDER       = "#E5E7EB"
BORDER_LT    = "#F3F4F6"
BORDER_FOCUS = "#4F46E5"

FG           = "#1C1C1E"
FG_SEC       = "#6B7280"
FG_HDR       = "#374151"

ACCENT       = "#4F46E5"
ACCENT_HV    = "#4338CA"
SUCCESS      = "#10B981"
SUCCESS_HV   = "#059669"
SUCCESS_BG   = "#D1FAE5"
SUCCESS_FG   = "#065F46"
DANGER       = "#EF4444"
DANGER_HV    = "#DC2626"
EXPORT_BG    = "#0891B2"
EXPORT_HV    = "#0E7490"
WARN_BG      = "#FEF3C7"
WARN_FG      = "#92400E"
WARN_BORD    = "#F59E0B"
WARN_HV      = "#D97706"
CANCEL_BG    = "#FFFFFF"
CANCEL_FG    = "#374151"
CANCEL_BD    = "#D1D5DB"
CANCEL_HV    = "#E5E7EB"

ROW_A        = "#FFFFFF"
ROW_B        = "#EEF1F4"

NAV_APP_NAME = "#7EB8FF"
NAV_INACTIVE = "#94A3B8"
NAV_HOVER    = "#2D4A7A"
NAV_DATE     = "#FFD580"

START_BG     = "#10B981"
START_HV     = "#059669"
START_ACTIVE = "#047857"

# ═══════════════════════════════════════════════════════════════════════════════
#  COMPONENT LIBRARY
# ═══════════════════════════════════════════════════════════════════════════════

class Btn(tk.Frame):
    """Label-based button that renders correctly on all platforms."""
    STYLES = {
        "primary":  (ACCENT,    "#FFFFFF", ACCENT_HV),
        "success":  (SUCCESS,   "#FFFFFF", SUCCESS_HV),
        "danger":   (DANGER,    "#FFFFFF", DANGER_HV),
        "export":   (EXPORT_BG, "#FFFFFF", EXPORT_HV),
        "cancel":   ("#FFFFFF", FG_HDR,    CANCEL_HV),
        "warning":  (WARN_BORD, "#FFFFFF", WARN_HV),
        "ghost":    ("#FFFFFF", FG_HDR,    CANCEL_HV),
        "outline":  ("#FFFFFF", ACCENT,   "#EEF2FF"),
    }

    def __init__(self, parent, text="", command=None, style="primary", **kw):
        bg, fg, hv = self.STYLES.get(style, self.STYLES["primary"])
        bd_color = CANCEL_BD if style in ("cancel", "ghost") else bg
        super().__init__(parent, bg=bg, highlightbackground=bd_color,
                         highlightthickness=1, cursor="hand2")
        self._bg, self._fg, self._hv, self._bd = bg, fg, hv, bd_color
        self._cmd = command
        self._lbl = tk.Label(self, text=text, bg=bg, fg=fg,
                             font=(FONT, 11, "bold"), padx=16, pady=8,
                             cursor="hand2")
        self._lbl.pack()
        for w in (self, self._lbl):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_click)

    def _on_enter(self, _):
        self.config(bg=self._hv, highlightbackground=self._hv)
        self._lbl.config(bg=self._hv)

    def _on_leave(self, _):
        self.config(bg=self._bg, highlightbackground=self._bd)
        self._lbl.config(bg=self._bg)

    def _on_click(self, _):
        if self._cmd:
            self._cmd()

    def set_text(self, text: str):
        self._lbl.config(text=text)


class Inp(tk.Entry):
    """Styled input field with focus-highlight border."""
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG_INPUT, fg=FG, insertbackground=FG,
                         relief="flat", font=(FONT, 12), bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=BORDER_FOCUS, **kw)


class Card(tk.Frame):
    """White container with 1px border."""
    def __init__(self, parent, bg=BG_CARD, **kw):
        super().__init__(parent, bg=bg, highlightbackground=BORDER,
                         highlightthickness=1, padx=16, pady=12, **kw)


class Toast(tk.Toplevel):
    """Floating notification at top-center. Auto-dismisses after *ms* milliseconds."""
    def __init__(self, parent, message: str, ms: int = 3000,
                 bg: str = SUCCESS_BG, fg: str = SUCCESS_FG):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=bg)
        tk.Label(self, text=message, bg=bg, fg=fg,
                 font=(FONT, 12, "bold"), padx=20, pady=10).pack()
        self.update_idletasks()
        pw, px = parent.winfo_width(), parent.winfo_rootx()
        tw = self.winfo_reqwidth()
        x = px + (pw - tw) // 2
        y = parent.winfo_rooty() + 56
        self.geometry(f"+{x}+{y}")
        self.after(ms, self.destroy)


# ═══════════════════════════════════════════════════════════════════════════════
#  DAY DETAIL VIEW — Individual receipts for a selected day
# ═══════════════════════════════════════════════════════════════════════════════

class DayDetailView(tk.Frame):
    """Shows all individual receipts for a single day as a scrollable list."""

    def __init__(self, parent, date_key: str,
                 receipts: list[RoomChargeReceipt], on_back=None, **kw):
        super().__init__(parent, bg=BG_NAV, **kw)
        self._on_back = on_back

        # Parse date
        try:
            dt = datetime.datetime.strptime(date_key, "%Y%m%d")
            title = dt.strftime("%A, %B %d, %Y")
        except ValueError:
            title = date_key

        # ── Top bar ──────────────────────────────────────────────────────
        top = tk.Frame(self, bg=BG_NAV)
        top.pack(fill="x", padx=20, pady=(16, 0))

        back_lbl = tk.Label(
            top, text="\u2190  Back", bg=BG_NAV, fg=NAV_INACTIVE,
            font=(FONT, 12), cursor="hand2")
        back_lbl.pack(side="left")
        back_lbl.bind("<Enter>", lambda e: back_lbl.config(fg="#FFFFFF"))
        back_lbl.bind("<Leave>", lambda e: back_lbl.config(fg=NAV_INACTIVE))
        back_lbl.bind("<Button-1>", lambda e: self._go_back())

        tk.Label(top, text=title, bg=BG_NAV, fg="#FFFFFF",
                 font=(FONT, 18, "bold")).pack(side="left", padx=(16, 0))

        count = len(receipts)
        tk.Label(top, text=f"{count} receipt{'s' if count != 1 else ''}",
                 bg=BG_NAV, fg=NAV_DATE,
                 font=(FONT, 12)).pack(side="right")

        # ── Day totals bar ───────────────────────────────────────────────
        totals_bar = tk.Frame(self, bg="#0F1D36")
        totals_bar.pack(fill="x", padx=20, pady=(12, 0))

        day_sales = sum(r.total for r in receipts)
        day_tips = sum(r.tip for r in receipts)
        day_tax = sum(r.tax_total for r in receipts)

        for label, value, color in [
            ("Total Sales", f"${day_sales:,.2f}", "#6EE7B7"),
            ("Total Tips", f"${day_tips:,.2f}", "#7EB8FF"),
            ("Total Tax", f"${day_tax:,.2f}", "#FFD580"),
        ]:
            cell = tk.Frame(totals_bar, bg="#0F1D36")
            cell.pack(side="left", expand=True, fill="x", padx=12, pady=10)
            tk.Label(cell, text=label, bg="#0F1D36", fg=NAV_INACTIVE,
                     font=(FONT, 10)).pack()
            tk.Label(cell, text=value, bg="#0F1D36", fg=color,
                     font=(FONT, 18, "bold")).pack()

        # ── Scrollable receipt list ──────────────────────────────────────
        list_frame = tk.Frame(self, bg=BG_NAV)
        list_frame.pack(fill="both", expand=True, padx=20, pady=(12, 16))

        canvas = tk.Canvas(list_frame, bg=BG_NAV, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical",
                                  command=canvas.yview)
        scroll_inner = tk.Frame(canvas, bg=BG_NAV)

        scroll_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._canvas_window_id = canvas.create_window((0, 0), window=scroll_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ── Mouse wheel / trackpad scrolling (cross-platform) ─────────
        def _on_mousewheel(event):
            if IS_MAC:
                canvas.yview_scroll(int(-1 * event.delta), "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_linux_scroll_up(event):
            canvas.yview_scroll(-3, "units")

        def _on_linux_scroll_down(event):
            canvas.yview_scroll(3, "units")

        def _bind_scroll(widget):
            """Recursively bind scroll events to a widget and its children."""
            widget.bind("<MouseWheel>", _on_mousewheel)
            widget.bind("<Button-4>", _on_linux_scroll_up)
            widget.bind("<Button-5>", _on_linux_scroll_down)

        # Bind to canvas and inner frame
        _bind_scroll(canvas)
        _bind_scroll(scroll_inner)

        # Also bind globally as fallback
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_linux_scroll_up)
        canvas.bind_all("<Button-5>", _on_linux_scroll_down)

        def _cleanup_scroll(e):
            try:
                canvas.unbind_all("<MouseWheel>")
                canvas.unbind_all("<Button-4>")
                canvas.unbind_all("<Button-5>")
            except Exception:
                pass

        self.bind("<Destroy>", _cleanup_scroll)

        # Store scroll binder so receipt cards can use it
        self._bind_scroll = _bind_scroll

        # ── Render each receipt as a card ────────────────────────────────
        for idx, r in enumerate(sorted(receipts, key=lambda x: x.check_number)):
            self._render_receipt_card(scroll_inner, r, idx)

        # Ensure inner frame fills canvas width
        # Update scroll_inner width when canvas resizes
        def _on_canvas_resize(e):
            canvas.itemconfig(self._canvas_window_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_resize)

    def _render_receipt_card(self, parent, r: RoomChargeReceipt, idx: int):
        """Render a single receipt as a styled card."""
        card_bg = "#1E3358"
        card = tk.Frame(parent, bg=card_bg,
                        highlightbackground="#2D4A7A",
                        highlightthickness=1)
        card.pack(fill="x", pady=(0, 8))

        # ── Header row: check # + tab/room + time ────────────────────
        hdr = tk.Frame(card, bg=card_bg)
        hdr.pack(fill="x", padx=16, pady=(12, 0))

        tk.Label(hdr, text=f"Check #{r.check_number}",
                 bg=card_bg, fg="#FFFFFF",
                 font=(FONT, 14, "bold")).pack(side="left")

        # Charge type badge
        badge_color = CHARGE_TYPE_COLORS.get(r.charge_type, ACCENT)
        tk.Label(hdr, text=f" {r.charge_type} ",
                 bg=badge_color, fg="#FFFFFF",
                 font=(FONT, 9, "bold"),
                 padx=6, pady=2).pack(side="left", padx=(10, 0))

        if r.tab_name:
            tab_label = r.tab_name
            if r.charge_type in ("Room Charge", "Hotel Charge") and not r.tab_name.lower().startswith("room"):
                tab_label = f"Room {r.tab_name}"
            tk.Label(hdr, text=tab_label,
                     bg="#2D4A7A", fg="#FFFFFF",
                     font=(FONT, 10, "bold"),
                     padx=8, pady=2).pack(side="left", padx=(6, 0))

        # Time from display_date
        time_str = ""
        if r.paid_date:
            for fmt in ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p",
                        "%m/%d/%y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p"):
                try:
                    dt = datetime.datetime.strptime(r.paid_date.strip(), fmt)
                    time_str = dt.strftime("%I:%M %p").lstrip("0")
                    break
                except ValueError:
                    continue
        if time_str:
            tk.Label(hdr, text=time_str, bg=card_bg, fg=NAV_INACTIVE,
                     font=(FONT, 11)).pack(side="right")

        # ── Server/table info ─────────────────────────────────────────
        if r.server or r.table:
            info = tk.Frame(card, bg=card_bg)
            info.pack(fill="x", padx=16, pady=(4, 0))
            parts = []
            if r.server:
                parts.append(f"Server: {r.server}")
            if r.table:
                parts.append(f"Table: {r.table}")
            tk.Label(info, text="  |  ".join(parts),
                     bg=card_bg, fg=NAV_INACTIVE,
                     font=(FONT, 10)).pack(side="left")

        # ── Items table ───────────────────────────────────────────────
        if r.items:
            sep = tk.Frame(card, bg="#2D4A7A", height=1)
            sep.pack(fill="x", padx=16, pady=(8, 0))

            items_frame = tk.Frame(card, bg=card_bg)
            items_frame.pack(fill="x", padx=16, pady=(4, 0))

            for item in r.items:
                row = tk.Frame(items_frame, bg=card_bg)
                row.pack(fill="x", pady=1)
                name = item.name if len(item.name) <= 35 else item.name[:32] + "..."
                qty_str = f"x{item.qty:g}" if item.qty != 1 else ""
                tk.Label(row, text=f"  {name}  {qty_str}",
                         bg=card_bg, fg="#94A3B8",
                         font=(FONT, 10), anchor="w").pack(side="left")
                tk.Label(row, text=f"${item.net_price:,.2f}",
                         bg=card_bg, fg="#94A3B8",
                         font=(FONT, 10)).pack(side="right")

        # ── Totals row ────────────────────────────────────────────────
        sep2 = tk.Frame(card, bg="#2D4A7A", height=1)
        sep2.pack(fill="x", padx=16, pady=(8, 0))

        totals = tk.Frame(card, bg=card_bg)
        totals.pack(fill="x", padx=16, pady=(8, 12))

        # Left side: subtotal + tax
        left = tk.Frame(totals, bg=card_bg)
        left.pack(side="left")
        tk.Label(left, text=f"Subtotal: ${r.subtotal:,.2f}",
                 bg=card_bg, fg=NAV_INACTIVE,
                 font=(FONT, 10)).pack(side="left", padx=(0, 12))
        tk.Label(left, text=f"Tax: ${r.tax_total:,.2f}",
                 bg=card_bg, fg=NAV_INACTIVE,
                 font=(FONT, 10)).pack(side="left", padx=(0, 12))
        if r.tip > 0:
            tk.Label(left, text=f"Tip: ${r.tip:,.2f}",
                     bg=card_bg, fg="#7EB8FF",
                     font=(FONT, 10, "bold")).pack(side="left")

        # Right side: total
        tk.Label(totals, text=f"${r.total:,.2f}",
                 bg=card_bg, fg="#6EE7B7",
                 font=(FONT, 16, "bold")).pack(side="right")

        # Bind scroll events to the card and all children
        if hasattr(self, "_bind_scroll"):
            def _bind_recursive(widget):
                self._bind_scroll(widget)
                for child in widget.winfo_children():
                    _bind_recursive(child)
            _bind_recursive(card)

    def _go_back(self):
        if self._on_back:
            self._on_back()


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGIN WINDOW — Email + Password (Firebase Auth)
# ═══════════════════════════════════════════════════════════════════════════════

SUPPORT_EMAIL = "stamhadsoftware@gmail.com"
LOGIN_APP_NAME = "ANEMI"

class LoginWindow(tk.Tk):
    """
    Standalone login window using Firebase Authentication.
    Calls on_success(email, display_name, role, uid, id_token) on login.
    """

    def __init__(self, on_success, prefill_email=""):
        super().__init__()
        self.title("ANEMI — Sign In")
        self.configure(bg=BG_PAGE)
        self.geometry("440x540")
        self.resizable(False, False)
        self._on_success = on_success
        self._prefill_email = prefill_email
        self._build_ui()

        if self._prefill_email:
            self.email_entry.insert(0, self._prefill_email)
            self.remember_var.set(True)
            self.pw_entry.focus_set()

    def _build_ui(self):
        card = tk.Frame(self, bg=BG_CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center", width=380, height=460)

        # ── Branding header ─────────────────────────────────────────────
        tk.Label(card, text=LOGIN_APP_NAME, bg=BG_CARD, fg=BG_NAV,
                 font=(FONT, 26, "bold")).pack(pady=(28, 0))
        tk.Label(card, text="Room Charge Importer", bg=BG_CARD, fg=FG_SEC,
                 font=(FONT, 11)).pack(pady=(2, 4))
        tk.Label(card, text="by Stamhad Software", bg=BG_CARD, fg=FG_SEC,
                 font=(FONT, 9)).pack(pady=(0, 4))
        tk.Frame(card, bg=ACCENT, height=3, width=60).pack(pady=(0, 18))

        # Email
        tk.Label(card, text="Email", bg=BG_CARD, fg=FG,
                 font=(FONT, 11, "bold"), anchor="w").pack(fill="x", padx=40)
        self.email_entry = tk.Entry(card, font=(FONT, 13), width=24,
                                     highlightthickness=2, highlightcolor=ACCENT,
                                     highlightbackground=BORDER, relief="flat",
                                     bg="#F9FAFB")
        self.email_entry.pack(fill="x", padx=40, pady=(4, 14))

        # Password
        tk.Label(card, text="Password", bg=BG_CARD, fg=FG,
                 font=(FONT, 11, "bold"), anchor="w").pack(fill="x", padx=40)
        self.pw_entry = tk.Entry(card, font=(FONT, 13), width=24, show="\u2022",
                                  highlightthickness=2, highlightcolor=ACCENT,
                                  highlightbackground=BORDER, relief="flat",
                                  bg="#F9FAFB")
        self.pw_entry.pack(fill="x", padx=40, pady=(4, 10))

        # Remember me
        self.remember_var = tk.BooleanVar(value=False)
        tk.Checkbutton(card, text="Remember me",
                        variable=self.remember_var, bg=BG_CARD, fg=FG,
                        selectcolor="#F9FAFB", activebackground=BG_CARD,
                        font=(FONT, 10)).pack(anchor="w", padx=38, pady=(0, 6))

        # Error label
        self.err_lbl = tk.Label(card, text="", bg=BG_CARD, fg=DANGER,
                                 font=(FONT, 10, "bold"), wraplength=300)
        self.err_lbl.pack(pady=(0, 6))

        # Sign In button
        self.login_btn = tk.Frame(card, bg=ACCENT, cursor="hand2")
        self.login_btn.pack(pady=(0, 10))
        self.login_btn_lbl = tk.Label(self.login_btn, text="  Sign In  ",
                                       bg=ACCENT, fg="#FFFFFF",
                                       font=(FONT, 13, "bold"),
                                       padx=40, pady=10, cursor="hand2")
        self.login_btn_lbl.pack()

        for w in (self.login_btn, self.login_btn_lbl):
            w.bind("<Button-1>", lambda e: self._do_login())
            w.bind("<Enter>", lambda e: self._btn_hover(True))
            w.bind("<Leave>", lambda e: self._btn_hover(False))

        self.pw_entry.bind("<Return>", lambda e: self._do_login())
        self.email_entry.bind("<Return>", lambda e: self.pw_entry.focus_set())

        # Forgot password
        tk.Label(card,
                 text=f"Forgot password? Contact {SUPPORT_EMAIL}",
                 bg=BG_CARD, fg=FG_SEC, font=(FONT, 9)).pack(pady=(2, 0))

        tk.Label(self, text=f"ANEMI  \u00A9 2026  Stamhad Software",
                 bg=BG_PAGE, fg=FG_SEC, font=(FONT, 9)).pack(side="bottom", pady=12)

    def _btn_hover(self, entering):
        c = ACCENT_HV if entering else ACCENT
        self.login_btn.config(bg=c)
        self.login_btn_lbl.config(bg=c)

    def _do_login(self):
        email = self.email_entry.get().strip()
        password = self.pw_entry.get().strip()
        if not email or not password:
            self.err_lbl.config(text="Please enter email and password.")
            return

        self.err_lbl.config(text="Signing in...", fg=FG_SEC)
        self.update()

        def _auth_thread():
            ok, msg, account = auth.authenticate(email, password)
            self.after(0, lambda: self._handle_result(
                ok, msg, account, email, password))

        threading.Thread(target=_auth_thread, daemon=True).start()

    def _handle_result(self, ok, msg, account, email, password):
        if ok:
            uid = account.get("uid", "")
            display_name = account.get("display_name") or email
            role = account.get("role", "staff")
            id_token = account.get("id_token", "")

            if self.remember_var.get():
                auth.save_session(email, password, display_name)
            else:
                auth.clear_session()

            self.destroy()
            self._on_success(email, display_name, role, uid, id_token)

        elif account and not account.get("enabled", True):
            self.err_lbl.config(
                text=f"Account suspended. Contact {SUPPORT_EMAIL}",
                fg=DANGER)
        else:
            self.err_lbl.config(text=msg, fg=DANGER)


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTO-LOGIN SPLASH  (re-authenticates with Firebase using saved credentials)
# ═══════════════════════════════════════════════════════════════════════════════

class AutoLoginSplash(tk.Tk):
    """
    Small splash shown during auto-login.
    Calls Firebase sign-in with saved credentials.
    If it works -> launches app. If not -> shows login form.
    """

    def __init__(self, session, on_success, on_fail):
        super().__init__()
        self.title("ANEMI")
        self.configure(bg=BG_NAV)
        self.geometry("340x200")
        self.resizable(False, False)
        self._session = session
        self._on_success = on_success
        self._on_fail = on_fail

        # Splash content
        tk.Label(self, text=LOGIN_APP_NAME, bg=BG_NAV, fg="#FFFFFF",
                 font=(FONT, 22, "bold")).pack(pady=(36, 2))
        tk.Label(self, text="Room Charge Importer", bg=BG_NAV, fg=NAV_INACTIVE,
                 font=(FONT, 10)).pack(pady=(0, 4))
        tk.Label(self, text="by Stamhad Software", bg=BG_NAV, fg=FG_SEC,
                 font=(FONT, 9)).pack(pady=(0, 12))
        self._status = tk.Label(self, text="Signing in...", bg=BG_NAV,
                                 fg=NAV_INACTIVE, font=(FONT, 11))
        self._status.pack(pady=(0, 10))

        self.after(100, self._try_auto_login)

    def _try_auto_login(self):
        email = self._session["email"]
        password = self._session["password"]

        def _worker():
            ok, msg, account = auth.authenticate(email, password)
            self.after(0, lambda: self._handle(ok, msg, account, password))

        threading.Thread(target=_worker, daemon=True).start()

    def _handle(self, ok, msg, account, password):
        if ok:
            email = account.get("email", self._session.get("email", ""))
            uid = account.get("uid", "")
            display_name = account.get("display_name") or email
            role = account.get("role", "staff")
            id_token = account.get("id_token", "")

            auth.save_session(email, password, display_name)

            self.destroy()
            self._on_success(email, display_name, role, uid, id_token)
        else:
            print(f"[Auto-login] Failed: {msg}")
            auth.clear_session()
            self.destroy()
            self._on_fail(self._session.get("email", ""))


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def require_login(launch_app_callback):
    """
    Try auto-login first (saved credentials -> Firebase sign-in).
    If no session or auto-login fails -> show the login form.
    launch_app_callback(email, display_name, role, uid, id_token)
    """
    session = auth.load_session()

    if session:
        def on_fail(email=""):
            login_win = LoginWindow(launch_app_callback, prefill_email=email)
            login_win.mainloop()

        splash = AutoLoginSplash(session, launch_app_callback, on_fail)
        splash.mainloop()
    else:
        login_win = LoginWindow(launch_app_callback)
        login_win.mainloop()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION — Clean single-screen with START button
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def __init__(self, user_role="admin", user_email="", user_display_name="",
                 user_uid="", id_token=""):
        super().__init__()
        self.title("ANEMI — Room Charge Importer")
        self.configure(bg=BG_NAV)
        self.minsize(900, 560)
        _ensure_dirs()

        # Store user info BEFORE building UI (role affects visible elements)
        self._user_role = user_role
        self._user_email = user_email
        self._user_display_name = user_display_name
        self._user_uid = user_uid
        self._id_token = id_token

        # Load saved settings
        self._settings = _load_settings()
        self._running = False
        self._pulse_id = None
        self._last_receipts: list[RoomChargeReceipt] = []

        self._build_ui()

        # Try to load existing data on startup
        self.after(200, self._load_existing_data)
        # First-run setup: if admin has no SSH key configured, prompt
        if self._user_role == "admin":
            self.after(400, self._check_first_run_setup)
        # Auto-check for updates on launch (non-blocking)
        if UPDATER_OK and _app_updater:
            _app_updater.check_and_prompt(parent_window=self)

    # ── Build the main screen (with inline calendar) ─────────────────────────
    def _build_ui(self):
        self._main = tk.Frame(self, bg=BG_NAV)
        self._main.pack(fill="both", expand=True)

        # ── Top bar: brand + settings gear ───────────────────────────────
        top_bar = tk.Frame(self._main, bg=BG_NAV)
        top_bar.pack(fill="x", padx=20, pady=(12, 0))

        tk.Label(top_bar, text="ANEMI", bg=BG_NAV, fg="#FFFFFF",
                 font=(FONT, 20, "bold")).pack(side="left")
        tk.Label(top_bar, text="Room Charge Importer", bg=BG_NAV,
                 fg=NAV_INACTIVE, font=(FONT, 11)).pack(side="left", padx=(10, 0))

        # User name + Sign out button (top right)
        user_display = getattr(self, "_user_display_name", "") or getattr(self, "_user_email", "")
        if user_display:
            tk.Label(top_bar, text=user_display, bg=BG_NAV, fg="#5A7BA5",
                     font=(FONT, 10), padx=4).pack(side="right")

        signout_btn = tk.Label(
            top_bar, text="Sign Out", bg=BG_NAV, fg=NAV_INACTIVE,
            font=(FONT, 10), cursor="hand2", padx=8, pady=4)
        signout_btn.pack(side="right")
        signout_btn.bind("<Enter>", lambda e: signout_btn.config(fg="#FCA5A5"))
        signout_btn.bind("<Leave>", lambda e: signout_btn.config(fg=NAV_INACTIVE))
        signout_btn.bind("<Button-1>", lambda e: self._sign_out())

        # Check for Updates button (top right)
        update_btn = tk.Label(
            top_bar, text="\u21BB Update", bg=BG_NAV, fg=NAV_INACTIVE,
            font=(FONT, 10), cursor="hand2", padx=8, pady=4)
        update_btn.pack(side="right")
        update_btn.bind("<Enter>", lambda e: update_btn.config(fg="#6EE7B7"))
        update_btn.bind("<Leave>", lambda e: update_btn.config(fg=NAV_INACTIVE))
        update_btn.bind("<Button-1>", lambda e: self._check_for_updates())

        # Settings gear (top right) — admin only
        settings_btn = tk.Label(
            top_bar, text="\u2699", bg=BG_NAV, fg=NAV_INACTIVE,
            font=(FONT, 18), cursor="hand2", padx=6, pady=4)
        if getattr(self, "_user_role", "admin") == "admin":
            settings_btn.pack(side="right")
        settings_btn.bind("<Enter>", lambda e: settings_btn.config(fg="#FFFFFF"))
        settings_btn.bind("<Leave>", lambda e: settings_btn.config(fg=NAV_INACTIVE))
        settings_btn.bind("<Button-1>", lambda e: self._open_settings())

        # ── Get Weekly Orders button + Export Last Month + status ──────
        action_bar = tk.Frame(self._main, bg=BG_NAV)
        action_bar.pack(fill="x", padx=20, pady=(12, 0))

        # Only show fetch button for admin role
        is_admin = getattr(self, "_user_role", "admin") == "admin"

        self._start_frame = tk.Frame(action_bar, bg=ACCENT,
                                     highlightbackground=ACCENT,
                                     highlightthickness=1, cursor="hand2")
        if is_admin:
            self._start_frame.pack(side="left")
        self._start_lbl = tk.Label(
            self._start_frame, text="Get Weekly Orders", bg=ACCENT,
            fg="#FFFFFF", font=(FONT, 12, "bold"), padx=20, pady=8,
            cursor="hand2")
        self._start_lbl.pack()
        for w in (self._start_frame, self._start_lbl):
            w.bind("<Enter>", self._start_hover_in)
            w.bind("<Leave>", self._start_hover_out)
            w.bind("<Button-1>", lambda e: self._on_start())

        # Export This Month PDF button
        self._export_this_frame = tk.Frame(action_bar, bg=ACCENT,
                                           highlightbackground=ACCENT,
                                           highlightthickness=1, cursor="hand2")
        self._export_this_frame.pack(side="left", padx=(10, 0))
        self._export_this_lbl = tk.Label(
            self._export_this_frame, text="Export This Month", bg=ACCENT,
            fg="#FFFFFF", font=(FONT, 11, "bold"), padx=16, pady=8,
            cursor="hand2")
        self._export_this_lbl.pack()
        for w in (self._export_this_frame, self._export_this_lbl):
            w.bind("<Enter>", lambda e: (
                self._export_this_frame.config(bg=ACCENT_HV, highlightbackground=ACCENT_HV),
                self._export_this_lbl.config(bg=ACCENT_HV)))
            w.bind("<Leave>", lambda e: (
                self._export_this_frame.config(bg=ACCENT, highlightbackground=ACCENT),
                self._export_this_lbl.config(bg=ACCENT)))
            w.bind("<Button-1>", lambda e: self._export_month_pdf("this"))

        # Export Last Month PDF button
        self._export_frame = tk.Frame(action_bar, bg="#374151",
                                      highlightbackground="#374151",
                                      highlightthickness=1, cursor="hand2")
        self._export_frame.pack(side="left", padx=(10, 0))
        self._export_lbl = tk.Label(
            self._export_frame, text="Export Last Month", bg="#374151",
            fg="#FFFFFF", font=(FONT, 11, "bold"), padx=16, pady=8,
            cursor="hand2")
        self._export_lbl.pack()
        for w in (self._export_frame, self._export_lbl):
            w.bind("<Enter>", lambda e: (
                self._export_frame.config(bg="#4B5563", highlightbackground="#4B5563"),
                self._export_lbl.config(bg="#4B5563")))
            w.bind("<Leave>", lambda e: (
                self._export_frame.config(bg="#374151", highlightbackground="#374151"),
                self._export_lbl.config(bg="#374151")))
            w.bind("<Button-1>", lambda e: self._export_month_pdf("last"))

        self._status_lbl = tk.Label(action_bar, text="", bg=BG_NAV,
                                    fg=NAV_INACTIVE, font=(FONT, 11))
        self._status_lbl.pack(side="left", padx=(16, 0))

        # Week navigation (right side) — ‹ Prev | Week Label | Next ›
        nav_frame = tk.Frame(action_bar, bg=BG_NAV)
        nav_frame.pack(side="right")

        self._prev_week_btn = tk.Label(
            nav_frame, text="\u276E", bg=BG_NAV, fg=NAV_INACTIVE,
            font=(FONT, 14, "bold"), cursor="hand2", padx=8)
        self._prev_week_btn.pack(side="left")
        self._prev_week_btn.bind("<Button-1>", lambda e: self._navigate_week(-1))
        self._prev_week_btn.bind("<Enter>",
                                 lambda e: self._prev_week_btn.config(fg="#FFFFFF"))
        self._prev_week_btn.bind("<Leave>",
                                 lambda e: self._prev_week_btn.config(fg=NAV_INACTIVE))

        self._week_label = tk.Label(nav_frame, text="", bg=BG_NAV,
                                    fg=NAV_DATE, font=(FONT, 11))
        self._week_label.pack(side="left", padx=(4, 4))

        self._next_week_btn = tk.Label(
            nav_frame, text="\u276F", bg=BG_NAV, fg=NAV_INACTIVE,
            font=(FONT, 14, "bold"), cursor="hand2", padx=8)
        self._next_week_btn.pack(side="left")
        self._next_week_btn.bind("<Button-1>", lambda e: self._navigate_week(1))
        self._next_week_btn.bind("<Enter>",
                                 lambda e: self._next_week_btn.config(fg="#FFFFFF"))
        self._next_week_btn.bind("<Leave>",
                                 lambda e: self._next_week_btn.config(fg=NAV_INACTIVE))

        # Initialize current week (Monday)
        today = datetime.date.today()
        self._current_monday = today - datetime.timedelta(days=today.weekday())
        dates = [self._current_monday + datetime.timedelta(days=i) for i in range(7)]
        self._update_week_label()

        # ── Day cards row (compact calendar) ─────────────────────────────
        self._cards_frame = tk.Frame(self._main, bg=BG_NAV)
        self._cards_frame.pack(fill="both", expand=True, padx=20, pady=(12, 0))

        for i in range(7):
            self._cards_frame.columnconfigure(i, weight=1, uniform="daycol")
        self._cards_frame.rowconfigure(0, weight=1)

        self._day_cards: list[dict] = []
        self._dates = dates

        for col, dt in enumerate(self._dates):
            self._build_day_card(col, dt)

        # ── Log area (hidden until running) ──────────────────────────────
        self._log_frame = tk.Frame(self._main, bg=BG_NAV)

        self._log = tk.Text(self._log_frame, bg="#0F1D36", fg="#A0CFFF",
                            font=("Courier", 9), height=6, relief="flat",
                            bd=0, wrap="word", highlightthickness=1,
                            highlightbackground="#2D4A7A",
                            highlightcolor=ACCENT, state="disabled",
                            padx=10, pady=6)
        self._log.pack(fill="both", expand=True, padx=20, pady=(0, 6))
        self._log.tag_configure("info", foreground="#7B9EC4")
        self._log.tag_configure("ok", foreground="#6EE7B7")
        self._log.tag_configure("warn", foreground="#FCD34D")
        self._log.tag_configure("err", foreground="#FCA5A5")
        self._log.tag_configure("bold", foreground="#FFFFFF",
                                font=("Courier", 9, "bold"))

        # ── Footer ──────────────────────────────────────────────────────
        tk.Label(self._main, text="Stamhad Software", bg=BG_NAV,
                 fg="#3A5278", font=(FONT, 9)).pack(side="bottom", pady=(0, 8))

    # ── Build a single day card ───────────────────────────────────────────────
    def _build_day_card(self, col: int, dt: datetime.date,
                        receipts: list[RoomChargeReceipt] | None = None):
        is_today = dt == datetime.date.today()
        card_bg = "#1E3A5F" if is_today else "#162240"
        count = len(receipts) if receipts else 0
        day_sales = sum(r.total for r in receipts) if receipts else 0
        day_tips = sum(r.tip for r in receipts) if receipts else 0

        card = tk.Frame(self._cards_frame, bg=card_bg,
                        highlightbackground=ACCENT if is_today else "#2D4A7A",
                        highlightthickness=2 if is_today else 1)
        card.grid(row=0, column=col, sticky="nsew", padx=3, pady=3)

        day_name = self.DAY_NAMES[dt.weekday()]
        day_num = dt.strftime("%d")

        # Day header
        tk.Label(card, text=day_name, bg=card_bg,
                 fg="#FFFFFF" if is_today else NAV_INACTIVE,
                 font=(FONT, 11, "bold")).pack(pady=(10, 0))
        tk.Label(card, text=day_num, bg=card_bg,
                 fg="#FFFFFF" if is_today else "#5A7BA5",
                 font=(FONT, 20, "bold")).pack(pady=(0, 6))

        sep = tk.Frame(card, bg="#2D4A7A", height=1)
        sep.pack(fill="x", padx=10, pady=(0, 6))

        if count > 0:
            # Charge type summary (e.g. "2 Room · 1 Hotel")
            type_counts = Counter(r.charge_type for r in receipts)
            type_parts = []
            for ct, n in type_counts.most_common():
                short = ct.replace(" Charge", "")  # "Room", "Hotel", "Voucher"
                type_parts.append(f"{n} {short}")
            tk.Label(card, text=" · ".join(type_parts),
                     bg=card_bg, fg=NAV_INACTIVE,
                     font=(FONT, 8)).pack(pady=(2, 0))

            tk.Label(card, text=f"${day_sales:,.2f}", bg=card_bg,
                     fg="#6EE7B7", font=(FONT, 14, "bold")).pack(pady=(2, 0))
            tk.Label(card, text="sales", bg=card_bg, fg=NAV_INACTIVE,
                     font=(FONT, 8)).pack()
            tk.Label(card, text=f"${day_tips:,.2f}", bg=card_bg,
                     fg="#7EB8FF", font=(FONT, 12, "bold")).pack(pady=(4, 0))
            tk.Label(card, text="tips", bg=card_bg, fg=NAV_INACTIVE,
                     font=(FONT, 8)).pack(pady=(0, 10))

            # Click to view day details
            key = dt.strftime("%Y%m%d")
            for widget in [card] + list(card.winfo_children()):
                widget.config(cursor="hand2")
                widget.bind("<Button-1>",
                            lambda e, d=key, dr=receipts: self._show_day(d, dr))

            # Hover
            def _hover_in(e, c=card, bg=card_bg):
                lighter = "#264A72" if bg == "#1E3A5F" else "#1E3358"
                c.config(bg=lighter)
                for ch in c.winfo_children():
                    try: ch.config(bg=lighter)
                    except Exception: pass

            def _hover_out(e, c=card, bg=card_bg):
                c.config(bg=bg)
                for ch in c.winfo_children():
                    try: ch.config(bg=bg)
                    except Exception: pass

            for widget in [card] + list(card.winfo_children()):
                widget.bind("<Enter>", _hover_in)
                widget.bind("<Leave>", _hover_out)
        else:
            tk.Label(card, text="—", bg=card_bg, fg="#3A5278",
                     font=(FONT, 14)).pack(expand=True)

        # Store reference
        if col < len(self._day_cards):
            self._day_cards[col] = {"frame": card, "dt": dt}
        else:
            self._day_cards.append({"frame": card, "dt": dt})

    # ── Week navigation helpers ──────────────────────────────────────────────
    def _update_week_label(self):
        """Update the week range text between the nav arrows."""
        sunday = self._current_monday + datetime.timedelta(days=6)
        start_str = self._current_monday.strftime("%b %d")
        end_str = sunday.strftime("%b %d, %Y")
        self._week_label.config(text=f"{start_str} — {end_str}")

    def _navigate_week(self, direction: int):
        """Move the calendar view by *direction* weeks (-1 = prev, +1 = next)."""
        if self._running:
            return
        self._current_monday += datetime.timedelta(weeks=direction)
        self._dates = [self._current_monday + datetime.timedelta(days=i)
                       for i in range(7)]
        self._update_week_label()

        # Rebuild cards for the new week using existing local data
        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
        export_path = Path(out_dir)
        if export_path.exists():
            try:
                all_receipts = find_room_charges(export_path)
                if all_receipts:
                    self._refresh_calendar(all_receipts)
                    return
            except Exception:
                pass
        # Fall back to Firebase, or show empty cards
        self._refresh_calendar([])
        self._load_from_firebase()

    # ── Refresh all day cards with new receipt data ───────────────────────────
    def _refresh_calendar(self, receipts: list[RoomChargeReceipt]):
        """Rebuild all 7 day cards with updated data."""
        self._last_receipts = receipts

        by_date: dict[str, list[RoomChargeReceipt]] = {}
        for r in receipts:
            by_date.setdefault(r.date_folder, []).append(r)

        # Destroy old cards
        for child in self._cards_frame.winfo_children():
            child.destroy()
        self._day_cards.clear()

        # Rebuild
        for col, dt in enumerate(self._dates):
            key = dt.strftime("%Y%m%d")
            day_receipts = by_date.get(key, [])
            self._build_day_card(col, dt, day_receipts if day_receipts else None)

    # ── Load existing data on startup ─────────────────────────────────────────
    def _load_existing_data(self):
        """Load calendar data — tries local files first, then Firebase."""
        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
        export_path = Path(out_dir)
        if export_path.exists():
            try:
                receipts = find_room_charges(export_path)
                if receipts:
                    self._refresh_calendar(receipts)
                    return
            except Exception:
                pass
        # Fall back to Firebase
        self._load_from_firebase()

    def _load_from_firebase(self):
        """Fetch orders from Firestore and populate the calendar."""
        if not AUTH_OK or not hasattr(self, "_id_token") or not self._id_token:
            return

        date_keys = [dt.strftime("%Y%m%d") for dt in self._dates]

        def _worker():
            try:
                orders = auth.fetch_orders(self._id_token, date_keys)
                receipts = []
                for o in orders:
                    items = []
                    for it in o.get("items", []):
                        items.append(ReceiptItem(
                            name=str(it.get("name", "")),
                            qty=float(it.get("qty", 0)),
                            net_price=float(it.get("net_price", 0)),
                            tax=float(it.get("tax", 0)),
                        ))
                    receipts.append(RoomChargeReceipt(
                        date_folder=str(o.get("date_folder", "")),
                        check_id=str(o.get("check_id", "")),
                        check_number=str(o.get("check_number", "")),
                        tab_name=str(o.get("tab_name", "")),
                        server=str(o.get("server", "")),
                        table=str(o.get("table", "")),
                        paid_date=str(o.get("paid_date", "")),
                        subtotal=float(o.get("subtotal", 0)),
                        tax_total=float(o.get("tax_total", 0)),
                        tip=float(o.get("tip", 0)),
                        total=float(o.get("total", 0)),
                        charge_type=str(o.get("charge_type", "Room Charge")),
                        items=items,
                    ))
                if receipts:
                    self.after(0, lambda: self._refresh_calendar(receipts))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    # ── Show day detail view ──────────────────────────────────────────────────
    def _show_day(self, date_key: str, receipts: list[RoomChargeReceipt]):
        self._main.pack_forget()
        self._detail = DayDetailView(
            self, date_key=date_key, receipts=receipts,
            on_back=self._close_day_detail)
        self._detail.pack(fill="both", expand=True)

    def _close_day_detail(self):
        if hasattr(self, "_detail") and self._detail.winfo_exists():
            self._detail.pack_forget()
            self._detail.destroy()
        self._main.pack(fill="both", expand=True)

    # ── Button hover ──────────────────────────────────────────────────────────
    def _start_hover_in(self, _):
        self._start_frame.config(bg=ACCENT_HV, highlightbackground=ACCENT_HV)
        self._start_lbl.config(bg=ACCENT_HV)

    def _start_hover_out(self, _):
        bg = START_ACTIVE if self._running else ACCENT
        self._start_frame.config(bg=bg, highlightbackground=bg)
        self._start_lbl.config(bg=bg)

    # ── First-run setup wizard ─────────────────────────────────────────────────
    def _check_first_run_setup(self):
        """If no SSH key is configured, prompt the admin to set one up."""
        key_path = self._settings.get("ssh_key", str(SFTP_DEFAULT_KEY))
        if Path(key_path).exists():
            return  # Already set up

        answer = messagebox.askyesno(
            "Welcome — First-Time Setup",
            "No SSH key is configured for fetching data from Toast.\n\n"
            "Would you like to select your SSH key file now?\n\n"
            "(You can also do this later via the \u2699 Settings icon.)",
            parent=self)
        if not answer:
            return

        key_file = filedialog.askopenfilename(
            title="Select your Toast SSH key file",
            filetypes=[("All files", "*"), ("PEM files", "*.pem"),
                       ("Key files", "*.key")],
            parent=self)
        if not key_file:
            return

        # Copy the key to the app data directory
        import shutil
        dest_dir = _default_data_dir() / "keys"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / Path(key_file).name
        try:
            shutil.copy2(key_file, dest_file)
            # Fix permissions on macOS/Linux
            if platform.system() != "Windows":
                dest_file.chmod(0o600)
        except Exception as exc:
            messagebox.showerror(
                "Setup Error",
                f"Could not copy key file:\n{exc}",
                parent=self)
            return

        # Save the path in settings
        self._settings["ssh_key"] = str(dest_file)
        _save_settings(self._settings)

        messagebox.showinfo(
            "Setup Complete",
            f"SSH key saved to:\n{dest_file}\n\n"
            "You're all set! Click 'Get Weekly Orders' to fetch data.",
            parent=self)

    # ── Start action ──────────────────────────────────────────────────────────
    def _on_start(self):
        if self._running:
            return

        key_path = self._settings.get("ssh_key", str(SFTP_DEFAULT_KEY))
        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))

        if not Path(key_path).exists():
            messagebox.showerror(
                "SSH Key Not Found",
                f"SSH key file not found:\n{key_path}\n\n"
                "Click the \u2699 gear icon to configure the correct path.",
                parent=self)
            return

        if not PARAMIKO_OK:
            messagebox.showerror(
                "paramiko not installed",
                "The 'paramiko' package is required.\n\n"
                "Install it with:  pip install paramiko",
                parent=self)
            return
        if not REPORTLAB_OK:
            messagebox.showerror(
                "reportlab not installed",
                "The 'reportlab' package is required.\n\n"
                "Install it with:  pip install reportlab",
                parent=self)
            return

        # Switch to running state
        self._running = True
        self._start_lbl.config(text="Fetching...")
        self._start_frame.config(bg=START_ACTIVE,
                                 highlightbackground=START_ACTIVE)
        self._start_lbl.config(bg=START_ACTIVE)
        self._pulse_step = 0
        self._pulse("Downloading last 7 days")

        # Show log
        self._log_frame.pack(fill="x", padx=0, pady=(4, 0))
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")

        # Always fetch the last 7 days (today + 6 days back)
        today = datetime.date.today()
        dates = [(today - datetime.timedelta(days=i)).strftime("%Y%m%d")
                 for i in range(6, -1, -1)]

        threading.Thread(
            target=self._full_pipeline_worker,
            args=(key_path, out_dir, dates),
            daemon=True,
        ).start()

    # ── Full pipeline: SFTP download → Generate receipts ─────────────────────
    def _full_pipeline_worker(self, key_path: str, out_dir: str,
                              dates: list[str]):
        downloaded = 0
        skipped = 0
        errors = 0

        self._log_safe("Connecting to SFTP...", "info")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            pkey = paramiko.RSAKey.from_private_key_file(key_path)
        except paramiko.ssh_exception.PasswordRequiredException:
            self._log_safe(
                "ERROR: Key file is passphrase-protected.", "err")
            self._finish_pipeline(0, 0)
            return
        except Exception as exc:
            self._log_safe(f"ERROR: Could not load SSH key: {exc}", "err")
            self._finish_pipeline(0, 0)
            return

        try:
            client.connect(
                hostname=SFTP_HOST, port=SFTP_PORT,
                username=SFTP_USERNAME, pkey=pkey,
                look_for_keys=False, allow_agent=False, timeout=30)
            sftp = client.open_sftp()
            self._log_safe("Connected to SFTP server!", "ok")
        except Exception as exc:
            self._log_safe(f"ERROR: Connection failed: {exc}", "err")
            self._finish_pipeline(0, 0)
            return

        try:
            out_path = Path(out_dir)
            out_path.mkdir(parents=True, exist_ok=True)

            for date_folder in dates:
                remote_dir = f"/{SFTP_EXPORT_ID}/{date_folder}"
                local_dir = out_path / date_folder
                local_dir.mkdir(parents=True, exist_ok=True)

                try:
                    sftp.stat(remote_dir)
                except FileNotFoundError:
                    self._log_safe(
                        f"[{date_folder}] Not available on server — skipping",
                        "info")
                    continue
                except Exception as exc:
                    self._log_safe(
                        f"[{date_folder}] Error: {exc}", "err")
                    errors += 1
                    continue

                for fname in SFTP_REQUIRED_FILES:
                    remote_path = f"{remote_dir}/{fname}"
                    local_path = local_dir / fname
                    try:
                        sftp.stat(remote_path)
                    except FileNotFoundError:
                        skipped += 1
                        continue
                    except Exception:
                        errors += 1
                        continue
                    try:
                        sftp.get(remote_path, str(local_path))
                        self._log_safe(
                            f"[{date_folder}] {fname}  \u2713", "ok")
                        downloaded += 1
                    except Exception as exc:
                        self._log_safe(
                            f"[{date_folder}] Failed: {fname} — {exc}", "err")
                        errors += 1

            sftp.close()
        except Exception as exc:
            self._log_safe(f"ERROR: {exc}", "err")
            errors += 1
        finally:
            client.close()

        self._log_safe(
            f"\nDownload complete: {downloaded} files downloaded.", "bold")

        # ── Phase 2: Generate receipts ────────────────────────────────────
        self._update_status("Generating receipts")
        self._log_safe("\nScanning for charges...", "info")

        export_path = Path(out_dir)
        receipt_count, receipts_dir = generate_all_receipts(
            export_path, log_fn=self._log_safe)

        all_receipts = find_room_charges(export_path)

        # ── Phase 3: Upload orders to Firebase ─────────────────────────────
        if AUTH_OK and all_receipts and hasattr(self, "_id_token") and self._id_token:
            self._update_status("Uploading to Firebase")
            self._log_safe("\nUploading orders to Firebase...", "info")
            order_dicts = []
            for r in all_receipts:
                order_dicts.append({
                    "date_folder": r.date_folder,
                    "check_id": r.check_id,
                    "check_number": r.check_number,
                    "tab_name": r.tab_name,
                    "server": r.server,
                    "table": r.table,
                    "paid_date": r.paid_date,
                    "subtotal": r.subtotal,
                    "tax_total": r.tax_total,
                    "tip": r.tip,
                    "total": r.total,
                    "charge_type": r.charge_type,
                    "items": [
                        {"name": it.name, "qty": it.qty,
                         "net_price": it.net_price, "tax": it.tax}
                        for it in r.items
                    ],
                })
            try:
                ok, errs = auth.upload_orders_batch(
                    self._id_token, order_dicts, log_fn=self._log_safe)
                self._log_safe(
                    f"\nFirebase: {ok} uploaded, {errs} error(s).", "bold")
            except Exception as exc:
                self._log_safe(f"\nFirebase upload failed: {exc}", "err")
        elif AUTH_OK and all_receipts:
            self._log_safe("\nFirebase upload skipped — not logged in.", "warn")

        self._finish_pipeline(downloaded, receipt_count, all_receipts)

    def _finish_pipeline(self, files_downloaded: int, receipts_generated: int,
                         all_receipts: list[RoomChargeReceipt] | None = None):
        def _done():
            self._running = False
            if self._pulse_id:
                self.after_cancel(self._pulse_id)
                self._pulse_id = None

            self._start_lbl.config(text="Get Weekly Orders")
            self._start_frame.config(bg=ACCENT, highlightbackground=ACCENT)
            self._start_lbl.config(bg=ACCENT)

            if files_downloaded == 0 and receipts_generated == 0:
                self._status_lbl.config(
                    text="No new data found.", fg="#FCD34D")
            else:
                parts = []
                if files_downloaded:
                    parts.append(f"{files_downloaded} files")
                if receipts_generated:
                    parts.append(
                        f"{receipts_generated} new receipt"
                        f"{'s' if receipts_generated != 1 else ''}")
                self._status_lbl.config(
                    text="\u2713  " + "  |  ".join(parts),
                    fg="#6EE7B7")
                Toast(self, "All done!")

            # Refresh the calendar cards with new data
            if all_receipts:
                self._refresh_calendar(all_receipts)

            # Hide log after a delay
            self.after(3000, self._hide_log)

        self.after(0, _done)

    def _hide_log(self):
        """Slide the log area away."""
        if not self._running:
            self._log_frame.pack_forget()

    # ── Logging helpers ──────────────────────────────────────────────────────
    def _log_msg(self, text: str, tag: str = "info"):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n", tag)
        self._log.see("end")
        self._log.config(state="disabled")

    def _log_safe(self, text: str, tag: str = "info"):
        self.after(0, self._log_msg, text, tag)

    # ── Status pulse ─────────────────────────────────────────────────────────
    def _pulse(self, base_msg: str):
        if not self._running:
            self._status_lbl.config(text="")
            return
        dots = "." * (self._pulse_step % 4)
        self._status_lbl.config(text=f"{base_msg}{dots}", fg=NAV_INACTIVE)
        self._pulse_step += 1
        self._pulse_id = self.after(500, self._pulse, base_msg)

    def _update_status(self, msg: str):
        def _do():
            self._pulse_step = 0
            if self._pulse_id:
                self.after_cancel(self._pulse_id)
            self._pulse(msg)
        self.after(0, _do)

    # ── Export Month PDF (this or last) ──────────────────────────────────────
    def _export_month_pdf(self, which: str = "this"):
        """
        Generate a combined PDF for this month or last month.
        Tries local files first, then Firebase data.
        which: "this" for current month, "last" for previous month.
        """
        if self._running:
            return

        if not REPORTLAB_OK:
            messagebox.showerror("reportlab not installed",
                                 "The 'reportlab' package is required.\n\n"
                                 "Install it with:  pip install reportlab",
                                 parent=self)
            return

        # Determine the date range
        today = datetime.date.today()
        if which == "last":
            first_of_this_month = today.replace(day=1)
            last_day = first_of_this_month - datetime.timedelta(days=1)
            first_day = last_day.replace(day=1)
        else:
            first_day = today.replace(day=1)
            last_day = today  # up to today

        month_label = first_day.strftime("%m-%Y")    # e.g. "03-2026"
        month_display = first_day.strftime("%B %Y")  # e.g. "March 2026"

        # ── Try local files first ────────────────────────────────────────
        month_receipts = []
        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
        export_path = Path(out_dir)

        # Check for existing generated PDF
        receipts_dir = export_path / "charge_receipts"
        existing_pdf = receipts_dir / f"{month_label}.pdf"
        if which == "last" and existing_pdf.exists():
            save_path = filedialog.asksaveasfilename(
                title=f"Export {month_display} PDF",
                initialfile=f"{month_label}.pdf",
                defaultextension=".pdf",
                filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
                parent=self)
            if save_path:
                import shutil
                try:
                    shutil.copy2(str(existing_pdf), save_path)
                    Toast(self, f"Exported {month_display} PDF!")
                except Exception as exc:
                    messagebox.showerror("Export Error",
                                         f"Failed to export PDF:\n{exc}",
                                         parent=self)
            return

        # Try local CSV data
        if export_path.exists():
            try:
                all_receipts = find_room_charges(export_path)
                for r in all_receipts:
                    try:
                        dt = datetime.datetime.strptime(r.date_folder, "%Y%m%d").date()
                        if first_day <= dt <= last_day:
                            month_receipts.append(r)
                    except ValueError:
                        continue
            except Exception:
                pass

        # ── Fall back to Firebase if no local data ───────────────────────
        if not month_receipts and AUTH_OK and hasattr(self, "_id_token") and self._id_token:
            try:
                # Generate all date keys for the month range
                date_keys = []
                d = first_day
                while d <= last_day:
                    date_keys.append(d.strftime("%Y%m%d"))
                    d += datetime.timedelta(days=1)

                orders = auth.fetch_orders(self._id_token, date_keys)
                for o in orders:
                    items = []
                    for it in o.get("items", []):
                        items.append(ReceiptItem(
                            name=str(it.get("name", "")),
                            qty=float(it.get("qty", 0)),
                            net_price=float(it.get("net_price", 0)),
                            tax=float(it.get("tax", 0)),
                        ))
                    month_receipts.append(RoomChargeReceipt(
                        date_folder=str(o.get("date_folder", "")),
                        check_id=str(o.get("check_id", "")),
                        check_number=str(o.get("check_number", "")),
                        tab_name=str(o.get("tab_name", "")),
                        server=str(o.get("server", "")),
                        table=str(o.get("table", "")),
                        paid_date=str(o.get("paid_date", "")),
                        subtotal=float(o.get("subtotal", 0)),
                        tax_total=float(o.get("tax_total", 0)),
                        tip=float(o.get("tip", 0)),
                        total=float(o.get("total", 0)),
                        charge_type=str(o.get("charge_type", "Room Charge")),
                        items=items,
                    ))
            except Exception:
                pass

        if not month_receipts:
            messagebox.showinfo("No Data",
                                f"No charge receipts found for {month_display}.",
                                parent=self)
            return

        # Ask user where to save
        save_path = filedialog.asksaveasfilename(
            title=f"Export {month_display} PDF",
            initialfile=f"{month_label}.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            parent=self)
        if not save_path:
            return

        try:
            generate_combined_pdf(month_receipts, Path(save_path))
            Toast(self, f"Exported {month_display} — "
                        f"{len(month_receipts)} receipts!")
        except Exception as exc:
            messagebox.showerror("Export Error",
                                 f"Failed to generate PDF:\n{exc}",
                                 parent=self)

    # ── Check for updates ───────────────────────────────────────────────────────
    def _check_for_updates(self):
        """Use the updater package to check GitHub for new .app / .exe releases."""
        if self._running:
            return

        if UPDATER_OK and _app_updater:
            def _worker():
                try:
                    result = _app_updater.check_for_updates()
                except Exception as exc:
                    self.after(0, lambda: messagebox.showinfo(
                        "Update Check",
                        f"Could not check for updates.\n\n{exc}",
                        parent=self))
                    return

                if result.get("update_available"):
                    self.after(0, lambda: self._show_update_dialog(result))
                else:
                    self.after(0, lambda: messagebox.showinfo(
                        "Up to Date",
                        f"You are running the latest version ({APP_VERSION}).",
                        parent=self))

            threading.Thread(target=_worker, daemon=True).start()
        else:
            messagebox.showinfo(
                "Update Check",
                "Updater module not available.",
                parent=self)

    def _show_update_dialog(self, result):
        """Show the update dialog from the updater package."""
        try:
            from updater.update_dialog import show_update_dialog
            show_update_dialog(self, _app_updater, result)
        except Exception as exc:
            messagebox.showerror("Update Error", str(exc), parent=self)

    # ── Sign out ───────────────────────────────────────────────────────────────
    def _sign_out(self):
        if self._running:
            return
        if AUTH_OK:
            auth.clear_session()
        self.destroy()
        # Re-launch login screen
        if AUTH_OK:
            require_login(_launch_app)
        else:
            _launch_app("offline@local", "Offline", "admin", "")

    # ── Settings dialog ──────────────────────────────────────────────────────
    def _open_settings(self):
        SettingsDialog(self, self._settings)




class SettingsDialog(tk.Toplevel):
    """Modal settings window for SSH key and output folder."""

    def __init__(self, parent: App, settings: dict):
        super().__init__(parent)
        self._parent = parent
        self._settings = settings
        self.title("Settings")
        self.configure(bg=BG_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Center on parent
        self.update_idletasks()
        w, h = 540, 340
        px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{px}+{py}")

        body = tk.Frame(self, bg=BG_PAGE)
        body.pack(fill="both", expand=True, padx=24, pady=20)

        tk.Label(body, text="Settings", bg=BG_PAGE, fg=FG,
                 font=(FONT, 18, "bold")).pack(anchor="w", pady=(0, 16))

        # ── SSH Key ──────────────────────────────────────────────────────
        card1 = Card(body)
        card1.pack(fill="x", pady=(0, 12))
        tk.Label(card1, text="SSH Private Key", bg=BG_CARD, fg=FG,
                 font=(FONT, 12, "bold")).pack(anchor="w")
        tk.Label(card1, text="RSA key file for Toast SFTP authentication",
                 bg=BG_CARD, fg=FG_SEC, font=(FONT, 10)).pack(anchor="w",
                                                                pady=(0, 6))
        row1 = tk.Frame(card1, bg=BG_CARD)
        row1.pack(fill="x")
        self._key_inp = Inp(row1)
        self._key_inp.pack(side="left", fill="x", expand=True, ipady=4)
        self._key_inp.insert(
            0, settings.get("ssh_key", str(SFTP_DEFAULT_KEY)))
        Btn(row1, text="Browse", style="ghost",
            command=self._browse_key).pack(side="left", padx=(8, 0))

        # ── Output Folder ────────────────────────────────────────────────
        card2 = Card(body)
        card2.pack(fill="x", pady=(0, 16))
        tk.Label(card2, text="Output Folder", bg=BG_CARD, fg=FG,
                 font=(FONT, 12, "bold")).pack(anchor="w")
        tk.Label(card2, text="Where downloaded files and receipts are saved",
                 bg=BG_CARD, fg=FG_SEC, font=(FONT, 10)).pack(anchor="w",
                                                                pady=(0, 6))
        row2 = tk.Frame(card2, bg=BG_CARD)
        row2.pack(fill="x")
        self._out_inp = Inp(row2)
        self._out_inp.pack(side="left", fill="x", expand=True, ipady=4)
        self._out_inp.insert(
            0, settings.get("output_folder", str(SFTP_DEFAULT_OUT)))
        Btn(row2, text="Browse", style="ghost",
            command=self._browse_out).pack(side="left", padx=(8, 0))

        # ── Buttons ──────────────────────────────────────────────────────
        btn_row = tk.Frame(body, bg=BG_PAGE)
        btn_row.pack(fill="x")
        Btn(btn_row, text="Save", style="primary",
            command=self._save).pack(side="right")
        Btn(btn_row, text="Cancel", style="ghost",
            command=self.destroy).pack(side="right", padx=(0, 8))

    def _browse_key(self):
        p = filedialog.askopenfilename(
            title="Select SSH Private Key",
            initialdir=str(Path.home() / "Downloads"),
            filetypes=[("All files", "*.*"), ("PEM files", "*.pem")])
        if p:
            self._key_inp.delete(0, "end")
            self._key_inp.insert(0, p)

    def _browse_out(self):
        p = filedialog.askdirectory(
            title="Select Output Folder",
            initialdir=str(SFTP_DEFAULT_OUT.parent))
        if p:
            self._out_inp.delete(0, "end")
            self._out_inp.insert(0, p)

    def _save(self):
        self._settings["ssh_key"] = self._key_inp.get().strip()
        self._settings["output_folder"] = self._out_inp.get().strip()
        _save_settings(self._settings)
        self._parent._settings = self._settings
        Toast(self._parent, "Settings saved!")
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def _launch_app(email, display_name, role, uid, id_token=""):
    """Called after successful login — launches the main application."""
    app = App(user_role=role, user_email=email,
              user_display_name=display_name, user_uid=uid,
              id_token=id_token)
    app.mainloop()


def main():
    if AUTH_OK:
        require_login(_launch_app)
    else:
        # Fallback: skip login if auth module not available
        print("[WARNING] auth_manager not found — skipping login.")
        _launch_app("offline@local", "Offline", "admin", "")


if __name__ == "__main__":
    main()
