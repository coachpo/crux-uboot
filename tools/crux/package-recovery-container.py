#!/usr/bin/env python3
"""Package a Recovery-variant U-Boot body into the physical Recovery v1 container.

    package-recovery-container.py --uboot-nodtb BIN --uboot-dtb DTB --mkbootimg MKBOOTIMG.PY \
        (--twrp-proven RECOVERY.IMG | --prefix-only) --output-dir DIR

The container is an Android boot image v1 (header size 1648, zero Recovery
DTBO, legacy DT-zero ID) whose kernel is gzip -9 u-boot-nodtb.bin (gzip OS byte
0xff) with u-boot.dtb appended. It is spliced over the start of the accepted
128 MiB base container (aa5dfe75...); its AVB NONE vbmeta and footer stay and
only the hash descriptor digest is recomputed over the same image size.

The base is derived, not stored: the OmniROM TWRP 3.3.1 image kept with the
rescue material (twrp-proven, b6eb388d...) with recovery-v1-base-prefix.raw
(the 2026-10-06 v1 header and U-Boot, 688,128 B) over its start and the AVB
digest recomputed. --prefix-only (for CI, which has no twrp-proven) writes the
U-Boot body and the v1 prefix only; the digest block then needs the local step.

Outputs recovery.img (flash with ABL `fastboot flash recovery`), the patch and
rollback files for every changed 4096-byte block range, and container.json.
Host only.
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

HERE = Path(__file__).resolve().parent
BASE_PREFIX = HERE / "recovery-v1-base-prefix.raw"
BASE_PREFIX_SHA256 = "4dc9e0aef4d2c269ebbb24e48b977f587ee9cb3704820ea8143a3364fdeb51f6"
TWRP_PROVEN_SHA256 = "b6eb388daac1f6b2ed6df77f9a34bdbe15155d70e752573bab41b94520748f8b"
BASE_SHA256 = "aa5dfe7599c587683fe861a04b30f1cd25dc838a4184be9d76530f28e45fbca5"
MKBOOTIMG_SHA256 = "021f2d960b2d3158a9df2ff6255f1140cebb09bc09f4886373438f2919559cd9"
RECOVERY_LBA = 0x28000
PARTITION_BYTES = 128 << 20
BLOCK = 4096
MKBOOTIMG_ARGS = ["--base", "0", "--kernel_offset", "0x8000", "--ramdisk_offset", "0x1000000",
                  "--second_offset", "0xf00000", "--tags_offset", "0x100", "--pagesize", "4096",
                  "--header_version", "1"]
HEADER_FIELDS = ("magic", "kernel_addr", "ramdisk_size", "ramdisk_addr", "second_size", "second_addr",
                 "tags_addr", "page_size", "header_version", "os_version", "name", "cmdline",
                 "extra_cmdline", "recovery_dtbo_size", "recovery_dtbo_offset", "header_size")


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


def avb_hash_descriptor(image):
    """Locate the single hash descriptor of the NONE vbmeta named by the footer."""
    footer = image[-64:]
    require(footer[:4] == b"AVBf", "no AVB footer")
    original, vbmeta_offset, vbmeta_size = struct.unpack_from(">QQQ", footer, 12)
    vbmeta = image[vbmeta_offset:vbmeta_offset + vbmeta_size]
    require(vbmeta[:4] == b"AVB0", "no vbmeta at the footer offset")
    auth_size, aux_size, algorithm = struct.unpack_from(">QQI", vbmeta, 12)
    descriptors_offset, descriptors_size = struct.unpack_from(">QQ", vbmeta, 96)
    require(algorithm == 0 and auth_size == 0, "vbmeta is not the NONE schema")
    position = vbmeta_offset + 256 + auth_size + descriptors_offset
    end = position + descriptors_size
    found = []
    while position < end:
        tag, following = struct.unpack_from(">QQ", image, position)
        if tag == 2:
            image_size = struct.unpack_from(">Q", image, position + 16)[0]
            algo = image[position + 24:position + 56].rstrip(b"\0")
            name_len, salt_len, digest_len = struct.unpack_from(">III", image, position + 56)
            name_at = position + 132
            salt_at = name_at + name_len
            digest_at = salt_at + salt_len
            found.append({"image_size": image_size, "algorithm": algo.decode(),
                          "name": image[name_at:salt_at].decode(), "salt": image[salt_at:digest_at],
                          "digest_offset": digest_at, "digest_len": digest_len})
        position += 16 + following
    require(len(found) == 1 and found[0]["algorithm"] == "sha256" and found[0]["digest_len"] == 32 and
            found[0]["name"] == "recovery" and found[0]["image_size"] == original,
            "unexpected AVB hash descriptor set")
    return found[0]


def splice(base, prefix):
    """Write prefix over base and recompute the AVB hash descriptor digest."""
    descriptor = avb_hash_descriptor(base)
    require(len(prefix) < descriptor["image_size"], "prefix reaches the hashed image end")
    image = bytearray(base)
    image[:len(prefix)] = prefix
    digest = hashlib.sha256(descriptor["salt"] + bytes(image[:descriptor["image_size"]])).digest()
    image[descriptor["digest_offset"]:descriptor["digest_offset"] + 32] = digest
    return bytes(image), descriptor, digest


def derive_base(twrp_proven):
    proven = twrp_proven.read_bytes()
    require(len(proven) == PARTITION_BYTES and sha256(proven) == TWRP_PROVEN_SHA256, "twrp-proven image differs")
    prefix = BASE_PREFIX.read_bytes()
    require(sha256(prefix) == BASE_PREFIX_SHA256, "recovery-v1-base-prefix.raw differs")
    base = splice(proven, prefix)[0]
    require(sha256(base) == BASE_SHA256, "derived base container differs from aa5dfe75")
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uboot-nodtb", type=Path, required=True)
    parser.add_argument("--uboot-dtb", type=Path, required=True)
    parser.add_argument("--mkbootimg", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--twrp-proven", type=Path, help="rescue twrp-proven recovery.img (b6eb388d...)")
    source.add_argument("--prefix-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    require(sha256(args.mkbootimg.read_bytes()) == MKBOOTIMG_SHA256, "mkbootimg.py is not the pinned one")
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    nodtb = args.uboot_nodtb.read_bytes()
    dtb = args.uboot_dtb.read_bytes()
    require(dtb[:4] == bytes.fromhex("d00dfeed"), "u-boot.dtb is not a DT")
    body = gzip.compress(nodtb, compresslevel=9, mtime=0) + dtb
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
    old, new = header(BASE_PREFIX.read_bytes()), header(prefix)
    require(new["header_version"] == 1 and new["header_size"] == 1648 and new["page_size"] == BLOCK and
            new["recovery_dtbo_size"] == 0 and new["recovery_dtbo_offset"] == 0 and
            new["kernel_size"] == len(body), "unexpected new v1 header")
    for key in HEADER_FIELDS:
        require(old[key] == new[key], f"v1 header field {key} differs from the accepted container")
    require(len(prefix) % BLOCK == 0 and prefix[BLOCK:BLOCK + len(body)] == body, "prefix layout differs")
    record = {"schema_version": 2, "recipe": "physical Recovery v1 container, header-v1-rdtbo-zero",
              "uboot_nodtb_sha256": sha256(nodtb), "uboot_dtb_sha256": sha256(dtb),
              "body_bytes": len(body), "body_sha256": sha256(body), "prefix_bytes": len(prefix),
              "prefix_sha256": sha256(prefix), "v1_id": new["id"].hex(), "base_sha256": BASE_SHA256,
              "lun": 0, "partition_lba": hex(RECOVERY_LBA), "partition_blocks": hex(PARTITION_BYTES // BLOCK)}
    if args.prefix_only:
        (out / "recovery-prefix.img").write_bytes(prefix)
        record["complete"] = False
        (out / "container.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record, indent=2))
        return
    base = derive_base(args.twrp_proven)
    image, descriptor, digest = splice(base, prefix)
    (out / "recovery.img").write_bytes(image)
    changed = [index for index in range(PARTITION_BYTES // BLOCK)
               if image[index * BLOCK:(index + 1) * BLOCK] != base[index * BLOCK:(index + 1) * BLOCK]]
    ranges = []
    for index in changed:
        if ranges and ranges[-1][1] == index:
            ranges[-1][1] = index + 1
        else:
            ranges.append([index, index + 1])
    patches = []
    for start, end in ranges:
        data = image[start * BLOCK:end * BLOCK]
        name = f"patch-{start:04x}-{end - start:x}.bin"
        rollback = f"rollback-{start:04x}-{end - start:x}.bin"
        (out / name).write_bytes(data)
        (out / rollback).write_bytes(base[start * BLOCK:end * BLOCK])
        patches.append({"relative_block": hex(start), "blocks": hex(end - start),
                        "lba": hex(RECOVERY_LBA + start), "patch": name, "patch_crc32": crc(data),
                        "patch_sha256": sha256(data), "rollback": rollback,
                        "rollback_crc32": crc(base[start * BLOCK:end * BLOCK])})
    vbmeta_offset = struct.unpack_from(">Q", image[-64:], 20)[0]
    record.update({"complete": True, "avb_image_size": descriptor["image_size"], "avb_salt": descriptor["salt"].hex(),
                   "avb_digest": digest.hex(), "vbmeta_sha256": sha256(image[vbmeta_offset:vbmeta_offset + 512]),
                   "container_bytes": len(image), "container_sha256": sha256(image), "container_crc32": crc(image),
                   "changed_ranges": patches})
    (out / "container.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
