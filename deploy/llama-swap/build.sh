#!/usr/bin/env bash
# Generated-By: OpenCode / deepseek-v4.1-flash
#
# Reproducible build of the patched llama-swap used for usage attribution.
#
# The source tarball is pinned by tag and sha256. Every patch under
# deploy/llama-swap/patches/ is applied in name order. Building requires Docker
# (Node for the UI bundle, Go for the static binary); the resulting binary is
# printed together with its sha256.
#
# Usage: ./build.sh [work-directory]
set -euo pipefail

readonly VERSION="v252"
readonly ARCHIVE="llama-swap-${VERSION}.tar.gz"
readonly URL="https://github.com/mostlygeek/llama-swap/archive/refs/tags/${VERSION}.tar.gz"
readonly SHA256="8681d563ea2766a9348aee6ae1b20f6c792a3945fb91f72e7ce712ab335d078f"
readonly SRCDIR="llama-swap-252"

workdir="${1:-./build/llama-swap}"
mkdir -p "$workdir"
workdir="$(cd "$workdir" && pwd)"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
patches_dir="$script_dir/patches"

if [ ! -d "$patches_dir" ] || ! ls "$patches_dir"/*.patch >/dev/null 2>&1; then
    echo "error: no *.patch files under $patches_dir" >&2
    exit 1
fi

if [ ! -f "$workdir/$ARCHIVE" ]; then
    curl -fSL "$URL" -o "$workdir/$ARCHIVE"
fi
echo "${SHA256}  ${workdir}/${ARCHIVE}" | sha256sum -c -

src="$workdir/$SRCDIR"
rm -rf "$src"
tar -xzf "$workdir/$ARCHIVE" -C "$workdir"

for patch_file in "$patches_dir"/*.patch; do
    ( cd "$src" && patch -p1 --forward < "$patch_file" )
done

# Node bundle: the Go build embeds ui/dist via the embed_ui tag.
docker run --rm -v "$src":/src -w /src/ui node:22-slim sh -c 'npm ci && npm run build'

docker run --rm -v "$src":/src -w /src -e GOFLAGS=-buildvcs=false golang:1.26 \
    go build -tags embed_ui \
    -ldflags "-X main.version=${VERSION}-llmsvc.1 -X main.commit=e31a1ad -X main.date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    -o build/llama-swap-linux-amd64 .

out="$src/build/llama-swap-linux-amd64"
echo "built: $out"
sha256sum "$out"
