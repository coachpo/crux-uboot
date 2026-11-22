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

macOS (Apple Silicon, Homebrew `make`, `aarch64-unknown-linux-gnu` and `openssl@3`). OpenSSL 4
dropped the ENGINE API the host tools use, so `openssl@3` must come first when Homebrew
also links `openssl@4` (`tools/Makefile` asks `pkg-config` for the flags):

```sh
export PATH="/opt/homebrew/bin:$PATH"
export PKG_CONFIG_PATH="/opt/homebrew/opt/openssl@3/lib/pkgconfig"
export CPATH="/opt/homebrew/opt/openssl@3/include:/opt/homebrew/include"
export LIBRARY_PATH="/opt/homebrew/opt/openssl@3/lib:/opt/homebrew/lib"
CROSS_COMPILE=aarch64-unknown-linux-gnu- tools/crux/build-images.sh .output-crux ~/Crux-Rescue/twrp-proven/recovery.img
```

Linux: `CROSS_COMPILE=aarch64-linux-gnu- tools/crux/build-images.sh OUT [TWRP_PROVEN_IMG]`.

`build-images.sh` builds every configuration into `OUT/<config>/`, copies the
symbols to `OUT/symbols-<config>/`, checks with `check-env.py` that every
environment line is compiled in full (the Mac text-environment path has
truncated values before) and that each variant names its role, then packages
both images and writes `OUT/SHA256SUMS`. Without the twrp-proven image it
writes only `recovery/recovery-prefix.img`. With GNU `date` it sets
`SOURCE_DATE_EPOCH` to the HEAD commit time, so Linux builds are reproducible.

The workflow `.github/workflows/crux-uboot.yml` runs the same script on a
standard GitHub runner (workflow artifact; a prerelease only with a tag input).

A new build is a new binary. Before flashing, chainload its `u-boot.bin` from
the running Boot U-Boot over U-Boot fastboot into RAM and check version,
environment and role, then flash with ABL and check the boot menu.
