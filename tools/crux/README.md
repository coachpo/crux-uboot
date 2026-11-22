# Crux partition images

Host tools that turn the three Crux defconfigs into the images ABL flashes.
They never access a device.

| Image | Configuration | Recipe | Flash with ABL fastboot |
|---|---|---|---|
| `boot/boot.img` (128 MiB) | `xiaomi-crux-boot_defconfig` | `build-boot-image.py`: Android boot image v0 header, `gzip -9 -n` U-Boot (OS byte 3) + control DT, zero fill, NONE vbmeta and AVB footer (zero salt) | `fastboot flash boot boot.img` |
| `recovery/recovery.img` (128 MiB) | `xiaomi-crux-recovery_defconfig` | `package-recovery-container.py`: boot image v1 header (size 1648, zero Recovery DTBO), `gzip -9` U-Boot (OS byte 0xff) + control DT spliced over the derived base container `aa5dfe75…`; only the AVB hash digest is recomputed | `fastboot flash recovery recovery.img` |
| `xiaomi-crux/tools/mkimage` | `xiaomi-crux_defconfig` | host tool for FIT packaging | — |

The Recovery container base is derived from the rescue OmniROM TWRP 3.3.1 image
(`twrp-proven`, `b6eb388d…`) with `recovery-v1-base-prefix.raw` (the
2026-10-06 v1 header and U-Boot, `4dc9e0ae…`) over its start. The firmware
needs this v1 header and coherent AVB metadata: a v0 image in Recovery drops
to ABL fastboot. Flashing the Recovery container replaces whatever Recovery
holds; the signed TWRP ZIP install path needs this container
(`crux_uboot_role=recovery-container-crux-install-1`).

## Build

From the repository root. `host-tools.sh` fetches the pinned
`PixelExperience/system_tools_mkbootimg@5d90c7eb` and
`PixelExperience/external_avb@78fc9d38` (the commits the PE13 source lock pins)
into `~/.cache/crux-host-tools`; the recipes check their SHA-256.

Build the flashable candidates on Ubuntu 22.04 x86_64, either in OrbStack
`cruxbuild` or on the standard GitHub runner. Install the same Ubuntu host
packages listed in [the workflow](../../.github/workflows/crux-uboot.yml).
Use a full checkout of the intended commit; ensure it has the original
annotated upstream tag:

```sh
git fetch --no-tags https://github.com/u-boot/u-boot.git tag v2026.10-rc5
tools/crux/build-images.sh OUT
# To also assemble the full physical Recovery container:
tools/crux/build-images.sh OUT_WITH_RESCUE /path/to/twrp-proven/recovery.img
```

From the Mac, use `orb -m cruxbuild bash` and the commands above in the
VM-visible checkout. Pass the VM-visible path to
`~/Crux-Rescue/twrp-proven/recovery.img` only for the full container.
The resulting `mkimage` is a Linux executable. A previously built native
Mac `mkimage` used by PE packaging is separate; do not replace it with
this binary.

`build-images.sh` builds every configuration into `OUT/<config>/`, copies the
symbols to `OUT/symbols-<config>/`, checks with `check-env.py` that every
environment line is compiled in full (the Mac text-environment path has
truncated values before) and that each variant names its role, then packages
both images and writes `OUT/SHA256SUMS`. It always emits
`recovery/recovery-prefix.img` and `recovery/uboot-body.bin`; those two
files do not use the rescue base. Only the local full-container build adds
`recovery/recovery.img` and the splice/rollback blocks. Do not publish them.

The workflow `.github/workflows/crux-uboot.yml` runs the same script on a
standard GitHub runner (workflow artifact; a prerelease only with a tag input).
Both paths check the same kernel.org GCC 13.2.0/binutils archive by SHA-256,
verify the version tag, fix the UTC timestamp to the HEAD commit time, and
map source and output prefixes in the binaries and symbols. Ubuntu host
packages (including Python/zlib for packaging) still receive distro updates.
The build scans the raw payloads, symbols and public image files for host
paths and ABL identity before upload.

For a matching-commit comparison, run `cmp` on the two
`symbols-xiaomi-crux-{boot,recovery}/u-boot.bin` files, `boot/boot.img`,
`recovery/recovery-prefix.img` and `recovery/uboot-body.bin`. Neither an
identical build nor this comparison upgrades any deployed image.

A new build is a new binary. Before flashing, chainload its `u-boot.bin` from
the running Boot U-Boot over U-Boot fastboot into RAM and check version,
environment and role, then flash with ABL and check the boot menu.
