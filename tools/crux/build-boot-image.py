#!/usr/bin/env python3
"""Make the full 128 MiB Boot partition image for a Boot-variant U-Boot build.

    build-boot-image.py --uboot-nodtb BIN --uboot-dtb DTB --mkbootimg MKBOOTIMG.PY \
        --avbtool AVBTOOL.PY --out DIR

An Android boot image v0 header (page 4096) whose kernel is `gzip -9 -n` of
u-boot-nodtb.bin (gzip OS byte 3) with u-boot.dtb appended, the ramdisk/second
address fields kept at the values mkbootimg zeroes, zero padding, and a NONE
vbmeta with a boot hash descriptor and AVB footer at the partition end (fixed
zero salt, so the image is reproducible). ABL reads the footer; vbmeta flags 3
on the device disable the digest check. Writes boot.img, boot-prefix.img and
boot-image.json. Flash boot.img with ABL `fastboot flash boot`.
"""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

PARTITION_BYTES = 128 << 20
BLOCK = 4096
SALT = "00" * 32
MKBOOTIMG_SHA256 = "021f2d960b2d3158a9df2ff6255f1140cebb09bc09f4886373438f2919559cd9"
AVBTOOL_SHA256 = "730cf5104ea09aac11e9532ac3081d50bddbb45dcfb414283b6e51be1c586a3e"
ARGS = ["--base", "0", "--kernel_offset", "0x8000", "--ramdisk_offset", "0x1000000",
        "--second_offset", "0xf00000", "--tags_offset", "0x100", "--pagesize", "4096", "--header_version", "0"]
EXPECTED_HEADER = {"kernel_addr": 0x8000, "ramdisk_size": 0, "ramdisk_addr": 0x1000000, "second_size": 0,
                   "second_addr": 0xf00000, "tags_addr": 0x100, "page_size": BLOCK, "header_version": 0,
                   "os_version": 0}
FIELDS = ("kernel_size", "kernel_addr", "ramdisk_size", "ramdisk_addr", "second_size", "second_addr",
          "tags_addr", "page_size", "header_version", "os_version")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def crc(data):
    return f"{zlib.crc32(data) & 0xffffffff:08x}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uboot-nodtb", type=Path, required=True)
    parser.add_argument("--uboot-dtb", type=Path, required=True)
    parser.add_argument("--mkbootimg", type=Path, required=True)
    parser.add_argument("--avbtool", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if sha(args.mkbootimg.read_bytes()) != MKBOOTIMG_SHA256:
        raise SystemExit("build-boot-image: mkbootimg.py is not the pinned system_tools_mkbootimg@5d90c7eb")
    if sha(args.avbtool.read_bytes()) != AVBTOOL_SHA256:
        raise SystemExit("build-boot-image: avbtool.py is not the pinned external_avb@78fc9d38")
    dtb = args.uboot_dtb.read_bytes()
    if dtb[:4] != bytes.fromhex("d00dfeed"):
        raise SystemExit("build-boot-image: u-boot.dtb is not a DT")
    packed = bytearray(gzip.compress(args.uboot_nodtb.read_bytes(), compresslevel=9, mtime=0))
    packed[9] = 3
    body = bytes(packed) + dtb
    args.out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as directory:
        kernel, image = Path(directory) / "body.bin", Path(directory) / "prefix.img"
        kernel.write_bytes(body)
        subprocess.run([sys.executable, str(args.mkbootimg), "--kernel", str(kernel), *ARGS, "--output", str(image)],
                       check=True)
        prefix = bytearray(image.read_bytes())
    struct.pack_into("<I", prefix, 20, 0x01000000)
    struct.pack_into("<I", prefix, 28, 0x00f00000)
    prefix = bytes(prefix)
    values = struct.unpack_from("<8s10I", prefix)
    header = dict(zip(FIELDS, values[1:]))
    if (values[0] != b"ANDROID!" or any(header[key] != value for key, value in EXPECTED_HEADER.items())
            or header["kernel_size"] != len(body) or len(prefix) % BLOCK or prefix[BLOCK:BLOCK + len(body)] != body):
        raise SystemExit(f"build-boot-image: unexpected header or layout {header}")
    (args.out / "boot-prefix.img").write_bytes(prefix)
    full = args.out / "boot.img"
    full.write_bytes(prefix)
    subprocess.run([sys.executable, str(args.avbtool), "add_hash_footer", "--image", str(full),
                    "--partition_size", str(PARTITION_BYTES), "--partition_name", "boot",
                    "--algorithm", "NONE", "--salt", SALT], check=True)
    data = full.read_bytes()
    if len(data) != PARTITION_BYTES or data[:len(prefix)] != prefix or data[-64:-60] != b"AVBf":
        raise SystemExit("build-boot-image: avbtool did not produce a full Boot image")
    original, vbmeta_offset, vbmeta_size = struct.unpack_from(">QQQ", data, PARTITION_BYTES - 64 + 12)
    record = {"schema_version": 2, "recipe": "Boot v0 header + U-Boot + zero fill + NONE vbmeta/footer",
              "header": header, "body_bytes": len(body), "body_sha256": sha(body),
              "prefix_blocks": hex(len(prefix) // BLOCK), "prefix_sha256": sha(prefix), "prefix_crc32": crc(prefix),
              "avb": {"original_image_size": original, "vbmeta_offset": hex(vbmeta_offset),
                      "vbmeta_size": vbmeta_size, "vbmeta_sha256": sha(data[vbmeta_offset:vbmeta_offset + vbmeta_size]),
                      "salt": SALT},
              "image_bytes": len(data), "image_sha256": sha(data), "image_crc32": crc(data)}
    (args.out / "boot-image.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
