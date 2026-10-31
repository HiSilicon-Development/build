#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Run explicitly once on the build host. Never called by build.sh.
set -Eeuo pipefail
if (($# == 1)) && [[ $1 == -h || $1 == --help ]]; then
    printf '%s\n' 'Usage: scripts/install-deps.sh [--help]' \
        'Installs kernel and FFmpeg cross-build tools on a Debian/Ubuntu amd64 host.' \
        'This script does not install anything on the target device.'
    printf '%s\n' 'Use Debian 12 to keep FFmpeg compatible with Debian 12 and 13.'
    exit 0
fi
if (($# != 0)); then
    printf 'Usage: scripts/install-deps.sh [--help]\n' >&2
    exit 2
fi
if [[ $(dpkg --print-architecture) != amd64 ]]; then
    printf 'This dependency recipe is for a Debian/Ubuntu amd64 build host.\n' >&2
    exit 2
fi
ROOT=()
if ((EUID != 0)); then ROOT=(sudo); fi
"${ROOT[@]}" dpkg --add-architecture arm64
PACKAGES=(build-essential git python3 pkg-config bc bison flex rsync perl \
    libssl-dev:amd64 libelf-dev:amd64 clang llvm lld \
    gcc-aarch64-linux-gnu g++-aarch64-linux-gnu binutils-aarch64-linux-gnu \
    device-tree-compiler u-boot-tools kmod \
    libc6-dev:arm64 linux-libc-dev:arm64 libdrm-dev:arm64 libudev-dev:arm64)
"${ROOT[@]}" apt-get update
"${ROOT[@]}" apt-get install --no-install-recommends "${PACKAGES[@]}"
printf '\nBuild dependencies installed on the host. Run ./build.sh --target all --boards all\n'
