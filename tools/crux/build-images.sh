#!/usr/bin/env bash
# Build the three Crux U-Boot configurations and package their partition images.
#
#   tools/crux/build-images.sh OUT_DIR [TWRP_PROVEN_RECOVERY_IMG]
#
# Run from a clean, full checkout on Ubuntu 22.04 x86_64.
# Writes OUT_DIR/{xiaomi-crux,xiaomi-crux-boot,xiaomi-crux-recovery}/ build trees,
# OUT_DIR/symbols-*/, OUT_DIR/boot/boot.img (flash with ABL `fastboot flash boot`),
# OUT_DIR/recovery/recovery.img when the rescue twrp-proven image is given
# (`fastboot flash recovery`; otherwise only recovery-prefix.img), the host
# mkimage in OUT_DIR/xiaomi-crux/tools/ and OUT_DIR/SHA256SUMS. No device access.
set -euo pipefail

out=${1:?OUT_DIR}
twrp_proven=${2:-}
here=$(cd -- "$(dirname -- "$0")" && pwd -P)
src=$(pwd -P)
test "$(uname -sm)" = "Linux x86_64"
# shellcheck disable=SC1091
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:22.04
if [[ $(git rev-parse --show-toplevel) != "$src" ||
      $(git status --porcelain --untracked-files=no) ||
      $(git rev-parse --is-shallow-repository) != false ||
      $(git rev-parse --verify refs/tags/v2026.10-rc5) != e2d69a8dfecf34a178e049621c9e61c6c54b301c ]] ||
   ! git merge-base --is-ancestor 'v2026.10-rc5^{commit}' HEAD; then
    echo 'build-images: need a clean full checkout with the upstream v2026.10-rc5 tag' >&2
    exit 1
fi
# These untracked overrides would change the banner even with the correct tag.
shopt -s nullglob
version_overrides=(localversion*)
if [[ -e .scmversion ]] || (( ${#version_overrides[@]} )); then
    echo 'build-images: remove local SCM/version overrides from the build checkout' >&2
    exit 1
fi
test -e "$out" && { echo "build-images: $out exists" >&2; exit 1; }
if [[ -n $twrp_proven ]]; then
    twrp_proven=$(realpath "$twrp_proven")
fi
mkdir -p "$out"
out=$(cd -- "$out" && pwd -P)
jobs=$(getconf _NPROCESSORS_ONLN)
export LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export KBUILD_BUILD_USER=crux KBUILD_BUILD_HOST=build
unset LOCALVERSION

toolchain=$(mktemp -d)
trap 'rm -r -- "$toolchain"' EXIT
archive=x86_64-gcc-13.2.0-nolibc-aarch64-linux.tar.xz
curl -fsSL --retry 3 "https://cdn.kernel.org/pub/tools/crosstool/files/bin/x86_64/13.2.0/$archive" \
    -o "$toolchain/$archive"
printf '%s  %s\n' 6efd7ac6c40c9cc2edc871ec99ef27746cbf658265a3334e448d63ac711740c4 \
    "$toolchain/$archive" | sha256sum --check
tar -xf "$toolchain/$archive" -C "$toolchain"
compilers=("$toolchain"/*/aarch64-linux/bin/aarch64-linux-gcc)
test "${#compilers[@]}" -eq 1
cross=${compilers[0]%gcc}
"${cross}gcc" --version
"${cross}ld" --version

maps="-ffile-prefix-map=$src=/crux/source -ffile-prefix-map=$out=/crux/output"
build_args=(CROSS_COMPILE="$cross" KCFLAGS="$maps" KAFLAGS="$maps" HOSTCFLAGS="$maps"
            HOSTPKG_CONFIG=pkg-config)
# Avoid evaluating shell text derived from the cache path.
mapfile -t package_tools < <("$here/host-tools.sh")
test "${#package_tools[@]}" -eq 2
MKBOOTIMG=${package_tools[0]#MKBOOTIMG=}
AVBTOOL=${package_tools[1]#AVBTOOL=}

for config in xiaomi-crux xiaomi-crux-boot xiaomi-crux-recovery; do
    make O="$out/$config" "${build_args[@]}" "${config}_defconfig"
    make O="$out/$config" "${build_args[@]}" -j"$jobs"
    mkdir "$out/symbols-$config"
    cp "$out/$config"/{u-boot,u-boot.map,u-boot-nodtb.bin,u-boot.dtb,u-boot.bin} "$out/symbols-$config/"
    cp "$out/$config/.config" "$out/symbols-$config/config"
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
python3 "$here/check-public.py" "$out"
(
    cd "$out"
    find boot recovery symbols-* -type f | LC_ALL=C sort | while read -r file; do
        shasum -a 256 "$file" 2>/dev/null || sha256sum "$file"
    done
) > "$out/SHA256SUMS"
grep -E ' (boot/boot.img|recovery/recovery(-prefix)?.img)$' "$out/SHA256SUMS"
