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
import subprocess
import sys
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# ── Windows DPI awareness (must be set BEFORE importing tkinter) ─────────
if platform.system() == "Windows":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()     # Fallback for Win 8.0
        except Exception:
            pass

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

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
def _read_version() -> str:
    """Read version from version.txt, works both in dev and PyInstaller bundle."""
    import sys
    from pathlib import Path
    # PyInstaller sets sys._MEIPASS to the temp extraction folder
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    vf = base / "version.txt"
    if vf.exists():
        try:
            return vf.read_text().strip()
        except Exception:
            pass
    return "0.0.0"

APP_VERSION = _read_version()
GITHUB_USERNAME = "pipilas"
GITHUB_REPO = "anemi-room-charge"

# ── Updater (auto-download .app / .exe from GitHub Releases) ────────────
try:
    from updater import Updater as _Updater
    _app_updater = _Updater(
        current_version=APP_VERSION,
        github_username=GITHUB_USERNAME,
        github_repo=GITHUB_REPO,
        app_name="Room Charge & Sales",
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


# ═══════════════════════════════════════════════════════════════════════════════
#  DAILY SALES SUMMARY — Computed from PaymentDetails + CheckDetails CSVs
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DaypartSales:
    """Sales figures for a single daypart (Breakfast, Dinner, etc.)."""
    name: str
    orders: int = 0
    gross_sales: float = 0.0
    net_sales: float = 0.0
    discount: float = 0.0
    tax: float = 0.0
    tips: float = 0.0      # includes gratuity (auto-grat)
    admin_fee: float = 0.0  # V/MC/D processing fees
    refund: float = 0.0


@dataclass
class DailySalesSummary:
    """Complete daily sales breakdown with daypart detail."""
    date_folder: str
    dayparts: list[DaypartSales] = field(default_factory=list)
    void_amount: float = 0.0
    void_count: int = 0
    total_orders: int = 0
    total_guests: int = 0

    @property
    def total_gross(self) -> float:
        return sum(d.gross_sales for d in self.dayparts)

    @property
    def total_net(self) -> float:
        return sum(d.net_sales for d in self.dayparts)

    @property
    def total_discount(self) -> float:
        return sum(d.discount for d in self.dayparts)

    @property
    def total_tax(self) -> float:
        return sum(d.tax for d in self.dayparts)

    @property
    def total_tips(self) -> float:
        return sum(d.tips for d in self.dayparts)

    @property
    def total_admin_fee(self) -> float:
        return sum(d.admin_fee for d in self.dayparts)

    @property
    def total_revenue(self) -> float:
        """Total = Net + Tax + Tips (matches Toast Revenue Summary)."""
        return self.total_net + self.total_tax + self.total_tips


def _safe_float(val) -> float:
    """Convert a value to float, returning 0.0 on failure."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def compute_daily_sales(export_dir: Path, date_folder: str) -> DailySalesSummary | None:
    """
    Compute a daily sales summary from PaymentDetails.csv + CheckDetails.csv.

    Uses PaymentDetails for daypart (Service column) and amounts, and
    CheckDetails for per-check Tax and Discount. Excludes VOIDED payments.
    """
    day_dir = export_dir / date_folder
    pay_file = day_dir / "PaymentDetails.csv"
    check_file = day_dir / "CheckDetails.csv"

    if not pay_file.exists() or not check_file.exists():
        return None

    # ── 1. Build tax & discount lookup from CheckDetails ───────────────
    check_tax: dict[str, float] = {}
    check_disc: dict[str, float] = {}
    check_guests: dict[str, int] = {}
    with open(check_file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            chk = (row.get("Check #") or "").strip()
            if chk:
                check_tax[chk] = _safe_float(row.get("Tax"))
                check_disc[chk] = _safe_float(row.get("Discount"))
                ts = (row.get("Table Size") or "").strip()
                if ts:
                    check_guests[chk] = int(_safe_float(ts))

    # ── 2. Aggregate by daypart from PaymentDetails ────────────────────
    daypart_data: dict[str, dict] = {}
    seen_checks_per_daypart: dict[str, set] = {}
    void_amount = 0.0
    void_count = 0
    all_checks: set[str] = set()

    with open(pay_file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            status = (row.get("Status") or "").strip()
            chk = (row.get("Check #") or "").strip()

            # Count voids
            if status == "VOIDED":
                void_amount += _safe_float(row.get("Amount"))
                void_count += 1
                continue

            # Only count CAPTURED payments (skip DENIED, etc.)
            if status != "CAPTURED":
                continue

            svc = (row.get("Service") or "").strip() or "Other"
            # Combine Late Night into Dinner
            if svc == "Late Night":
                svc = "Dinner"
            amt = _safe_float(row.get("Amount"))
            tip = _safe_float(row.get("Tip"))
            grat = _safe_float(row.get("Gratuity"))
            fee = _safe_float(row.get("V/MC/D Fees"))
            # Subtract any refund from the amount
            refund_amt = _safe_float(row.get("Refund Amount"))
            amt -= refund_amt

            if svc not in daypart_data:
                daypart_data[svc] = {"amt": 0.0, "tip": 0.0,
                                     "tax": 0.0, "disc": 0.0, "fee": 0.0}
                seen_checks_per_daypart[svc] = set()

            daypart_data[svc]["amt"] += amt
            daypart_data[svc]["tip"] += tip + grat  # gratuity counts as tips
            daypart_data[svc]["fee"] += fee

            # Only count tax/discount once per check per daypart
            if chk and chk not in seen_checks_per_daypart[svc]:
                seen_checks_per_daypart[svc].add(chk)
                daypart_data[svc]["tax"] += check_tax.get(chk, 0.0)
                daypart_data[svc]["disc"] += check_disc.get(chk, 0.0)
                all_checks.add(chk)

    # ── 3. Build DaypartSales objects ──────────────────────────────────
    # Custom sort order: Breakfast, Brunch, Lunch, Dinner, then others
    order_map = {"Breakfast": 0, "Brunch": 1, "Lunch": 2, "Dinner": 3}
    dayparts = []
    for svc in sorted(daypart_data, key=lambda s: (order_map.get(s, 99), s)):
        d = daypart_data[svc]
        net = d["amt"] - d["tax"]
        gross = net + d["disc"]
        n_orders = len(seen_checks_per_daypart.get(svc, set()))
        dayparts.append(DaypartSales(
            name=svc,
            orders=n_orders,
            gross_sales=round(gross, 2),
            net_sales=round(net, 2),
            discount=round(d["disc"], 2),
            tax=round(d["tax"], 2),
            tips=round(d["tip"], 2),
            admin_fee=round(d["fee"], 2),
        ))

    total_guests = sum(check_guests.get(c, 0) for c in all_checks)

    return DailySalesSummary(
        date_folder=date_folder,
        dayparts=dayparts,
        void_amount=round(void_amount, 2),
        void_count=void_count,
        total_orders=len(all_checks),
        total_guests=total_guests,
    )


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
    c.drawString(left, y, "Room Charge & Sales")
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
    c.drawString(left, y, "Room Charge & Sales")
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
        c.drawString(left, y, "Room Charge & Sales")
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
#  SFTP CONFIGURATION — Room Charge & Sales Data Exports
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

# ── Platform detection ─────────────────────────────────────────────────────────
IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"
FONT = ("Helvetica Neue" if IS_MAC
        else ("Ubuntu" if platform.system() == "Linux" else "Segoe UI"))
# Windows renders tkinter fonts ~1pt larger than macOS, so we compensate
_SZ = (lambda s: s - 1) if IS_WIN else (lambda s: s)

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
        # On Windows, use "solid" relief + bd=1 for a crisper look
        relief = "solid" if IS_WIN else "flat"
        bd = 1 if IS_WIN else 0
        ht = 0 if IS_WIN else 1
        super().__init__(parent, bg=BG_INPUT, fg=FG, insertbackground=FG,
                         relief=relief, font=(FONT, _SZ(12)), bd=bd,
                         highlightthickness=ht, highlightbackground=BORDER,
                         highlightcolor=BORDER_FOCUS, **kw)
        if IS_WIN:
            # Add internal padding so text isn't jammed against the border
            self.config(borderwidth=1)


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


def _configure_ttk_styles():
    """Apply platform-aware ttk styles — called once at startup."""
    style = ttk.Style()
    if IS_WIN:
        try:
            style.theme_use("vista")  # Best-looking Windows theme
        except Exception:
            pass
    style.configure("TScrollbar", troughcolor="#1B2A4A", background="#3A5278",
                    bordercolor="#1B2A4A", arrowcolor="#94A3B8")


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
#  DAY SALES VIEW — Daypart breakdown, discounts, voids
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_void_user(raw: str) -> str:
    """Extract a readable name from Toast's Void User field.
    Input:  'RestaurantUser [id=..., user email = foo@bar.com]'
    Output: 'foo@bar.com'
    """
    import re
    m = re.search(r"user email\s*=\s*([^\]]+)", raw)
    if m:
        email = m.group(1).strip()
        # If it's a UUID-style placeholder, return just "Staff"
        if "@example.com" in email:
            return "Staff"
        return email
    return raw[:40] if raw else ""


class DaySalesView(tk.Frame):
    """Full-screen daily sales breakdown view (daypart table + voids/discounts)."""

    _DAYPART_COLORS = {
        "Breakfast": "#F59E0B",
        "Brunch":    "#FB923C",
        "Lunch":     "#38BDF8",
        "Dinner":    "#818CF8",
        "Other":     "#94A3B8",
    }

    def __init__(self, parent, summary: DailySalesSummary,
                 receipts: list[RoomChargeReceipt] | None = None,
                 on_back=None, **kw):
        super().__init__(parent, bg=BG_NAV, **kw)
        self._on_back = on_back
        self._summary = summary

        try:
            dt = datetime.datetime.strptime(summary.date_folder, "%Y%m%d")
            title = dt.strftime("%A, %B %d, %Y")
        except ValueError:
            title = summary.date_folder

        # ── Top bar ──────────────────────────────────────────────────────
        top = tk.Frame(self, bg=BG_NAV)
        top.pack(fill="x", padx=24, pady=(16, 0))

        back_lbl = tk.Label(
            top, text="\u2190  Back", bg=BG_NAV, fg=NAV_INACTIVE,
            font=(FONT, 12), cursor="hand2")
        back_lbl.pack(side="left")
        back_lbl.bind("<Enter>", lambda e: back_lbl.config(fg="#FFFFFF"))
        back_lbl.bind("<Leave>", lambda e: back_lbl.config(fg=NAV_INACTIVE))
        back_lbl.bind("<Button-1>", lambda e: self._go_back())

        tk.Label(top, text=title, bg=BG_NAV, fg="#FFFFFF",
                 font=(FONT, 18, "bold")).pack(side="left", padx=(16, 0))

        tk.Label(top, text="Daily Sales Summary", bg=BG_NAV, fg=NAV_DATE,
                 font=(FONT, 12)).pack(side="right")

        # ── Compute Manager Comp total from CheckDetails ──────────────────
        mgr_comp_total = 0.0
        try:
            _out = Path(self.winfo_toplevel()._settings.get(
                "output_folder", str(SFTP_DEFAULT_OUT)))
            _chk = _out / summary.date_folder / "CheckDetails.csv"
            if _chk.exists():
                with open(_chk, newline="", encoding="utf-8-sig") as _f:
                    for _row in csv.DictReader(_f):
                        _reason = (_row.get("Reason of Discount") or "").strip()
                        if "Manager Comp" in _reason:
                            mgr_comp_total += _safe_float(_row.get("Discount"))
        except Exception:
            pass

        # ── Grand totals bar ─────────────────────────────────────────────
        totals_bar = tk.Frame(self, bg="#0F1D36")
        totals_bar.pack(fill="x", padx=24, pady=(12, 0))

        total_sales = summary.total_net + summary.total_tax
        stats = [
            ("Total Sales",   f"${total_sales:,.2f}",          "#6EE7B7"),
            ("Net Sales",     f"${summary.total_net:,.2f}",    "#38BDF8"),
            ("Tax",           f"${summary.total_tax:,.2f}",    "#FFD580"),
            ("Manager Comp",  f"${mgr_comp_total:,.2f}",      "#F87171"),
        ]
        for label, value, color in stats:
            cell = tk.Frame(totals_bar, bg="#0F1D36")
            cell.pack(side="left", expand=True, fill="x", padx=8, pady=10)
            tk.Label(cell, text=label, bg="#0F1D36", fg=NAV_INACTIVE,
                     font=(FONT, 9)).pack()
            tk.Label(cell, text=value, bg="#0F1D36", fg=color,
                     font=(FONT, 16, "bold")).pack()

        # ── Info row: orders, guests, voids badge ────────────────────────
        info_bar = tk.Frame(self, bg=BG_NAV)
        info_bar.pack(fill="x", padx=24, pady=(10, 0))

        info_text = f"{summary.total_orders} orders"
        if summary.total_guests > 0:
            info_text += f"  \u00B7  {summary.total_guests} guests"
        tk.Label(info_bar, text=info_text, bg=BG_NAV, fg=NAV_INACTIVE,
                 font=(FONT, 11)).pack(side="left")

        if summary.void_count > 0:
            void_badge = tk.Label(
                info_bar,
                text=f"  {summary.void_count} void{'s' if summary.void_count != 1 else ''}  \u00B7  ${summary.void_amount:,.2f}  ",
                bg="#3B1111", fg="#FCA5A5",
                font=(FONT, 10, "bold"), padx=6, pady=2)
            void_badge.pack(side="right")
        else:
            tk.Label(info_bar, text="No voids", bg=BG_NAV, fg="#6EE7B7",
                     font=(FONT, 11)).pack(side="right")

        # ── Scrollable content area ──────────────────────────────────────
        content_frame = tk.Frame(self, bg=BG_NAV)
        content_frame.pack(fill="both", expand=True, padx=0, pady=(8, 0))

        canvas = tk.Canvas(content_frame, bg=BG_NAV, highlightthickness=0)
        scrollbar = ttk.Scrollbar(content_frame, orient="vertical",
                                  command=canvas.yview)
        scroll_inner = tk.Frame(canvas, bg=BG_NAV)

        scroll_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        cw_id = canvas.create_window((0, 0), window=scroll_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            if IS_MAC:
                canvas.yview_scroll(int(-1 * event.delta), "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
        def _on_canvas_resize(e):
            canvas.itemconfig(cw_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        def _cleanup(e):
            try:
                canvas.unbind_all("<MouseWheel>")
                canvas.unbind_all("<Button-4>")
                canvas.unbind_all("<Button-5>")
            except Exception:
                pass
        self.bind("<Destroy>", _cleanup)

        inner = scroll_inner  # shorthand

        # ── Daypart breakdown table ──────────────────────────────────────
        self._build_daypart_table(inner, summary)

        # ── Discounts section ────────────────────────────────────────────
        self._build_discounts_section(inner, summary)

        # ── Voids section ────────────────────────────────────────────────
        self._build_voids_section(inner, summary)

        # Padding
        tk.Frame(inner, bg=BG_NAV, height=20).pack(fill="x")

    # ── Daypart table ────────────────────────────────────────────────────
    def _build_daypart_table(self, parent, summary: DailySalesSummary):
        frame = tk.Frame(parent, bg=BG_NAV)
        frame.pack(fill="x", padx=24, pady=(8, 0))

        tk.Label(frame, text="Sales by Service Period", bg=BG_NAV,
                 fg="#FFFFFF", font=(FONT, 13, "bold")).pack(anchor="w",
                                                             pady=(0, 8))

        hdr_bg = "#0F1D36"
        hdr = tk.Frame(frame, bg=hdr_bg)
        hdr.pack(fill="x")
        for col in ["Service", "Orders", "Total Sales", "Net",
                     "Discount", "Tax"]:
            tk.Label(hdr, text=col, bg=hdr_bg, fg=NAV_INACTIVE,
                     font=(FONT, 10, "bold"), padx=8,
                     pady=8).pack(side="left", expand=True, fill="x")

        tk.Frame(frame, bg="#2D4A7A", height=1).pack(fill="x")

        for idx, dp in enumerate(summary.dayparts):
            row_bg = "#162240" if idx % 2 == 0 else "#1A2B4D"
            self._render_daypart_row(frame, dp, row_bg)

        tk.Frame(frame, bg="#2D4A7A", height=2).pack(fill="x")

        # Total row
        tot = tk.Frame(frame, bg="#0F1D36")
        tot.pack(fill="x")
        wk_total_sales = summary.total_net + summary.total_tax
        for val in ["Total", str(summary.total_orders),
                     f"${wk_total_sales:,.2f}",
                     f"${summary.total_net:,.2f}",
                     f"${summary.total_discount:,.2f}",
                     f"${summary.total_tax:,.2f}"]:
            tk.Label(tot, text=val, bg="#0F1D36", fg="#FFFFFF",
                     font=(FONT, 11, "bold"), padx=8,
                     pady=10).pack(side="left", expand=True, fill="x")

    def _render_daypart_row(self, parent, dp: DaypartSales, bg: str):
        row = tk.Frame(parent, bg=bg)
        row.pack(fill="x")

        # Name with dot
        nf = tk.Frame(row, bg=bg)
        nf.pack(side="left", expand=True, fill="x")
        dot_color = self._DAYPART_COLORS.get(dp.name, "#94A3B8")
        tk.Label(nf, text="\u25CF", bg=bg, fg=dot_color,
                 font=(FONT, 8), padx=4).pack(side="left")
        tk.Label(nf, text=dp.name, bg=bg, fg="#FFFFFF",
                 font=(FONT, 11), padx=4, pady=8).pack(side="left")

        total_sales = dp.net_sales + dp.tax
        vals = [str(dp.orders), f"${total_sales:,.2f}",
                f"${dp.net_sales:,.2f}",
                f"${dp.discount:,.2f}" if dp.discount > 0 else "\u2014",
                f"${dp.tax:,.2f}"]
        for v in vals:
            fg = "#3A5278" if v == "\u2014" else "#B0C4DE"
            tk.Label(row, text=v, bg=bg, fg=fg, font=(FONT, 11),
                     padx=8, pady=8).pack(side="left", expand=True, fill="x")

    # ── Discounts section ────────────────────────────────────────────────
    def _build_discounts_section(self, parent, summary: DailySalesSummary):
        try:
            out_dir = Path(self.winfo_toplevel()._settings.get(
                "output_folder", str(SFTP_DEFAULT_OUT)))
            check_file = out_dir / summary.date_folder / "CheckDetails.csv"
            if not check_file.exists():
                return
        except Exception:
            return

        disc_rows = []
        with open(check_file, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                disc = _safe_float(row.get("Discount"))
                if disc > 0:
                    disc_rows.append(row)

        if not disc_rows:
            return

        # Separate Manager Comp from other discounts
        comp_rows = []
        other_rows = []
        for row in disc_rows:
            reason = (row.get("Reason of Discount") or "").strip()
            if "Manager Comp" in reason:
                comp_rows.append(row)
            else:
                other_rows.append(row)

        frame = tk.Frame(parent, bg=BG_NAV)
        frame.pack(fill="x", padx=24, pady=(20, 0))

        # ── Manager Comp sub-section ──
        if comp_rows:
            comp_total = sum(_safe_float(r.get("Discount")) for r in comp_rows)
            hdr_frame = tk.Frame(frame, bg=BG_NAV)
            hdr_frame.pack(fill="x")
            tk.Label(hdr_frame, text=f"Manager Comp ({len(comp_rows)})",
                     bg=BG_NAV, fg="#F87171",
                     font=(FONT, 13, "bold")).pack(side="left")
            tk.Label(hdr_frame, text=f"${comp_total:,.2f}", bg=BG_NAV,
                     fg="#F87171", font=(FONT, 13, "bold")).pack(side="right")
            tk.Frame(frame, bg="#2D4A7A", height=1).pack(fill="x", pady=(4, 8))

            for row in comp_rows:
                self._render_discount_card(frame, row, is_comp=True)

        # ── Other discounts sub-section ──
        if other_rows:
            other_total = sum(_safe_float(r.get("Discount")) for r in other_rows)
            hdr_frame2 = tk.Frame(frame, bg=BG_NAV)
            hdr_frame2.pack(fill="x", pady=(12 if comp_rows else 0, 0))
            tk.Label(hdr_frame2, text=f"Other Discounts ({len(other_rows)})",
                     bg=BG_NAV, fg="#FCD34D",
                     font=(FONT, 13, "bold")).pack(side="left")
            tk.Label(hdr_frame2, text=f"${other_total:,.2f}", bg=BG_NAV,
                     fg="#FCD34D", font=(FONT, 13, "bold")).pack(side="right")
            tk.Frame(frame, bg="#2D4A7A", height=1).pack(fill="x", pady=(4, 8))

            for row in other_rows:
                self._render_discount_card(frame, row, is_comp=False)

    def _render_discount_card(self, parent, row: dict, is_comp: bool = False):
        """Render a single discount card row."""
        disc = _safe_float(row.get("Discount"))
        reason = (row.get("Reason of Discount") or "").strip()
        chk = (row.get("Check #") or "").strip()
        server = (row.get("Server") or "").strip()
        time_str = (row.get("Opened Time") or "").strip()

        card_bg = "#2A1525" if is_comp else "#1E3358"
        border_color = "#5B2D3D" if is_comp else "#2D4A7A"
        amount_color = "#F87171" if is_comp else "#FCD34D"

        card = tk.Frame(parent, bg=card_bg,
                        highlightbackground=border_color,
                        highlightthickness=1)
        card.pack(fill="x", pady=3)

        top_row = tk.Frame(card, bg=card_bg)
        top_row.pack(fill="x", padx=12, pady=(8, 0))

        tk.Label(top_row, text=f"Check #{chk}", bg=card_bg,
                 fg="#FFFFFF", font=(FONT, 11, "bold")).pack(side="left")

        if time_str:
            tk.Label(top_row, text=time_str, bg=card_bg,
                     fg=NAV_INACTIVE, font=(FONT, 10)).pack(
                         side="left", padx=(10, 0))

        tk.Label(top_row, text=f"${disc:,.2f}", bg=card_bg,
                 fg=amount_color, font=(FONT, 12, "bold")).pack(side="right")

        bot_row = tk.Frame(card, bg=card_bg)
        bot_row.pack(fill="x", padx=12, pady=(2, 8))

        if reason:
            tk.Label(bot_row, text=reason, bg=card_bg,
                     fg="#94A3B8", font=(FONT, 10)).pack(side="left")
        if server:
            tk.Label(bot_row, text=f"Server: {server}", bg=card_bg,
                     fg="#5A7BA5", font=(FONT, 10)).pack(side="right")

    # ── Voids section ────────────────────────────────────────────────────
    def _build_voids_section(self, parent, summary: DailySalesSummary):
        try:
            out_dir = Path(self.winfo_toplevel()._settings.get(
                "output_folder", str(SFTP_DEFAULT_OUT)))
            pay_file = out_dir / summary.date_folder / "PaymentDetails.csv"
            if not pay_file.exists():
                return
        except Exception:
            return

        void_rows = []
        with open(pay_file, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("Status") or "").strip() == "VOIDED":
                    void_rows.append(row)

        if not void_rows:
            return

        frame = tk.Frame(parent, bg=BG_NAV)
        frame.pack(fill="x", padx=24, pady=(20, 0))

        tk.Label(frame, text="Voided Payments", bg=BG_NAV,
                 fg="#FCA5A5", font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Frame(frame, bg="#2D4A7A", height=1).pack(fill="x", pady=(4, 8))

        for row in void_rows:
            chk = (row.get("Check #") or "").strip()
            amt = _safe_float(row.get("Amount"))
            server = (row.get("Server") or "").strip()
            card_type = (row.get("Card Type") or "").strip()
            last4 = (row.get("Last 4 Card Digits") or "").strip()
            void_date = (row.get("Void Date") or "").strip()
            void_user_raw = (row.get("Void User") or "").strip()
            void_approver_raw = (row.get("Void Approver") or "").strip()
            table = (row.get("Table") or "").strip()
            dining = (row.get("Dining Area") or "").strip()

            void_by = _parse_void_user(void_user_raw)
            approved_by = _parse_void_user(void_approver_raw)

            card = tk.Frame(frame, bg="#2A1520",
                            highlightbackground="#4A2030",
                            highlightthickness=1)
            card.pack(fill="x", pady=3)

            # Row 1: check, card info, amount
            r1 = tk.Frame(card, bg="#2A1520")
            r1.pack(fill="x", padx=12, pady=(8, 0))

            tk.Label(r1, text=f"Check #{chk}", bg="#2A1520",
                     fg="#FFFFFF", font=(FONT, 11, "bold")).pack(side="left")

            if card_type:
                card_info = card_type
                if last4:
                    card_info += f" ****{last4}"
                tk.Label(r1, text=card_info, bg="#2A1520",
                         fg="#7A6080", font=(FONT, 10)).pack(
                             side="left", padx=(10, 0))

            tk.Label(r1, text=f"${amt:,.2f}", bg="#2A1520",
                     fg="#FCA5A5", font=(FONT, 12, "bold")).pack(side="right")

            # Row 2: server, table, void time
            r2 = tk.Frame(card, bg="#2A1520")
            r2.pack(fill="x", padx=12, pady=(2, 0))

            details = []
            if server:
                details.append(f"Server: {server}")
            if table:
                loc = f"Table {table}"
                if dining:
                    loc += f" ({dining})"
                details.append(loc)
            if details:
                tk.Label(r2, text="  \u00B7  ".join(details), bg="#2A1520",
                         fg="#7A6080", font=(FONT, 10)).pack(side="left")

            if void_date:
                tk.Label(r2, text=void_date, bg="#2A1520",
                         fg="#7A6080", font=(FONT, 10)).pack(side="right")

            # Row 3: voided by / approved by
            r3 = tk.Frame(card, bg="#2A1520")
            r3.pack(fill="x", padx=12, pady=(2, 8))

            if void_by:
                tk.Label(r3, text=f"Voided by: {void_by}", bg="#2A1520",
                         fg="#7A6080", font=(FONT, 9)).pack(side="left")
            if approved_by and approved_by != void_by:
                tk.Label(r3, text=f"Approved by: {approved_by}",
                         bg="#2A1520", fg="#7A6080",
                         font=(FONT, 9)).pack(side="right")

    def _go_back(self):
        if self._on_back:
            self._on_back()


# ═══════════════════════════════════════════════════════════════════════════════
#  WEEKLY SALES EXPORT — PDF with daily daypart breakdowns + voids/discounts
# ═══════════════════════════════════════════════════════════════════════════════

def export_weekly_sales_pdf(export_dir: Path, dates: list[str],
                            out_path: Path,
                            sales_cache: dict | None = None) -> int:
    """
    Generate a styled PDF with weekly sales summaries.
    Returns the number of days with data.
    sales_cache: optional dict of date_folder -> DailySalesSummary from Firebase.
    """
    if not REPORTLAB_OK:
        raise RuntimeError("reportlab is required for PDF export")

    summaries: list[tuple[str, str, DailySalesSummary]] = []
    for date_folder in dates:
        summary = compute_daily_sales(export_dir, date_folder)
        # Fall back to Firebase-cached data if no local CSV
        if not summary and sales_cache:
            summary = sales_cache.get(date_folder)
        if not summary:
            continue
        try:
            dt = datetime.datetime.strptime(date_folder, "%Y%m%d")
            date_str = dt.strftime("%m/%d/%Y")
            day_name = dt.strftime("%A")
        except ValueError:
            date_str = date_folder
            day_name = ""
        summaries.append((date_str, day_name, summary))

    if not summaries:
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    w, h = letter
    c = rl_canvas.Canvas(str(out_path), pagesize=letter)
    left = 40
    right = w - 40
    cw = right - left

    # Colors
    navy = (0.106, 0.165, 0.290)
    dark_bg = (0.059, 0.114, 0.212)
    accent = (0.329, 0.431, 0.647)
    white = (1, 1, 1)
    light_gray = (0.7, 0.7, 0.7)
    green = (0.196, 0.804, 0.459)
    yellow = (0.988, 0.827, 0.302)
    red = (0.988, 0.647, 0.647)
    purple = (0.655, 0.545, 0.980)

    def _hline(y_pos, weight=0.5):
        c.setStrokeColorRGB(*accent)
        c.setLineWidth(weight)
        c.line(left, y_pos, right, y_pos)

    def _new_page():
        c.showPage()
        return h - 50

    # ── Week header ──────────────────────────────────────────────────────
    y = h - 50
    first_date = summaries[0][0]
    last_date = summaries[-1][0]
    c.setFont("Helvetica-Bold", 20)
    c.setFillColorRGB(*navy)
    c.drawString(left, y, "Room Charge & Sales")
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(*light_gray)
    c.drawRightString(right, y, f"Weekly Sales Report")
    y -= 18
    c.setFont("Helvetica", 11)
    c.drawString(left, y, f"{first_date}  \u2013  {last_date}")
    y -= 6
    _hline(y, 1.5)
    y -= 20

    # ── Week totals ──────────────────────────────────────────────────────
    wk_net = sum(s.total_net for _, _, s in summaries)
    wk_gross = sum(s.total_gross for _, _, s in summaries)
    wk_tax = sum(s.total_tax for _, _, s in summaries)
    wk_disc = sum(s.total_discount for _, _, s in summaries)
    wk_voids = sum(s.void_amount for _, _, s in summaries)
    wk_void_cnt = sum(s.void_count for _, _, s in summaries)
    wk_orders = sum(s.total_orders for _, _, s in summaries)
    wk_total_sales = wk_net + wk_tax

    # Revenue summary box
    c.setFillColorRGB(0.04, 0.08, 0.16)
    c.roundRect(left, y - 80, cw, 80, 6, fill=1, stroke=0)

    box_labels = [
        ("Total Sales", f"${wk_total_sales:,.2f}"),
        ("Net Sales",   f"${wk_net:,.2f}"),
        ("Tax",         f"${wk_tax:,.2f}"),
        ("Discounts",   f"${wk_disc:,.2f}"),
    ]
    col_w = cw / len(box_labels)
    for i, (lbl, val) in enumerate(box_labels):
        cx = left + col_w * i + col_w / 2
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(*light_gray)
        c.drawCentredString(cx, y - 30, lbl)
        c.setFont("Helvetica-Bold", 14)
        c.setFillColorRGB(*white)
        c.drawCentredString(cx, y - 50, val)
    y -= 90

    # Secondary stats line
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(*light_gray)
    stats_line = f"{wk_orders} orders"
    if wk_void_cnt > 0:
        stats_line += f"  |  Voids: {wk_void_cnt} (${wk_voids:,.2f})"
    c.drawString(left + 4, y, stats_line)
    y -= 24

    # ── Daily breakdowns ─────────────────────────────────────────────────
    cols = ["Service", "Orders", "Total Sales", "Net", "Disc", "Tax"]
    col_xs = [left + 4]
    data_start = left + 80
    data_w = (right - 4 - data_start) / (len(cols) - 1)
    for i in range(1, len(cols)):
        col_xs.append(data_start + data_w * (i - 1) + data_w)

    for day_idx, (date_str, day_name, summary) in enumerate(summaries):
        needed = 24 + 16 * (len(summary.dayparts) + 1) + 40
        if y - needed < 50:
            y = _new_page()

        # Day header
        c.setFont("Helvetica-Bold", 12)
        c.setFillColorRGB(*navy)
        c.drawString(left, y, f"{day_name}, {date_str}")
        day_total = summary.total_net + summary.total_tax
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.0, 0.55, 0.3)
        c.drawRightString(right, y, f"Total: ${day_total:,.2f}")
        y -= 4
        _hline(y, 0.8)
        y -= 14

        # Table header
        c.setFont("Helvetica-Bold", 8)
        c.setFillColorRGB(*light_gray)
        for i, col in enumerate(cols):
            if i == 0:
                c.drawString(col_xs[0], y, col)
            else:
                c.drawRightString(col_xs[i], y, col)
        y -= 2
        _hline(y, 0.3)
        y -= 12

        # Daypart rows
        for dp in summary.dayparts:
            c.setFont("Helvetica", 9)
            c.setFillColorRGB(*navy)
            c.drawString(col_xs[0], y, dp.name)
            dp_total_sales = dp.net_sales + dp.tax
            vals = [str(dp.orders), f"${dp_total_sales:,.2f}",
                    f"${dp.net_sales:,.2f}",
                    f"${dp.discount:,.2f}" if dp.discount > 0 else "-",
                    f"${dp.tax:,.2f}"]
            for i, v in enumerate(vals):
                if v == "-":
                    c.setFillColorRGB(0.6, 0.6, 0.65)
                else:
                    c.setFillColorRGB(0.25, 0.30, 0.40)
                c.drawRightString(col_xs[i + 1], y, v)
            y -= 14

        # Total row
        _hline(y + 10, 0.5)
        c.setFont("Helvetica-Bold", 9)
        c.setFillColorRGB(*navy)
        c.drawString(col_xs[0], y, "Total")
        day_total_sales = summary.total_net + summary.total_tax
        tot_vals = [str(summary.total_orders),
                    f"${day_total_sales:,.2f}",
                    f"${summary.total_net:,.2f}",
                    f"${summary.total_discount:,.2f}",
                    f"${summary.total_tax:,.2f}"]
        c.setFillColorRGB(0.2, 0.25, 0.35)
        for i, v in enumerate(tot_vals):
            c.drawRightString(col_xs[i + 1], y, v)
        y -= 4

        # Void line if any
        if summary.void_count > 0:
            y -= 10
            c.setFont("Helvetica", 9)
            c.setFillColorRGB(*red)
            c.drawString(col_xs[0], y,
                         f"Voids: {summary.void_count}  \u2014  ${summary.void_amount:,.2f}")
            y -= 4

        y -= 18  # spacing between days

    # ── Footer ────────────────────────────────────────────────────────────
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(*light_gray)
    c.drawString(left, 30, f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')}  |  Room Charge & Sales")

    c.save()
    return len(summaries)


# ═══════════════════════════════════════════════════════════════════════════════
#  APP ICON HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _set_app_icon(window):
    """Set the app icon on a tkinter window. Works for both .ico and .png."""
    icon_path = _app_root() / "icons" / "app_icon.ico"
    png_path = _app_root() / "icons" / "app_icon.png"
    try:
        if IS_WIN and icon_path.exists():
            window.iconbitmap(str(icon_path))
        elif png_path.exists():
            icon_img = tk.PhotoImage(file=str(png_path))
            window.iconphoto(True, icon_img)
            window._icon_ref = icon_img  # prevent garbage collection
    except Exception:
        pass


def _app_root():
    """Return the app root — works both for normal Python and PyInstaller bundles."""
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        return Path(_sys._MEIPASS)
    return Path(__file__).resolve().parent


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGIN WINDOW — Email + Password (Firebase Auth)
# ═══════════════════════════════════════════════════════════════════════════════

SUPPORT_EMAIL = "stamhadsoftware@gmail.com"
LOGIN_APP_NAME = "Room Charge & Sales"

class LoginWindow(tk.Tk):
    """
    Standalone login window using Firebase Authentication.
    Calls on_success(email, display_name, role, uid, id_token) on login.
    """

    def __init__(self, on_success, prefill_email=""):
        super().__init__()
        self.title("Room Charge & Sales — Sign In")
        _set_app_icon(self)
        self.configure(bg=BG_PAGE)
        _lw, _lh = (440, 560) if IS_WIN else (440, 540)
        self.geometry(f"{_lw}x{_lh}")
        try:
            sx = self.winfo_screenwidth()
            sy = self.winfo_screenheight()
            x = max(0, (sx - _lw) // 2)
            y = max(0, (sy - _lh) // 2 - 40)
            self.geometry(f"{_lw}x{_lh}+{x}+{y}")
        except Exception:
            pass
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
        _card_h = 480 if IS_WIN else 460
        card.place(relx=0.5, rely=0.5, anchor="center", width=380, height=_card_h)

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
        _entry_kw = dict(font=(FONT, _SZ(13)), width=24, bg="#F9FAFB")
        if IS_WIN:
            _entry_kw.update(relief="solid", bd=1, highlightthickness=0)
        else:
            _entry_kw.update(relief="flat", highlightthickness=2,
                             highlightcolor=ACCENT, highlightbackground=BORDER)
        self.email_entry = tk.Entry(card, **_entry_kw)
        self.email_entry.pack(fill="x", padx=40, pady=(4, 14), ipady=3 if IS_WIN else 0)

        # Password
        tk.Label(card, text="Password", bg=BG_CARD, fg=FG,
                 font=(FONT, _SZ(11), "bold"), anchor="w").pack(fill="x", padx=40)
        self.pw_entry = tk.Entry(card, show="\u2022", **_entry_kw)
        self.pw_entry.pack(fill="x", padx=40, pady=(4, 10), ipady=3 if IS_WIN else 0)

        # Remember me
        self.remember_var = tk.BooleanVar(value=False)
        _cb_kw = dict(text="Remember me", variable=self.remember_var,
                      bg=BG_CARD, fg=FG, activebackground=BG_CARD,
                      font=(FONT, _SZ(10)))
        if not IS_WIN:
            _cb_kw["selectcolor"] = "#F9FAFB"
        tk.Checkbutton(card, **_cb_kw).pack(anchor="w", padx=38, pady=(0, 6))

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

        tk.Label(self, text=f"Room Charge & Sales  \u00A9 2026  Stamhad Software",
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
        self.title("Room Charge & Sales")
        _set_app_icon(self)
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
        self.title("Room Charge & Sales")
        _set_app_icon(self)
        self.configure(bg=BG_NAV)
        self.minsize(900, 560)
        # Default window size and center on screen
        win_w, win_h = 1040, 640
        self.geometry(f"{win_w}x{win_h}")
        try:
            sx = self.winfo_screenwidth()
            sy = self.winfo_screenheight()
            x = max(0, (sx - win_w) // 2)
            y = max(0, (sy - win_h) // 2 - 30)
            self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        except Exception:
            pass
        # Windows-specific: use dark title bar on Win 11+
        if IS_WIN:
            try:
                import ctypes
                HWND = ctypes.windll.user32.GetForegroundWindow()
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    HWND, DWMWA_USE_IMMERSIVE_DARK_MODE,
                    ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
            except Exception:
                pass
        _ensure_dirs()

        # Store user info BEFORE building UI (role affects visible elements)
        self._user_role = user_role
        self._user_email = user_email
        self._user_display_name = user_display_name
        self._user_uid = user_uid
        self._id_token = id_token

        # Configure ttk styles for this platform
        _configure_ttk_styles()

        # Load saved settings
        self._settings = _load_settings()
        self._running = False
        self._pulse_id = None
        self._last_receipts: list[RoomChargeReceipt] = []
        self._all_receipts_cache: dict[str, dict[str, RoomChargeReceipt]] = {}
        self._sales_cache: dict[str, DailySalesSummary | None] = {}

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

        tk.Label(top_bar, text="Room Charge & Sales", bg=BG_NAV, fg="#FFFFFF",
                 font=(FONT, 20, "bold")).pack(side="left")

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

        # ── Tab bar ────────────────────────────────────────────────────
        is_viewer = self._user_role == "viewer"
        self._current_tab = "room_charge" if is_viewer else "sales"
        tab_bar = tk.Frame(self._main, bg=BG_NAV)
        tab_bar.pack(fill="x", padx=20, pady=(10, 0))

        TAB_ACTIVE_BG = "#2563EB"
        TAB_INACTIVE_BG = "#1E3358"
        self._TAB_ACTIVE_BG = TAB_ACTIVE_BG
        self._TAB_INACTIVE_BG = TAB_INACTIVE_BG

        self._tab_sales_btn = tk.Label(
            tab_bar, text="  Sales  ",
            bg=TAB_ACTIVE_BG if not is_viewer else TAB_INACTIVE_BG,
            fg="#FFFFFF" if not is_viewer else NAV_INACTIVE,
            font=(FONT, 11, "bold"), padx=20, pady=6, cursor="hand2")
        # Viewers cannot access the Sales tab
        if not is_viewer:
            self._tab_sales_btn.pack(side="left")
            self._tab_sales_btn.bind("<Button-1>",
                                     lambda e: self._switch_tab("sales"))

        self._tab_rc_btn = tk.Label(
            tab_bar, text="  Room Charge  ",
            bg=TAB_ACTIVE_BG if is_viewer else TAB_INACTIVE_BG,
            fg="#FFFFFF" if is_viewer else NAV_INACTIVE,
            font=(FONT, 11, "bold"), padx=20, pady=6, cursor="hand2")
        self._tab_rc_btn.pack(side="left", padx=(2 if not is_viewer else 0, 0))
        self._tab_rc_btn.bind("<Button-1>",
                              lambda e: self._switch_tab("room_charge"))

        # ── Action bar ─────────────────────────────────────────────────
        action_bar = tk.Frame(self._main, bg=BG_NAV)
        action_bar.pack(fill="x", padx=20, pady=(10, 0))
        self._action_bar = action_bar

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

        # Export This Month PDF button (Room Charge tab)
        self._export_this_frame = tk.Frame(action_bar, bg=ACCENT,
                                           highlightbackground=ACCENT,
                                           highlightthickness=1, cursor="hand2")
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

        # Export Last Month PDF button (Room Charge tab)
        self._export_frame = tk.Frame(action_bar, bg="#374151",
                                      highlightbackground="#374151",
                                      highlightthickness=1, cursor="hand2")
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

        # Export Weekly Sales PDF button (Sales tab)
        self._export_week_frame = tk.Frame(action_bar, bg="#5B21B6",
                                           highlightbackground="#5B21B6",
                                           highlightthickness=1, cursor="hand2")
        self._export_week_frame.pack(side="left", padx=(10, 0))
        self._export_week_lbl = tk.Label(
            self._export_week_frame, text="Export Week Sales", bg="#5B21B6",
            fg="#FFFFFF", font=(FONT, 11, "bold"), padx=16, pady=8,
            cursor="hand2")
        self._export_week_lbl.pack()
        for w in (self._export_week_frame, self._export_week_lbl):
            w.bind("<Enter>", lambda e: (
                self._export_week_frame.config(bg="#6D28D9", highlightbackground="#6D28D9"),
                self._export_week_lbl.config(bg="#6D28D9")))
            w.bind("<Leave>", lambda e: (
                self._export_week_frame.config(bg="#5B21B6", highlightbackground="#5B21B6"),
                self._export_week_lbl.config(bg="#5B21B6")))
            w.bind("<Button-1>", lambda e: self._export_weekly_sales())

        # Export Last Week Sales PDF button (Sales tab)
        self._export_lastweek_frame = tk.Frame(action_bar, bg="#374151",
                                               highlightbackground="#374151",
                                               highlightthickness=1, cursor="hand2")
        self._export_lastweek_frame.pack(side="left", padx=(10, 0))
        self._export_lastweek_lbl = tk.Label(
            self._export_lastweek_frame, text="Export Last Week", bg="#374151",
            fg="#FFFFFF", font=(FONT, 11, "bold"), padx=16, pady=8,
            cursor="hand2")
        self._export_lastweek_lbl.pack()
        for w in (self._export_lastweek_frame, self._export_lastweek_lbl):
            w.bind("<Enter>", lambda e: (
                self._export_lastweek_frame.config(bg="#4B5563", highlightbackground="#4B5563"),
                self._export_lastweek_lbl.config(bg="#4B5563")))
            w.bind("<Leave>", lambda e: (
                self._export_lastweek_frame.config(bg="#374151", highlightbackground="#374151"),
                self._export_lastweek_lbl.config(bg="#374151")))
            w.bind("<Button-1>", lambda e: self._export_weekly_sales(last_week=True))

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

        # Show correct buttons for the default tab (sales)
        self._update_tab_buttons()

        # ── Log area (hidden until running) ──────────────────────────────
        self._log_frame = tk.Frame(self._main, bg=BG_NAV)

        _mono = "Consolas" if IS_WIN else "Courier"
        self._log = tk.Text(self._log_frame, bg="#0F1D36", fg="#A0CFFF",
                            font=(_mono, 9), height=6, relief="flat",
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
                                font=(_mono, 9, "bold"))

        # ── Footer ──────────────────────────────────────────────────────
        tk.Label(self._main, text="Stamhad Software", bg=BG_NAV,
                 fg="#3A5278", font=(FONT, 9)).pack(side="bottom", pady=(0, 8))

    # ── Build a single day card ───────────────────────────────────────────────
    def _build_day_card(self, col: int, dt: datetime.date,
                        receipts: list[RoomChargeReceipt] | None = None):
        is_today = dt == datetime.date.today()
        card_bg = "#1E3A5F" if is_today else "#162240"

        card = tk.Frame(self._cards_frame, bg=card_bg,
                        highlightbackground=ACCENT if is_today else "#2D4A7A",
                        highlightthickness=2 if is_today else 1)
        card.grid(row=0, column=col, sticky="nsew", padx=3, pady=3)

        day_name = self.DAY_NAMES[dt.weekday()]
        day_num = dt.strftime("%d")

        # Day header (shared between tabs)
        tk.Label(card, text=day_name, bg=card_bg,
                 fg="#FFFFFF" if is_today else NAV_INACTIVE,
                 font=(FONT, 11, "bold")).pack(pady=(10, 0))
        tk.Label(card, text=day_num, bg=card_bg,
                 fg="#FFFFFF" if is_today else "#5A7BA5",
                 font=(FONT, 20, "bold")).pack(pady=(0, 6))

        sep = tk.Frame(card, bg="#2D4A7A", height=1)
        sep.pack(fill="x", padx=10, pady=(0, 6))

        key = dt.strftime("%Y%m%d")
        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
        has_sales_data = ((Path(out_dir) / key / "PaymentDetails.csv").exists()
                          or key in self._sales_cache)
        clickable = False

        if self._current_tab == "sales":
            # ── Sales tab card content ──
            if has_sales_data:
                summary = self._sales_cache.get(key)
                if summary:
                    total_sales = summary.total_net + summary.total_tax
                    tk.Label(card, text=f"{summary.total_orders} orders",
                             bg=card_bg, fg=NAV_INACTIVE,
                             font=(FONT, 8)).pack(pady=(2, 0))
                    tk.Label(card, text=f"${total_sales:,.2f}", bg=card_bg,
                             fg="#6EE7B7", font=(FONT, 14, "bold")).pack(pady=(2, 0))
                    tk.Label(card, text="total sales", bg=card_bg,
                             fg=NAV_INACTIVE, font=(FONT, 8)).pack()
                    tk.Label(card, text=f"${summary.total_net:,.2f}", bg=card_bg,
                             fg="#38BDF8", font=(FONT, 12, "bold")).pack(pady=(2, 0))
                    tk.Label(card, text="net sales", bg=card_bg,
                             fg=NAV_INACTIVE, font=(FONT, 8)).pack()
                    clickable = True
                    for widget in [card] + list(card.winfo_children()):
                        widget.config(cursor="hand2")
                        widget.bind("<Button-1>",
                                    lambda e, d=key, dr=receipts:
                                        self._show_day_sales(d, dr))
                else:
                    tk.Label(card, text="No data", bg=card_bg, fg="#3A5278",
                             font=(FONT, 9)).pack(pady=(4, 0))
            else:
                tk.Label(card, text="—", bg=card_bg, fg="#3A5278",
                         font=(FONT, 14)).pack(expand=True)
        else:
            # ── Room Charge tab card content ──
            count = len(receipts) if receipts else 0
            day_sales = sum(r.total for r in receipts) if receipts else 0
            day_tips = sum(r.tip for r in receipts) if receipts else 0

            if count > 0:
                type_counts = Counter(r.charge_type for r in receipts)
                type_parts = []
                for ct, n in type_counts.most_common():
                    short = ct.replace(" Charge", "")
                    type_parts.append(f"{n} {short}")
                tk.Label(card, text=" · ".join(type_parts),
                         bg=card_bg, fg=NAV_INACTIVE,
                         font=(FONT, 8)).pack(pady=(2, 0))

                tk.Label(card, text=f"${day_sales:,.2f}", bg=card_bg,
                         fg="#6EE7B7", font=(FONT, 14, "bold")).pack(pady=(2, 0))
                tk.Label(card, text="charges", bg=card_bg, fg=NAV_INACTIVE,
                         font=(FONT, 8)).pack()
                tk.Label(card, text=f"${day_tips:,.2f}", bg=card_bg,
                         fg="#7EB8FF", font=(FONT, 12, "bold")).pack(pady=(2, 0))
                tk.Label(card, text="tips", bg=card_bg, fg=NAV_INACTIVE,
                         font=(FONT, 8)).pack()

                clickable = True
                for widget in [card] + list(card.winfo_children()):
                    widget.config(cursor="hand2")
                    widget.bind("<Button-1>",
                                lambda e, d=key, dr=receipts: self._show_day(d, dr))
            elif has_sales_data:
                tk.Label(card, text="No charges", bg=card_bg, fg="#3A5278",
                         font=(FONT, 9)).pack(pady=(4, 0))
            else:
                tk.Label(card, text="—", bg=card_bg, fg="#3A5278",
                         font=(FONT, 14)).pack(expand=True)

        # Hover effect
        if clickable:
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

        # Store reference
        if col < len(self._day_cards):
            self._day_cards[col] = {"frame": card, "dt": dt}
        else:
            self._day_cards.append({"frame": card, "dt": dt})

    # ── Tab switching helpers ─────────────────────────────────────────────────
    def _switch_tab(self, tab_name: str):
        """Switch between 'sales' and 'room_charge' tabs."""
        if tab_name == self._current_tab:
            return
        # Viewers can only access room_charge
        if tab_name == "sales" and self._user_role == "viewer":
            return
        self._current_tab = tab_name
        self._update_tab_buttons()
        # Rebuild day cards for the new tab
        receipts = getattr(self, "_last_receipts", [])
        self._refresh_calendar(receipts)

    def _update_tab_buttons(self):
        """Update tab button styling and show/hide action buttons per tab."""
        if self._current_tab == "sales":
            self._tab_sales_btn.config(bg=self._TAB_ACTIVE_BG, fg="#FFFFFF")
            self._tab_rc_btn.config(bg=self._TAB_INACTIVE_BG, fg=NAV_INACTIVE)
            # Show Sales exports, hide Room Charge exports
            self._export_week_frame.pack(side="left", padx=(10, 0))
            self._export_lastweek_frame.pack(side="left", padx=(10, 0))
            self._export_this_frame.pack_forget()
            self._export_frame.pack_forget()
        else:
            self._tab_rc_btn.config(bg=self._TAB_ACTIVE_BG, fg="#FFFFFF")
            self._tab_sales_btn.config(bg=self._TAB_INACTIVE_BG, fg=NAV_INACTIVE)
            # Show Room Charge exports, hide Sales exports
            self._export_week_frame.pack_forget()
            self._export_lastweek_frame.pack_forget()
            self._export_this_frame.pack(side="left", padx=(10, 0))
            self._export_frame.pack(side="left", padx=(10, 0))

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
        local_receipts = []
        if export_path.exists():
            try:
                local_receipts = find_room_charges(export_path)
            except Exception:
                pass
        self._refresh_calendar(local_receipts)
        # Always try Firebase to fill in missing data (sales + receipts)
        self._load_from_firebase()

    # ── Refresh all day cards with new receipt data ───────────────────────────
    def _refresh_calendar(self, receipts: list[RoomChargeReceipt]):
        """Rebuild all 7 day cards with updated data."""
        self._last_receipts = receipts

        # Update persistent receipts cache (keyed by date_folder + check_id)
        for r in receipts:
            cache_key = r.date_folder
            if cache_key not in self._all_receipts_cache:
                self._all_receipts_cache[cache_key] = {}
            self._all_receipts_cache[cache_key][(r.date_folder, r.check_id)] = r

        # Build by_date from persistent cache for current week's dates
        by_date: dict[str, list[RoomChargeReceipt]] = {}
        for dt in self._dates:
            key = dt.strftime("%Y%m%d")
            if key in self._all_receipts_cache:
                by_date[key] = list(self._all_receipts_cache[key].values())

        # Pre-compute sales data for the current week's dates only.
        # Keep all other cached entries (from Firebase or other weeks) intact.
        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
        for dt in self._dates:
            key = dt.strftime("%Y%m%d")
            if (Path(out_dir) / key / "PaymentDetails.csv").exists():
                # Local CSV always wins — recompute from source
                self._sales_cache[key] = compute_daily_sales(Path(out_dir), key)
            # Otherwise keep whatever is already cached (Firebase data)

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
        local_receipts = []
        if export_path.exists():
            try:
                local_receipts = find_room_charges(export_path)
            except Exception:
                pass

        if local_receipts:
            self._refresh_calendar(local_receipts)
        # Always try Firebase to fill in missing data (sales + receipts)
        self._load_from_firebase()

    def _load_from_firebase(self):
        """Fetch orders and sales from Firestore and populate the calendar."""
        if not AUTH_OK or not hasattr(self, "_id_token") or not self._id_token:
            return

        date_keys = [dt.strftime("%Y%m%d") for dt in self._dates]

        def _worker():
            try:
                # Fetch room charge orders
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

                # Fetch sales summaries from Firebase
                fb_sales = auth.fetch_sales(self._id_token, date_keys)
                fb_sales_cache: dict[str, DailySalesSummary] = {}
                for s in fb_sales:
                    key = s.get("date_folder", "")
                    if not key:
                        continue
                    dayparts = []
                    for dp in s.get("dayparts", []):
                        dayparts.append(DaypartSales(
                            name=str(dp.get("name", "Other")),
                            orders=int(dp.get("orders", 0)),
                            net_sales=float(dp.get("net_sales", 0)),
                            tax=float(dp.get("tax", 0)),
                            discount=float(dp.get("discount", 0)),
                            gross_sales=float(dp.get("gross_sales", 0)),
                        ))
                    fb_sales_cache[key] = DailySalesSummary(
                        date_folder=key,
                        dayparts=dayparts,
                        void_amount=float(s.get("void_amount", 0)),
                        void_count=int(s.get("void_count", 0)),
                        total_orders=int(s.get("total_orders", 0)),
                        total_guests=int(s.get("total_guests", 0)),
                    )

                def _apply():
                    # Merge Firebase receipts with any existing local ones
                    existing = getattr(self, "_last_receipts", [])
                    existing_ids = {(r.date_folder, r.check_id) for r in existing}
                    new_receipts = [r for r in receipts
                                    if (r.date_folder, r.check_id)
                                    not in existing_ids]
                    merged = existing + new_receipts
                    if merged or fb_sales_cache:
                        # Merge Firebase sales into cache (only where local is missing)
                        for key, summary in fb_sales_cache.items():
                            if key not in self._sales_cache:
                                self._sales_cache[key] = summary
                        if merged:
                            self._refresh_calendar(merged)
                        elif fb_sales_cache:
                            # No receipts changed but sales loaded — rebuild cards
                            self._refresh_calendar(
                                getattr(self, "_last_receipts", []))
                self.after(0, _apply)
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

    def _show_day_sales(self, date_key: str,
                        receipts: list[RoomChargeReceipt] | None = None):
        """Show the daily sales summary view for a given date."""
        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
        summary = compute_daily_sales(Path(out_dir), date_key)
        # Fall back to Firebase-cached data if no local CSV
        if not summary:
            summary = self._sales_cache.get(date_key)
        if not summary:
            messagebox.showinfo(
                "No Sales Data",
                f"No sales data available for {date_key}.\n\n"
                "Make sure the data has been downloaded first.",
                parent=self)
            return
        self._main.pack_forget()
        self._detail = DaySalesView(
            self, summary=summary, receipts=receipts,
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
    # ── SSH Key resolution (Firebase → local → browse → upload) ────────────
    def _resolve_ssh_key(self, interactive: bool = True) -> str | None:
        """
        Resolve the SSH key in priority order:
        1. Try loading from Firebase
        2. Check local path (settings or default)
        3. Check common fallback paths (e.g. ~/Desktop/toast/keys/)
        4. If interactive, open file picker
        5. Auto-upload to Firebase for future machines

        Returns the local file path to the key, or None if not found.
        """
        dest_path = str(_default_data_dir() / "keys" / "toast_rsa_key")

        # ── 1. Try Firebase first ──────────────────────────────────────────
        if AUTH_OK and self._id_token and self._user_uid:
            try:
                ok, result = auth.load_ssh_key_from_firebase(
                    self._id_token, self._user_uid, dest_path)
                if ok:
                    self._settings["ssh_key"] = result
                    _save_settings(self._settings)
                    return result
            except Exception:
                pass

        # ── 2. Check configured local path ─────────────────────────────────
        local_key = self._settings.get("ssh_key", str(SFTP_DEFAULT_KEY))
        if Path(local_key).exists():
            # Auto-upload to Firebase if not there yet
            self._maybe_upload_key_to_firebase(local_key)
            return local_key

        # ── 3. Check common fallback paths ─────────────────────────────────
        fallback_paths = [
            Path.home() / "Desktop" / "toast" / "keys" / "toast_rsa_key",
            Path.home() / "Desktop" / "toast_rsa_key",
            Path.home() / "Downloads" / "toast_rsa_key",
            Path.home() / ".ssh" / "toast_rsa_key",
        ]
        for fp in fallback_paths:
            if fp.exists():
                # Copy to standard location
                import shutil
                dest = Path(dest_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(fp), str(dest))
                if platform.system() != "Windows":
                    dest.chmod(0o600)
                self._settings["ssh_key"] = str(dest)
                _save_settings(self._settings)
                # Auto-upload to Firebase
                self._maybe_upload_key_to_firebase(str(dest))
                return str(dest)

        # ── 4. Interactive: open file picker ───────────────────────────────
        if not interactive:
            return None

        return None  # Caller handles the file picker flow

    def _maybe_upload_key_to_firebase(self, key_path: str):
        """Upload a local key to Firebase in a background thread (non-blocking)."""
        if not AUTH_OK or not self._id_token or not self._user_uid:
            return

        def _worker():
            try:
                if not auth.check_ssh_key_in_firebase(
                        self._id_token, self._user_uid):
                    auth.save_ssh_key_to_firebase(
                        self._id_token, self._user_uid, key_path)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _browse_and_install_key(self) -> str | None:
        """Open a file picker, validate the key, copy locally, upload to Firebase."""
        key_file = filedialog.askopenfilename(
            title="Select your Toast SSH key file",
            filetypes=[("All files", "*"), ("All files", "*.*")],
            parent=self)
        if not key_file:
            return None

        # Validate: check first line for PRIVATE KEY / BEGIN
        try:
            with open(key_file, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline().strip()
            if "PRIVATE KEY" not in first_line and "BEGIN" not in first_line:
                proceed = messagebox.askyesno(
                    "Key Validation Warning",
                    "This doesn't look like an SSH private key.\n\n"
                    f"First line: {first_line[:60]}\n\n"
                    "Upload anyway?",
                    parent=self)
                if not proceed:
                    return None
        except Exception:
            pass  # Binary file or unreadable — still allow

        # Copy to standard location
        import shutil
        dest_dir = _default_data_dir() / "keys"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / "toast_rsa_key"
        try:
            shutil.copy2(key_file, str(dest_file))
            if platform.system() != "Windows":
                dest_file.chmod(0o600)
        except Exception as exc:
            messagebox.showerror(
                "Setup Error",
                f"Could not copy key file:\n{exc}",
                parent=self)
            return None

        # Save to settings
        self._settings["ssh_key"] = str(dest_file)
        _save_settings(self._settings)

        # Upload to Firebase
        if AUTH_OK and self._id_token and self._user_uid:
            ok, msg = auth.save_ssh_key_to_firebase(
                self._id_token, self._user_uid, str(dest_file))
            if ok:
                Toast(self, "SSH key saved & synced to cloud!")
            else:
                Toast(self, "Key saved locally (cloud sync failed)",
                      bg=WARN_BG, fg=WARN_FG)
        else:
            Toast(self, "SSH key saved locally!")

        return str(dest_file)

    def _check_first_run_setup(self):
        """If no SSH key is available anywhere, prompt the admin to set one up."""
        # Try resolving without user interaction first
        resolved = self._resolve_ssh_key(interactive=False)
        if resolved:
            return  # Key found (either Firebase or local)

        answer = messagebox.askyesno(
            "Welcome — First-Time Setup",
            "No SSH key was found for fetching data from Toast.\n\n"
            "Would you like to select your SSH key file now?\n"
            "It will be synced to your account so other computers "
            "can use it automatically.\n\n"
            "(You can also do this later via the \u2699 Settings icon.)",
            parent=self)
        if not answer:
            return

        self._browse_and_install_key()

    # ── Start action ──────────────────────────────────────────────────────────
    def _on_start(self):
        if self._running:
            return

        # Resolve SSH key: Firebase → local → fallbacks → file picker
        key_path = self._resolve_ssh_key(interactive=False)
        if not key_path:
            # No key found automatically — ask user to browse
            answer = messagebox.askyesno(
                "SSH Key Required",
                "No SSH key found locally or in your cloud account.\n\n"
                "Would you like to select your SSH key file now?",
                parent=self)
            if answer:
                key_path = self._browse_and_install_key()
            if not key_path:
                return

        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))

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

        # ── Phase 4: Upload sales summaries to Firebase ──────────────────
        if AUTH_OK and hasattr(self, "_id_token") and self._id_token:
            self._update_status("Uploading sales to Firebase")
            self._log_safe("\nUploading sales summaries to Firebase...", "info")
            sales_dicts = []
            for dt in self._dates:
                key = dt.strftime("%Y%m%d")
                summary = compute_daily_sales(export_path, key)
                if summary:
                    sales_dicts.append({
                        "date_folder": key,
                        "dayparts": [
                            {"name": dp.name, "orders": dp.orders,
                             "net_sales": dp.net_sales, "tax": dp.tax,
                             "discount": dp.discount,
                             "gross_sales": dp.gross_sales}
                            for dp in summary.dayparts
                        ],
                        "void_amount": summary.void_amount,
                        "void_count": summary.void_count,
                        "total_orders": summary.total_orders,
                        "total_guests": summary.total_guests,
                    })
            if sales_dicts:
                try:
                    ok, errs = auth.upload_sales_batch(
                        self._id_token, sales_dicts, log_fn=self._log_safe)
                    self._log_safe(
                        f"\nFirebase sales: {ok} uploaded, {errs} error(s).",
                        "bold")
                except Exception as exc:
                    self._log_safe(
                        f"\nFirebase sales upload failed: {exc}", "err")

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

    # ── Export Weekly Sales PDF ──────────────────────────────────────────────
    def _export_weekly_sales(self, last_week: bool = False):
        """Export a week's sales data to a PDF file.
        If last_week=True, exports the week before the currently displayed one."""
        if self._running:
            return

        if not REPORTLAB_OK:
            messagebox.showerror("reportlab not installed",
                                 "The 'reportlab' package is required.\n\n"
                                 "Install it with:  pip install reportlab",
                                 parent=self)
            return

        out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
        export_path = Path(out_dir)

        # Determine the Monday for the target week
        target_monday = self._current_monday
        if last_week:
            target_monday = self._current_monday - datetime.timedelta(weeks=1)

        # Build date strings for the target week (Mon → Sun)
        dates = [
            (target_monday + datetime.timedelta(days=i)).strftime("%Y%m%d")
            for i in range(7)
        ]

        week_start = target_monday.strftime("%m-%d")
        week_end = (target_monday + datetime.timedelta(days=6)).strftime("%m-%d-%Y")
        default_name = f"WeeklySales_{week_start}_to_{week_end}.pdf"

        save_path = filedialog.asksaveasfilename(
            title="Export Weekly Sales",
            initialfile=default_name,
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            parent=self)
        if not save_path:
            return

        try:
            count = export_weekly_sales_pdf(export_path, dates, Path(save_path),
                                              sales_cache=self._sales_cache)
            if count == 0:
                messagebox.showinfo(
                    "No Data",
                    "No sales data found for this week.\n\n"
                    "Make sure you have downloaded the SFTP data first.",
                    parent=self)
            else:
                Toast(self, f"Exported {count} day(s) of sales data!")
                # Try to open the file
                try:
                    if IS_MAC:
                        subprocess.Popen(["open", save_path])
                    elif IS_WIN:
                        os.startfile(save_path)
                    else:
                        subprocess.Popen(["xdg-open", save_path])
                except Exception:
                    pass
        except Exception as exc:
            messagebox.showerror("Export Error",
                                 f"Failed to export weekly sales:\n{exc}",
                                 parent=self)

    # ── Export Month PDF (this or last) ──────────────────────────────────────
    def _export_month_pdf(self, which: str = "this"):
        """
        Generate a combined PDF for this month or last month.
        Firebase is the PRIMARY data source — always fetched first.
        Local CSV / cache data supplements anything Firebase missed.
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

        # Ask user where to save FIRST (so they can cancel before the fetch)
        save_path = filedialog.asksaveasfilename(
            title=f"Export {month_display} PDF",
            initialfile=f"{month_label}.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            parent=self)
        if not save_path:
            return

        # ── Show a progress window while fetching ────────────────────────
        progress_win = tk.Toplevel(self)
        progress_win.title("Exporting…")
        progress_win.resizable(False, False)
        progress_win.transient(self)
        progress_win.grab_set()
        pw, ph = 340, 100
        sx = self.winfo_rootx() + (self.winfo_width() - pw) // 2
        sy = self.winfo_rooty() + (self.winfo_height() - ph) // 2
        progress_win.geometry(f"{pw}x{ph}+{sx}+{sy}")
        progress_label = tk.Label(progress_win,
                                  text=f"Fetching {month_display} data from cloud…",
                                  font=("Helvetica", 12))
        progress_label.pack(expand=True, padx=20, pady=20)

        def _update_label(msg):
            try:
                progress_label.config(text=msg)
                progress_win.update_idletasks()
            except Exception:
                pass

        def _do_export():
            error_msg = ""
            month_receipts_by_key: dict[tuple[str, str], RoomChargeReceipt] = {}
            firebase_ok = False

            # ── Source 1 (PRIMARY): Firebase ──────────────────────────────
            if AUTH_OK and hasattr(self, "_id_token") and self._id_token:
                try:
                    date_keys = []
                    d = first_day
                    while d <= last_day:
                        date_keys.append(d.strftime("%Y%m%d"))
                        d += datetime.timedelta(days=1)

                    self.after(0, lambda: _update_label(
                        f"Fetching {month_display} from cloud…"))

                    orders = auth.fetch_orders(self._id_token, date_keys)
                    for o in orders:
                        df = str(o.get("date_folder", ""))
                        cid = str(o.get("check_id", ""))
                        items = []
                        for it in o.get("items", []):
                            items.append(ReceiptItem(
                                name=str(it.get("name", "")),
                                qty=float(it.get("qty", 0)),
                                net_price=float(it.get("net_price", 0)),
                                tax=float(it.get("tax", 0)),
                            ))
                        month_receipts_by_key[(df, cid)] = RoomChargeReceipt(
                            date_folder=df,
                            check_id=cid,
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
                        )
                    firebase_ok = True
                except Exception as exc:
                    error_msg = f"Firebase fetch error: {exc}"

            fb_count = len(month_receipts_by_key)

            # ── Source 2: Local CSV data (fills in anything Firebase missed) ──
            self.after(0, lambda: _update_label("Checking local files…"))
            out_dir = self._settings.get("output_folder", str(SFTP_DEFAULT_OUT))
            export_path = Path(out_dir)
            if export_path.exists():
                try:
                    all_receipts = find_room_charges(export_path)
                    for r in all_receipts:
                        try:
                            dt = datetime.datetime.strptime(
                                r.date_folder, "%Y%m%d").date()
                            if first_day <= dt <= last_day:
                                if (r.date_folder, r.check_id) not in month_receipts_by_key:
                                    month_receipts_by_key[
                                        (r.date_folder, r.check_id)] = r
                        except ValueError:
                            continue
                except Exception:
                    pass

            # ── Source 3: In-memory cache (fills in remaining gaps) ───────
            if hasattr(self, "_all_receipts_cache"):
                for date_key, receipts_dict in self._all_receipts_cache.items():
                    try:
                        dt = datetime.datetime.strptime(
                            date_key, "%Y%m%d").date()
                        if first_day <= dt <= last_day:
                            for (df, cid), r in receipts_dict.items():
                                if (df, cid) not in month_receipts_by_key:
                                    month_receipts_by_key[(df, cid)] = r
                    except ValueError:
                        continue

            month_receipts = list(month_receipts_by_key.values())
            total_count = len(month_receipts)
            local_extra = total_count - fb_count

            # ── Generate PDF ─────────────────────────────────────────────
            if not month_receipts:
                self.after(0, lambda: _finish_export(
                    None, error_msg or f"No charge receipts found for {month_display}."))
                return

            month_receipts.sort(key=lambda r: (r.date_folder, r.check_number))

            self.after(0, lambda: _update_label(
                f"Generating PDF — {total_count} receipts…"))

            try:
                generate_combined_pdf(month_receipts, Path(save_path))
                summary = (f"Exported {month_display} — "
                           f"{total_count} receipts")
                if firebase_ok:
                    summary += f" ({fb_count} from cloud"
                    if local_extra > 0:
                        summary += f", +{local_extra} local"
                    summary += ")"
                self.after(0, lambda: _finish_export(summary, None))
            except Exception as exc:
                self.after(0, lambda: _finish_export(
                    None, f"Failed to generate PDF:\n{exc}"))

        def _finish_export(success_msg, err_msg):
            try:
                progress_win.grab_release()
                progress_win.destroy()
            except Exception:
                pass
            if err_msg:
                messagebox.showerror("Export Error", err_msg, parent=self)
            elif success_msg:
                Toast(self, success_msg)

        threading.Thread(target=_do_export, daemon=True).start()

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
    """Modal settings window with SSH key management and output folder."""

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
        w, h = 560, 440
        px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{px}+{py}")

        body = tk.Frame(self, bg=BG_PAGE)
        body.pack(fill="both", expand=True, padx=24, pady=20)

        tk.Label(body, text="Settings", bg=BG_PAGE, fg=FG,
                 font=(FONT, 18, "bold")).pack(anchor="w", pady=(0, 16))

        # ── Toast SSH Key ────────────────────────────────────────────────
        card1 = Card(body)
        card1.pack(fill="x", pady=(0, 12))

        hdr_row = tk.Frame(card1, bg=BG_CARD)
        hdr_row.pack(fill="x")
        tk.Label(hdr_row, text="Toast SSH Key", bg=BG_CARD, fg=FG,
                 font=(FONT, 12, "bold")).pack(side="left")
        # Status badge (filled in by _refresh_key_status)
        self._key_status_lbl = tk.Label(
            hdr_row, text="  Checking...  ", bg="#E5E7EB", fg="#374151",
            font=(FONT, 9, "bold"), padx=8, pady=2)
        self._key_status_lbl.pack(side="right")

        tk.Label(card1, text="RSA key for Toast SFTP — synced to your account",
                 bg=BG_CARD, fg=FG_SEC, font=(FONT, 10)).pack(
                     anchor="w", pady=(0, 8))

        # Buttons row
        key_btns = tk.Frame(card1, bg=BG_CARD)
        key_btns.pack(fill="x")
        Btn(key_btns, text="Browse & Upload Key", style="primary",
            command=self._browse_and_upload).pack(side="left")
        self._sync_btn = Btn(key_btns, text="Sync Local → Cloud",
                             style="outline", command=self._sync_to_firebase)
        self._sync_btn.pack(side="left", padx=(8, 0))

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

        # ── Save / Cancel ────────────────────────────────────────────────
        btn_row = tk.Frame(body, bg=BG_PAGE)
        btn_row.pack(fill="x")
        Btn(btn_row, text="Save", style="primary",
            command=self._save).pack(side="right")
        Btn(btn_row, text="Cancel", style="ghost",
            command=self.destroy).pack(side="right", padx=(0, 8))

        # Check key status in background
        self.after(100, self._refresh_key_status)

    def _refresh_key_status(self):
        """Determine SSH key status and update the badge."""
        def _worker():
            local_exists = Path(
                self._settings.get("ssh_key", str(SFTP_DEFAULT_KEY))
            ).exists()
            cloud_exists = False
            if (AUTH_OK and self._parent._id_token
                    and self._parent._user_uid):
                try:
                    cloud_exists = auth.check_ssh_key_in_firebase(
                        self._parent._id_token, self._parent._user_uid)
                except Exception:
                    pass
            self.after(0, lambda: self._update_badge(local_exists, cloud_exists))

        threading.Thread(target=_worker, daemon=True).start()

    def _update_badge(self, local_exists: bool, cloud_exists: bool):
        self._local_exists = local_exists
        self._cloud_exists = cloud_exists
        if cloud_exists and local_exists:
            self._key_status_lbl.config(
                text="  Synced to Cloud  ", bg=SUCCESS_BG, fg=SUCCESS_FG)
        elif local_exists and not cloud_exists:
            self._key_status_lbl.config(
                text="  Local Only  ", bg=WARN_BG, fg=WARN_FG)
        elif cloud_exists and not local_exists:
            self._key_status_lbl.config(
                text="  In Cloud (not local)  ", bg="#DBEAFE", fg="#1E40AF")
        else:
            self._key_status_lbl.config(
                text="  Not Configured  ", bg="#FEE2E2", fg="#991B1B")

    def _browse_and_upload(self):
        """Browse for key file, validate, copy locally, upload to Firebase."""
        result = self._parent._browse_and_install_key()
        if result:
            self._refresh_key_status()

    def _sync_to_firebase(self):
        """Upload the existing local key to Firebase."""
        local_key = self._settings.get("ssh_key", str(SFTP_DEFAULT_KEY))
        if not Path(local_key).exists():
            messagebox.showwarning(
                "No Local Key",
                "No local SSH key found to sync.\n"
                "Use 'Browse & Upload Key' to select one first.",
                parent=self)
            return

        if not AUTH_OK or not self._parent._id_token:
            messagebox.showwarning(
                "Not Signed In",
                "You must be signed in to sync to the cloud.",
                parent=self)
            return

        ok, msg = auth.save_ssh_key_to_firebase(
            self._parent._id_token, self._parent._user_uid, local_key)
        if ok:
            Toast(self._parent, "SSH key synced to cloud!")
            self._refresh_key_status()
        else:
            messagebox.showerror(
                "Sync Failed", f"Could not upload key:\n{msg}", parent=self)

    def _browse_out(self):
        p = filedialog.askdirectory(
            title="Select Output Folder",
            initialdir=str(SFTP_DEFAULT_OUT.parent))
        if p:
            self._out_inp.delete(0, "end")
            self._out_inp.insert(0, p)

    def _save(self):
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
