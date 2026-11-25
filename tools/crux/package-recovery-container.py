#!/usr/bin/env python3
"""Package a Recovery-variant U-Boot build into the full 128 MiB physical Recovery container.

    package-recovery-container.py --uboot-nodtb BIN --uboot-dtb DTB --mkbootimg MKBOOTIMG.PY \
        --avbtool AVBTOOL.PY --output-dir DIR

The container is an Android boot image v1 (header size 1648, zero Recovery
DTBO, page 4096) whose kernel is gzip -9 u-boot-nodtb.bin (gzip OS byte 0xff)
with u-boot.dtb appended, the ramdisk/second address fields kept at the values
mkbootimg zeroes, zero padding, and a NONE vbmeta with a recovery hash
descriptor and AVB footer at the partition end (fixed zero salt, so the image
is reproducible). The firmware needs the v1 header and coherent AVB metadata in
Recovery (a v0 image drops to ABL fastboot); vbmeta flags 3 on the device
disable the digest check. Every byte comes from the U-Boot build and the two
pinned host tools.

Writes recovery.img (flash with ABL `fastboot flash recovery`),
recovery-prefix.img, uboot-body.bin and container.json. Host only.
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
RECOVERY_LBA = 0x28000
MKBOOTIMG_SHA256 = "021f2d960b2d3158a9df2ff6255f1140cebb09bc09f4886373438f2919559cd9"
AVBTOOL_SHA256 = "730cf5104ea09aac11e9532ac3081d50bddbb45dcfb414283b6e51be1c586a3e"
MKBOOTIMG_ARGS = ["--base", "0", "--kernel_offset", "0x8000", "--ramdisk_offset", "0x1000000",
                  "--second_offset", "0xf00000", "--tags_offset", "0x100", "--pagesize", "4096",
                  "--header_version", "1"]
# The v1 header fields of the accepted containers (2026-10-06 onwards); only the
# kernel size and the mkbootimg ID follow the U-Boot body.
EXPECTED_HEADER = {"magic": b"ANDROID!", "kernel_addr": 0x8000, "ramdisk_size": 0, "ramdisk_addr": 0x1000000,
                   "second_size": 0, "second_addr": 0xf00000, "tags_addr": 0x100, "page_size": BLOCK,
                   "header_version": 1, "os_version": 0, "name": bytes(16), "cmdline": bytes(512),
                   "extra_cmdline": bytes(1024), "recovery_dtbo_size": 0, "recovery_dtbo_offset": 0,
                   "header_size": 1648}


def require(condition, message):
    if not condition:
        raise SystemExit("package-recovery-container: " + message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def crc(data):
    return f"{zlib.crc32(data) & 0xffffffff:08x}"


def header(data):
    fields = struct.unpack_from("<8s10I16s", data)
    return {"magic": fields[0], "kernel_size": fields[1], "kernel_addr": fields[2],
            "ramdisk_size": fields[3], "ramdisk_addr": fields[4], "second_size": fields[5],
            "second_addr": fields[6], "tags_addr": fields[7], "page_size": fields[8],
            "header_version": fields[9], "os_version": fields[10], "name": fields[11],
            "cmdline": data[64:576], "id": data[576:608], "extra_cmdline": data[608:1632],
            "recovery_dtbo_size": struct.unpack_from("<I", data, 1632)[0],
            "recovery_dtbo_offset": struct.unpack_from("<Q", data, 1636)[0],
            "header_size": struct.unpack_from("<I", data, 1644)[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uboot-nodtb", type=Path, required=True)
    parser.add_argument("--uboot-dtb", type=Path, required=True)
    parser.add_argument("--mkbootimg", type=Path, required=True)
    parser.add_argument("--avbtool", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    require(sha256(args.mkbootimg.read_bytes()) == MKBOOTIMG_SHA256,
            "mkbootimg.py is not the pinned system_tools_mkbootimg@5d90c7eb")
    require(sha256(args.avbtool.read_bytes()) == AVBTOOL_SHA256, "avbtool.py is not the pinned external_avb@78fc9d38")
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    nodtb = args.uboot_nodtb.read_bytes()
    dtb = args.uboot_dtb.read_bytes()
    require(dtb[:4] == bytes.fromhex("d00dfeed"), "u-boot.dtb is not a DT")
    compressed = bytearray(gzip.compress(nodtb, compresslevel=9, mtime=0))
    compressed[9] = 0xff  # Preserve the accepted gzip OS byte across Python versions.
    body = bytes(compressed) + dtb
    (out / "uboot-body.bin").write_bytes(body)
    with tempfile.TemporaryDirectory() as directory:
        prefix_path = Path(directory) / "prefix.img"
        subprocess.run([sys.executable, str(args.mkbootimg), "--kernel", str(out / "uboot-body.bin"),
                        *MKBOOTIMG_ARGS, "--output", str(prefix_path)], check=True)
        prefix = bytearray(prefix_path.read_bytes())
    # mkbootimg zeroes the ramdisk/second addresses when their sizes are zero;
    # the accepted container (like the Boot image) keeps the offset values.
    struct.pack_into("<I", prefix, 20, 0x01000000)
    struct.pack_into("<I", prefix, 28, 0x00f00000)
    prefix = bytes(prefix)
    fields = header(prefix)
    for key, value in EXPECTED_HEADER.items():
        require(fields[key] == value, f"v1 header field {key} differs from the accepted container")
    require(fields["kernel_size"] == len(body) and len(prefix) % BLOCK == 0 and prefix[BLOCK:BLOCK + len(body)] == body,
            "prefix layout differs")
    (out / "recovery-prefix.img").write_bytes(prefix)
    image_path = out / "recovery.img"
    image_path.write_bytes(prefix)
    subprocess.run([sys.executable, str(args.avbtool), "add_hash_footer", "--image", str(image_path),
                    "--partition_size", str(PARTITION_BYTES), "--partition_name", "recovery",
                    "--algorithm", "NONE", "--salt", SALT], check=True)
    image = image_path.read_bytes()
    require(len(image) == PARTITION_BYTES and image[:len(prefix)] == prefix and image[-64:-60] == b"AVBf",
            "avbtool did not produce a full Recovery container")
    original, vbmeta_offset, vbmeta_size = struct.unpack_from(">QQQ", image, PARTITION_BYTES - 64 + 12)
    require(original == len(prefix) and not any(image[len(prefix):vbmeta_offset]),
            "container holds bytes other than the prefix, zero fill and AVB metadata")
    record = {"schema_version": 3, "recipe": "Recovery v1 header (rdtbo zero) + U-Boot + zero fill + NONE vbmeta/footer",
              "uboot_nodtb_sha256": sha256(nodtb), "uboot_dtb_sha256": sha256(dtb),
              "body_bytes": len(body), "body_sha256": sha256(body), "prefix_bytes": len(prefix),
              "prefix_sha256": sha256(prefix), "v1_id": fields["id"].hex(),
              "avb": {"original_image_size": original, "vbmeta_offset": hex(vbmeta_offset), "vbmeta_size": vbmeta_size,
                      "vbmeta_sha256": sha256(image[vbmeta_offset:vbmeta_offset + vbmeta_size]), "salt": SALT},
              "lun": 0, "partition_lba": hex(RECOVERY_LBA), "partition_blocks": hex(PARTITION_BYTES // BLOCK),
              "container_bytes": len(image), "container_sha256": sha256(image), "container_crc32": crc(image)}
    (out / "container.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
