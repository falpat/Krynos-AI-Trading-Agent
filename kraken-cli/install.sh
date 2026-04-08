#!/bin/sh
set -e

REPO_URL="https://github.com/krakenfx/kraken-cli"
BINARY="kraken"
INSTALL_DIR="/usr/local/bin"

get_target() {
  os=$(uname -s)
  arch=$(uname -m)

  case "$os" in
    Darwin)
      case "$arch" in
        x86_64)  echo "x86_64-apple-darwin" ;;
        arm64)   echo "aarch64-apple-darwin" ;;
        *)       echo "Unsupported architecture: $arch" >&2; exit 1 ;;
      esac
      ;;
    Linux)
      case "$arch" in
        x86_64)  echo "x86_64-unknown-linux-gnu" ;;
        aarch64) echo "aarch64-unknown-linux-gnu" ;;
        *)       echo "Unsupported architecture: $arch" >&2; exit 1 ;;
      esac
      ;;
    *)
      echo "Unsupported OS: $os" >&2
      echo "For Windows, download the binary manually from:" >&2
      echo "  ${REPO_URL}/releases" >&2
      exit 1
      ;;
  esac
}

main() {
  target=$(get_target)

  tag=$(curl -sSf "https://raw.githubusercontent.com/krakenfx/kraken-cli/main/Cargo.toml" | grep '^version' | head -1 | cut -d'"' -f2)
  if [ -z "$tag" ]; then
    echo "Error: could not determine latest version" >&2
    exit 1
  fi
  version="v${tag}"
  tag="cli-${version}"

  tarball="kraken-${version}-${target}.tar.gz"
  url="${REPO_URL}/releases/download/${tag}/${tarball}"
  checksums_url="${REPO_URL}/releases/download/${tag}/checksums.txt"

  echo "Installing ${BINARY} ${version} (${target})..."
  echo ""

  tmpdir=$(mktemp -d)
  trap 'rm -rf "$tmpdir"' EXIT

  curl -sSfL "$url" -o "$tmpdir/$tarball"
  curl -sSfL "$checksums_url" -o "$tmpdir/checksums.txt"

  expected_hash=$(grep "$tarball" "$tmpdir/checksums.txt" | awk '{print $1}')
  if [ -z "$expected_hash" ]; then
    echo "Error: no checksum found for $tarball" >&2
    exit 1
  fi

  if command -v sha256sum >/dev/null 2>&1; then
    actual_hash=$(sha256sum "$tmpdir/$tarball" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then
    actual_hash=$(shasum -a 256 "$tmpdir/$tarball" | awk '{print $1}')
  else
    echo "Error: need sha256sum or shasum to verify download" >&2
    exit 1
  fi

  if [ "$actual_hash" != "$expected_hash" ]; then
    echo "Error: checksum mismatch!" >&2
    echo "  Expected: $expected_hash" >&2
    echo "  Got:      $actual_hash" >&2
    exit 1
  fi

  echo "Checksum verified."
  tar xzf "$tmpdir/$tarball" -C "$tmpdir"

  if [ -w "$INSTALL_DIR" ]; then
    mv "$tmpdir/$BINARY" "$INSTALL_DIR/$BINARY"
  else
    sudo mv "$tmpdir/$BINARY" "$INSTALL_DIR/$BINARY"
  fi

  chmod +x "$INSTALL_DIR/$BINARY"

  echo ""
  echo "Installed ${BINARY} to ${INSTALL_DIR}/${BINARY}"
  echo "Run 'kraken --help' to get started."
}

main
