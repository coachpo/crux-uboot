# Crux partition images

Host tools that turn the three Crux defconfigs into the images ABL flashes.
They never access a device.

| Image | Configuration | Recipe | Flash with ABL fastboot |
|---|---|---|---|
| `boot/boot.img` (128 MiB) | `xiaomi-crux-boot_defconfig` | `build-boot-image.py`: Android boot image v0 header, `gzip -9 -n` U-Boot (OS byte 3) + control DT, zero fill, NONE vbmeta and AVB footer (zero salt) | `fastboot flash boot boot.img` |
| `recovery/recovery.img` (128 MiB) | `xiaomi-crux-recovery_defconfig` | `package-recovery-container.py`: boot image v1 header (size 1648, zero Recovery DTBO), `gzip -9` U-Boot (OS byte 0xff) + control DT, zero fill, NONE vbmeta and AVB footer (zero salt) | `fastboot flash recovery recovery.img` |
| `xiaomi-crux/tools/mkimage` | `xiaomi-crux_defconfig` | host tool for FIT packaging | — |

Both images come only from the build and the pinned host tools. The Recovery
container keeps the v1 header fields of the accepted containers (checked by the
recipe): the firmware needs this v1 header and coherent AVB metadata in
Recovery, and a v0 image there drops to ABL fastboot. Containers before
2026-11-25 were spliced over the rescue OmniROM TWRP 3.3.1 partition image;
this recipe no longer reads it. Flashing the Recovery container replaces whatever Recovery
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
```

From the Mac, use `orb -m cruxbuild bash` and the commands above in the
VM-visible checkout. The resulting `mkimage` is a Linux executable. PE path B
packages on the Mac with a Mach-O `mkimage` built from this checkout with the
tools-only configuration, as the
[Mac packaging tools](https://github.com/coachpo/crux-pe13-distributed/blob/main/local/README.md#mac-packaging-tools)
describe; do not replace it with this binary.

`build-images.sh` builds every configuration into `OUT/<config>/`, copies the
symbols to `OUT/symbols-<config>/`, checks with `check-env.py` that every
environment line is compiled in full (the Mac text-environment path has
truncated values before) and that each variant names its role, then packages
both images (with `recovery/recovery-prefix.img` and `recovery/uboot-body.bin`)
and writes `OUT/SHA256SUMS`.

The workflow `.github/workflows/crux-uboot.yml` runs the same script on a
standard GitHub runner (workflow artifact; a prerelease only with a tag input).
Both paths check the same kernel.org GCC 13.2.0/binutils archive by SHA-256,
verify the version tag, fix the UTC timestamp to the HEAD commit time, and
map source and output prefixes in the binaries and symbols. Ubuntu host
packages (including Python/zlib for packaging) still receive distro updates.
The build scans the raw payloads, symbols and public image files for host
paths and ABL identity before upload.

For a matching-commit comparison, run `cmp` on the two
`symbols-xiaomi-crux-{boot,recovery}/u-boot.bin` files, `boot/boot.img` and
`recovery/recovery.img`. Neither an
identical build nor this comparison upgrades any deployed image.

A new build is a new binary. Before flashing, chainload its `u-boot.bin` from
the running Boot U-Boot over U-Boot fastboot into RAM and check version,
environment and role, then flash with ABL and check the boot menu.
