#!/bin/sh
# CSA one-line installer — served by the CSA server itself at GET /install, with @@BASE_URL@@
# rewritten to that server's own public base URL. Usage:  curl -sSL <server>/install | sh
#
# Contract:
#   * user-local, NO sudo: ~/.local/lib/csa/csa-client + a ~/.local/bin/csa-client symlink
#   * print EVERY path it touches — that transparency IS the privacy UX, not decoration
#   * unsupported OS/arch: exit non-zero with a clear message, leave NOTHING behind
#   * no /dev/tty (true `curl | sh` in a pipe): install, print the `csa-client setup` line, exit 0
#
# Everything is inside main() with `main "$@"` as the LAST line: sh executes a script as it reads it,
# so a connection that dies mid-download would otherwise run a half-script. This way a truncated
# body never reaches the invocation and does nothing. (rustup does the same. The server also sends
# Content-Length so curl itself fails loudly on a short read.)
set -eu

CSA_BASE="@@BASE_URL@@"

main() {
    CSA_LIB="$HOME/.local/lib/csa"
    CSA_BIN="$HOME/.local/bin"
    TMP=""
    trap 'cleanup' EXIT INT TERM HUP

    detect_target
    warn_cleartext
    printf '==> Downloading %s\n' "$CSA_BASE/latest/$ASSET"
    download
    verify_checksum
    install_binary
    print_paths
    finish_setup
}

cleanup() {
    # A failed download/verify must leave nothing behind — including on Ctrl-C.
    [ -n "${TMP:-}" ] && [ -d "$TMP" ] && rm -rf "$TMP"
    return 0
}

die() {
    printf '\n%s\n' "error: $*" >&2
    exit 1
}

# detect_target: uname -> the asset name CI publishes (.github/workflows/release.yml).
# Anything not in that matrix fails HERE, before a single file is written.
detect_target() {
    os="$(uname -s 2>/dev/null || echo unknown)"
    arch="$(uname -m 2>/dev/null || echo unknown)"
    case "$os" in
        Darwin) os_n="darwin" ;;
        Linux)  os_n="linux" ;;
        MINGW*|MSYS*|CYGWIN*)
            die "Windows is not supported by this installer.
Download csa-client-windows-amd64.exe from $CSA_BASE/latest/csa-client-windows-amd64.exe
and run:  csa-client.exe setup --server $CSA_BASE" ;;
        *) die "unsupported operating system: $os (CSA ships macOS and Linux binaries).
Nothing was installed." ;;
    esac
    case "$arch" in
        arm64|aarch64) arch_n="arm64" ;;
        x86_64|amd64)  arch_n="amd64" ;;
        *) die "unsupported CPU architecture: $arch on $os (CSA ships arm64 and amd64).
Nothing was installed." ;;
    esac
    ASSET="csa-client-${os_n}-${arch_n}"
}

# warn_cleartext: over plain http to a non-loopback host, an active MITM can rewrite BOTH the binary
# and checksums.txt, so the sha256 check proves nothing. The server refuses to bake a non-loopback
# http URL unless the operator set CSA_PUBLIC_URL, so reaching here means they opted into it — warn,
# don't block (they may be on a trusted LAN).
warn_cleartext() {
    case "$CSA_BASE" in
        http://127.0.0.1*|http://localhost*|http://[::1]*|https://*) return 0 ;;
        http://*) printf '    WARNING: downloading over plain HTTP from %s — an on-path attacker can\n' "$CSA_BASE" >&2
                  printf '             tamper with the binary. Prefer an https server URL.\n' >&2 ;;
    esac
}

download() {
    command -v curl >/dev/null 2>&1 || die "curl is required but was not found in PATH."
    TMP="$(mktemp -d "${TMPDIR:-/tmp}/csa-install.XXXXXX")" || die "could not create a temporary directory."
    # --fail: a 404 must not be saved as a "binary". Downloads to TMP, never over the live path.
    if ! curl -fsSL --proto '=https,http' -o "$TMP/$ASSET" "$CSA_BASE/latest/$ASSET"; then
        die "could not download $CSA_BASE/latest/$ASSET
The server may be unreachable, behind a proxy, or not serving client binaries yet.
A 404 means the server publishes no client binaries: its admin runs an image built from server/Dockerfile, which carries them.
Nothing was installed."
    fi
    [ -s "$TMP/$ASSET" ] || die "downloaded an empty file from $CSA_BASE/latest/$ASSET. Nothing was installed."
}

