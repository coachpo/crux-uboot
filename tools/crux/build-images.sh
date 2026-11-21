#!/usr/bin/env bash
# Build the three Crux U-Boot configurations and package their partition images.
#
#   tools/crux/build-images.sh OUT_DIR [TWRP_PROVEN_RECOVERY_IMG]
#
# Run from the repository root with CROSS_COMPILE set (Mac: aarch64-unknown-linux-gnu-,
# kernel.org toolchain: aarch64-linux-). MAKE defaults to gmake when present.
# Writes OUT_DIR/{xiaomi-crux,xiaomi-crux-boot,xiaomi-crux-recovery}/ build trees,
# OUT_DIR/symbols-*/, OUT_DIR/boot/boot.img (flash with ABL `fastboot flash boot`),
# OUT_DIR/recovery/recovery.img when the rescue twrp-proven image is given
# (`fastboot flash recovery`; otherwise only recovery-prefix.img), the host
# mkimage in OUT_DIR/xiaomi-crux/tools/ and OUT_DIR/SHA256SUMS. No device access.
set -euo pipefail

out=${1:?OUT_DIR}
twrp_proven=${2:-}
: "${CROSS_COMPILE:?set CROSS_COMPILE}"
make=${MAKE:-$(command -v gmake || command -v make)}
here=$(cd -- "$(dirname -- "$0")" && pwd -P)
test -e "$out" && { echo "build-images: $out exists" >&2; exit 1; }
mkdir -p "$out"
out=$(cd -- "$out" && pwd -P)
jobs=$(getconf _NPROCESSORS_ONLN)
# A reproducible U-Boot timestamp needs GNU date (Linux, or gdate on macOS).
if [[ -z ${SOURCE_DATE_EPOCH:-} ]] && { date --version >/dev/null 2>&1 || command -v gdate >/dev/null; }; then
    export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
fi
eval "$("$here/host-tools.sh")"

for config in xiaomi-crux xiaomi-crux-boot xiaomi-crux-recovery; do
    "$make" O="$out/$config" CROSS_COMPILE="$CROSS_COMPILE" "${config}_defconfig"
    "$make" O="$out/$config" CROSS_COMPILE="$CROSS_COMPILE" -j"$jobs"
    mkdir "$out/symbols-$config"
    cp "$out/$config"/{u-boot,u-boot.map,u-boot-nodtb.bin,u-boot.dtb,u-boot.bin,.config} "$out/symbols-$config/"
done
python3 "$here/check-env.py" board/qualcomm/xiaomi-crux.env "$out/xiaomi-crux/u-boot-nodtb.bin" -
python3 "$here/check-env.py" board/qualcomm/xiaomi-crux-boot.env "$out/xiaomi-crux-boot/u-boot-nodtb.bin" boot-crux-identity-1
python3 "$here/check-env.py" board/qualcomm/xiaomi-crux-recovery.env "$out/xiaomi-crux-recovery/u-boot-nodtb.bin" \
    recovery-container-crux-install-1

python3 "$here/build-boot-image.py" --mkbootimg "$MKBOOTIMG" --avbtool "$AVBTOOL" \
    --uboot-nodtb "$out/symbols-xiaomi-crux-boot/u-boot-nodtb.bin" \
    --uboot-dtb "$out/symbols-xiaomi-crux-boot/u-boot.dtb" --out "$out/boot" > /dev/null
if [[ -n $twrp_proven ]]; then
    source_args=(--twrp-proven "$twrp_proven")
else
    source_args=(--prefix-only)
fi
python3 "$here/package-recovery-container.py" --mkbootimg "$MKBOOTIMG" "${source_args[@]}" \
    --uboot-nodtb "$out/symbols-xiaomi-crux-recovery/u-boot-nodtb.bin" \
    --uboot-dtb "$out/symbols-xiaomi-crux-recovery/u-boot.dtb" --output-dir "$out/recovery" > /dev/null
(
    cd "$out"
    find boot recovery symbols-* -type f | LC_ALL=C sort | while read -r file; do
        shasum -a 256 "$file" 2>/dev/null || sha256sum "$file"
    done
) > "$out/SHA256SUMS"
grep -E ' (boot/boot.img|recovery/recovery(-prefix)?.img)$' "$out/SHA256SUMS"
