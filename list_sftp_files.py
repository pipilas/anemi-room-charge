#!/usr/bin/env python3
"""
List all files available on the Toast SFTP server.
Run this on your computer where paramiko is installed:
    python3 list_sftp_files.py
"""

import paramiko
from pathlib import Path

# ── SFTP Config (same as main app) ──────────────────────────────────────────
SFTP_HOST     = "s-9b0f88558b264dfda.server.transfer.us-east-1.amazonaws.com"
SFTP_PORT     = 22
SFTP_USERNAME = "MamakaMeditteraneanDataExports"
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

    # List root level
    root = f"/{SFTP_EXPORT_ID}"
    print(f"=== Root directory: {root} ===")
    try:
        root_items = sorted(sftp.listdir(root))
        print(f"Found {len(root_items)} date folders:")
        for item in root_items[-10:]:  # Show last 10 folders
            print(f"  📁 {item}")
        if len(root_items) > 10:
            print(f"  ... and {len(root_items) - 10} more older folders")
    except Exception as e:
        print(f"Error listing root: {e}")

    # List files in the most recent date folder
    if root_items:
        latest = root_items[-1]
        latest_path = f"{root}/{latest}"
        print(f"\n=== Files in latest folder: {latest_path} ===")
        try:
            files = sorted(sftp.listdir_attr(latest_path), key=lambda x: x.filename)
            print(f"Found {len(files)} files:\n")
            for f in files:
                size_kb = f.st_size / 1024 if f.st_size else 0
                print(f"  📄 {f.filename:<45} ({size_kb:>8.1f} KB)")
        except Exception as e:
            print(f"Error listing folder: {e}")

        # Also check one older folder in case some files appear only on certain days
        if len(root_items) > 3:
            older = root_items[-4]
            older_path = f"{root}/{older}"
            print(f"\n=== Files in older folder: {older_path} ===")
            try:
                files = sorted(sftp.listdir_attr(older_path), key=lambda x: x.filename)
                print(f"Found {len(files)} files:\n")
                for f in files:
                    size_kb = f.st_size / 1024 if f.st_size else 0
                    print(f"  📄 {f.filename:<45} ({size_kb:>8.1f} KB)")
            except Exception as e:
                print(f"Error listing folder: {e}")

    sftp.close()
    client.close()
    print("\nDone!")

except Exception as e:
    print(f"Connection error: {e}")
    client.close()
