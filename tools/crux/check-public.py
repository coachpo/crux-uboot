#!/usr/bin/env python3
"""Scan the U-Boot candidates and public assets, not the private rescue base."""
import argparse
from pathlib import Path
import re

HOST_PATHS = (b"/home/", b"/Users/", b"/mnt/mac/", b"/__w/", b"/github/workspace")
IDENTITY = re.compile(
    rb"(?:androidboot\.(?:serialno|cpuid|oled_wp|hwversion|hwlevel|hwc|ufsid|dp|baseband)"
    rb"|msm_drm\.dsi_display0)=[^\s\x00]"
)
HOST_EMAIL = re.compile(rb"[\w.+-]+@[\w.-]+\.local\b", re.IGNORECASE)


def scan(out):
    # Symbols include the uncompressed U-Boot payload/DTs in the two images.
    files = [p for group in sorted(out.glob("symbols-*")) for p in sorted(group.iterdir())]
    files += sorted((out / "boot").iterdir())
    files += [out / "recovery" / name
              for name in ("recovery-prefix.img", "uboot-body.bin", "container.json")]
    for path in files:
        data = path.read_bytes()
        if any(prefix in data for prefix in HOST_PATHS) or HOST_EMAIL.search(data):
            raise SystemExit(f"Host path or local email in {path.relative_to(out)}")
        if IDENTITY.search(data):
            raise SystemExit(f"ABL identity assignment in {path.relative_to(out)}")
    print(f"Public U-Boot scan: {len(files)} files, 0 findings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    scan(parser.parse_args().out)
