#!/usr/bin/env bash
# Fetch the pinned host packaging tools into a cache and print their paths.
#
#   tools/crux/host-tools.sh [CACHE_DIR]   (default ${CRUX_HOST_TOOLS:-$HOME/.cache/crux-host-tools})
#
# mkbootimg and avbtool are the same commits the PE13 source lock pins
# (system/tools/mkbootimg, external/avb); the recipes check their SHA-256.
set -euo pipefail

cache=${1:-${CRUX_HOST_TOOLS:-$HOME/.cache/crux-host-tools}}
mkdir -p "$cache"
fetch() {
    local name=$1 repository=$2 revision=$3
    if [[ ! -d $cache/$name/.git ]]; then
        git init -q "$cache/$name"
    fi
    if [[ $(git -C "$cache/$name" rev-parse -q --verify HEAD || true) != "$revision" ]]; then
        git -C "$cache/$name" fetch -q --depth 1 "https://github.com/$repository" "$revision"
        git -C "$cache/$name" checkout -q --detach FETCH_HEAD
    fi
    test "$(git -C "$cache/$name" rev-parse HEAD)" = "$revision"
}
fetch mkbootimg PixelExperience/system_tools_mkbootimg 5d90c7eb1bd50994e5b9143c7ad627b90b189c47
fetch avb PixelExperience/external_avb 78fc9d3835df730f4f4f3f4a09bc15552a7ccab4
printf 'MKBOOTIMG=%s\nAVBTOOL=%s\n' "$cache/mkbootimg/mkbootimg.py" "$cache/avb/avbtool.py"