# verify_checksum: compare against the CI-published checksums.txt (sha256sum format: "<hash>  <name>").
# Guards the silent-truncation case a plain HTTP body cannot. If the box has no sha256 tool at all we
# warn and continue rather than refuse: Content-Length already makes curl fail on a short read, so the
# hash is a second line of defence, not the only one.
verify_checksum() {
    sum=""
    if command -v shasum >/dev/null 2>&1; then
        sum="$(shasum -a 256 "$TMP/$ASSET" 2>/dev/null | cut -d' ' -f1)"
    elif command -v sha256sum >/dev/null 2>&1; then
        sum="$(sha256sum "$TMP/$ASSET" 2>/dev/null | cut -d' ' -f1)"
    elif command -v openssl >/dev/null 2>&1; then
        sum="$(openssl dgst -sha256 "$TMP/$ASSET" 2>/dev/null | awk '{print $NF}')"
    else
        printf '    (no sha256 tool found — skipping checksum verification)\n'
        return 0
    fi
    if ! curl -fsSL --proto '=https,http' -o "$TMP/checksums.txt" "$CSA_BASE/latest/checksums.txt" 2>/dev/null; then
        printf '    (server publishes no checksums.txt — skipping verification)\n'
        return 0
    fi
    want="$(awk -v a="$ASSET" '$2 == a || $2 == "*"a {print $1}' "$TMP/checksums.txt" | head -n1)"
    if [ -z "$want" ]; then
        printf '    (no checksum listed for %s — skipping verification)\n' "$ASSET"
        return 0
    fi
    if [ "$sum" != "$want" ]; then
        die "checksum mismatch for $ASSET
  expected: $want
  actual:   $sum
The download was corrupted or tampered with. Nothing was installed."
    fi
    printf '==> Verified sha256\n'
}

install_binary() {
    mkdir -p "$CSA_LIB" "$CSA_BIN" || die "could not create $CSA_LIB / $CSA_BIN"
    chmod 0755 "$TMP/$ASSET"
    # mv over the live path last, and only after verification: an interrupted install never leaves a
    # half-written binary where the symlink points.
    mv -f "$TMP/$ASSET" "$CSA_LIB/csa-client" || die "could not install into $CSA_LIB"
    ln -sf "$CSA_LIB/csa-client" "$CSA_BIN/csa-client" || die "could not link $CSA_BIN/csa-client"
}

print_paths() {
    printf '==> Installed %s -> %s\n' "$CSA_BIN/csa-client" "$CSA_LIB/csa-client"
    printf '    These are the only two paths this installer wrote.\n'
    printf '    It has NOT touched your shell config, ~/.claude, or ~/.csa — and it has not\n'
    printf '    sent anything anywhere. The next step asks before any of that.\n'
}

# on_path: is $CSA_BIN already on PATH? ~/.local/bin is NOT on the default macOS zsh PATH, so
# without this the user's very next command is "csa-client: command not found". We PRINT the export
# line instead of editing their shell rc: this installer's promise is that it touched exactly the
# two paths above, and silently rewriting .zshrc would make that a lie.
on_path() {
    case ":${PATH:-}:" in
        *":$CSA_BIN:"*) return 0 ;;
        *) return 1 ;;
    esac
}

path_hint() {
    on_path && return 0
    rc="your shell profile"
    case "${SHELL:-}" in
        */zsh)  rc="~/.zshrc" ;;
        */bash) rc="~/.bashrc" ;;
        */fish) rc="~/.config/fish/config.fish" ;;
    esac
    printf '\n    NOTE: %s is not on your PATH. To run csa-client by name, add this to %s:\n' "$CSA_BIN" "$rc"
    printf '      export PATH="$HOME/.local/bin:$PATH"\n'
}

# finish_setup: hand over to `csa-client setup`, which is where consent is asked and where anything
# else gets written. `curl | sh` leaves stdin on the pipe, so setup's consent prompt must read the
# terminal directly. We try to OPEN /dev/tty rather than test it: on macOS a `[ -r /dev/tty ]` test
# can pass with no controlling terminal, and consent that cannot be read must never be assumed.
finish_setup() {
    cleanup   # the `exec` below replaces this shell, so the EXIT trap never fires — clean TMP now,
    trap - EXIT INT TERM HUP   # ...and disarm it so a later firing can't rm an already-gone dir
    if ( exec 3</dev/tty ) 2>/dev/null; then
        printf '==> Finishing setup...\n'
        exec "$CSA_LIB/csa-client" setup --server "$CSA_BASE" </dev/tty
    fi
    path_hint
    printf '\n==> Almost done — one step left, in a terminal:\n'
    if on_path; then
        printf '      csa-client setup --server %s\n' "$CSA_BASE"
    else
        printf '      %s/csa-client setup --server %s\n' "$CSA_BIN" "$CSA_BASE"
    fi
    printf '\n    That shows you exactly what CSA would collect, asks you to accept it, and\n'
    printf '    connects this machine to your team. Nothing ships until you do.\n'
    exit 0
}

main "$@"
