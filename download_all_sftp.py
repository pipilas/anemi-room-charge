#!/usr/bin/env python3
"""
Download ALL files from the latest Toast SFTP date folder.
Saves to: ./all_exports/<date>/

Run:  python3 download_all_sftp.py
"""

import os
import paramiko
from pathlib import Path

# ── SFTP Config ─────────────────────────────────────────────────────────────
SFTP_HOST      = "s-9b0f88558b264dfda.server.transfer.us-east-1.amazonaws.com"
SFTP_PORT      = 22
SFTP_USERNAME  = "MamakaMeditteraneanDataExports"
SFTP_EXPORT_ID = "287721"

# Try to find the SSH key
KEY_PATHS = [
    Path.home() / "Library" / "Application Support" / "AnemiRoomCharge" / "keys" / "toast_rsa_key",
    Path.home() / "Desktop" / "toast" / "keys" / "toast_rsa_key",
    Path(__file__).parent / "keys" / "toast_rsa_key",
]

key_path = None
for p in KEY_PATHS:
    if p.exists():
        key_path = str(p)
        break

if not key_path:
    print("ERROR: Could not find SSH key. Checked:")
    for p in KEY_PATHS:
        print(f"  - {p}")
    exit(1)

print(f"Using SSH key: {key_path}")
print(f"Connecting to {SFTP_HOST}:{SFTP_PORT}...")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

try:
    pkey = paramiko.RSAKey.from_private_key_file(key_path)
    client.connect(
        hostname=SFTP_HOST, port=SFTP_PORT,
        username=SFTP_USERNAME, pkey=pkey,
        look_for_keys=False, allow_agent=False, timeout=30)
    sftp = client.open_sftp()
    print("Connected!\n")

    root = f"/{SFTP_EXPORT_ID}"
    root_items = sorted(sftp.listdir(root))

    if not root_items:
        print("No date folders found!")
        exit(1)

    # Pick the latest date folder
    latest = root_items[-1]
    remote_dir = f"{root}/{latest}"

    # Local output directory (next to this script)
    out_dir = Path(__file__).parent / "all_exports" / latest
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading ALL files from: {remote_dir}")
    print(f"Saving to: {out_dir}\n")

    # List all files
    filenames = sorted(sftp.listdir(remote_dir))
    print(f"Found {len(filenames)} files:\n")

    downloaded = 0
    for fname in filenames:
        remote_path = f"{remote_dir}/{fname}"
        local_path = out_dir / fname
        try:
            sftp.get(remote_path, str(local_path))
            size_kb = local_path.stat().st_size / 1024
            print(f"  ✓ {fname:<45} ({size_kb:>8.1f} KB)")
            downloaded += 1
        except Exception as e:
            print(f"  ✗ {fname:<45} ERROR: {e}")

    sftp.close()
    client.close()

    print(f"\nDone! Downloaded {downloaded}/{len(filenames)} files to:")
    print(f"  {out_dir}")
    print(f"\nYou can now open the folder and inspect each CSV.")

except Exception as e:
    print(f"Connection error: {e}")
    try:
        client.close()
    except Exception:
        pass
