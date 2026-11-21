#!/usr/bin/env python3
"""Check that every key=value line of a text environment is compiled in full.

    check-env.py ENV_FILE U-BOOT-NODTB.BIN EXPECTED_ROLE

The Mac text-environment build has produced truncated values; the default
environment is stored as NUL-terminated key=value strings, so each line must
appear verbatim followed by a NUL, and crux_uboot_role must be the expected one.
"""
import sys
from pathlib import Path

env, binary, role = Path(sys.argv[1]), Path(sys.argv[2]).read_bytes(), sys.argv[3]
lines = [line for line in env.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
missing = [line.split("=", 1)[0] for line in lines if line.encode() + b"\0" not in binary]
roles = [line.split("=", 1)[1] for line in lines if line.startswith("crux_uboot_role=")]
if missing or roles != ([role] if role != "-" else []):
    raise SystemExit(f"check-env: {env}: missing or truncated {missing}, role {roles} (expected {role})")
print(f"check-env: {env}: {len(lines)} values complete, role {roles[0] if roles else '-'}")
