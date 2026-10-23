#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  build_deb.sh – Erzeugt .deb-Pakete für ec-su_axb35-linux
#  
#  Aufruf:   bash packaging/build_deb.sh [all|standard|mok]
#  Output:   dist/ec-su_axb35-linux_1.1-1_amd64.deb
#            dist/ec-su_axb35-linux-mok_1.1-1_amd64.deb
# ═══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"      # → GMKtec Evo-X2 Lüftersteuerung V1.1/
if [ -d "${REPO_ROOT}/ec-su_axb35-linux/src" ]; then
    PROJECT_ROOT="${REPO_ROOT}/ec-su_axb35-linux"
else
    PROJECT_ROOT="${REPO_ROOT}"
fi
DIST_DIR="${SCRIPT_DIR}/dist"
BUILD_TMP="/tmp/ec-deb-build-$$"

PKG_NAME="ec-su-axb35-linux"
PKG_VERSION="1.1-1"
ARCH="amd64"

# ─── Farben ──────────────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; B='\033[1;34m'; N='\033[0m'
info()  { echo -e "${B}[INFO]${N} $*"; }
ok()    { echo -e "${G}[OK]${N}   $*"; }
warn()  { echo -e "${Y}[WARN]${N} $*"; }

# ─── Cleanup ─────────────────────────────────────────────
cleanup() { rm -rf "$BUILD_TMP"; }
trap cleanup EXIT

# ─── Build one .deb ──────────────────────────────────────
build_deb() {
    local variant="$1"          # "standard" or "mok"
    local pkg_name="${PKG_NAME}"
    [ "$variant" = "mok" ] && pkg_name="${PKG_NAME}-mok"
    # PKG_NAME is already "ec-su-axb35-linux" (no underscores – Debian rule)

    local out_file="${DIST_DIR}/${pkg_name}_${PKG_VERSION}_${ARCH}.deb"
    local staging="${BUILD_TMP}/${pkg_name}"

    info "Building: ${pkg_name} (${variant})"

    # ── Create directory structure ──
    mkdir -p "${staging}/usr/src/ec_su_axb35-1.1/src"
    mkdir -p "${staging}/usr/local/bin"
    mkdir -p "${staging}/usr/share/applications"
    mkdir -p "${staging}/DEBIAN"

    # ── Copy DKMS source (Kbuild + src/) ──
    cp "${PROJECT_ROOT}/Makefile"  "${staging}/usr/src/ec_su_axb35-1.1/"
    cp "${PROJECT_ROOT}/src/ec_su_axb35.c" "${staging}/usr/src/ec_su_axb35-1.1/src/"
    cp "${SCRIPT_DIR}/dkms.conf" "${staging}/usr/src/ec_su_axb35-1.1/"

    # ── Copy GUI script ──
    cp "${PROJECT_ROOT}/python-gui/axb35_monitor_gui.py" \
       "${staging}/usr/local/bin/axb35-system-monitor"
    chmod 0755 "${staging}/usr/local/bin/axb35-system-monitor"

    # ── Desktop entry (template – postinst will also create it) ──
    cat > "${staging}/usr/share/applications/ec-su_axb35-linux.desktop" << 'DESK'
[Desktop Entry]
Type=Application
Name=AXB35 System-Monitor
Comment=Fan control & system monitor for Sixunited AXB35-02
Exec=/usr/local/bin/axb35-system-monitor
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
DESK

    # ── DEBIAN/control ──
    local ctrl="${SCRIPT_DIR}/control"
    cp "$ctrl" "${staging}/DEBIAN/control"
    # Adjust package name for mok variant
    if [ "$variant" = "mok" ]; then
        sed -i 's/^Package: ec-su-axb35-linux$/Package: ec-su-axb35-linux-mok/' \
            "${staging}/DEBIAN/control"
        sed -i '/^Description:/a\ Secure Boot (MOK) version – automatically signs the module.' \
            "${staging}/DEBIAN/control"
    fi

    # ── DEBIAN/postinst ──
    if [ "$variant" = "mok" ]; then
        cp "${SCRIPT_DIR}/postinst-mok" "${staging}/DEBIAN/postinst"
    else
        cp "${SCRIPT_DIR}/postinst" "${staging}/DEBIAN/postinst"
    fi
    chmod 0755 "${staging}/DEBIAN/postinst"

    # ── DEBIAN/prerm + postrm ──
    cp "${SCRIPT_DIR}/prerm"   "${staging}/DEBIAN/prerm"
    cp "${SCRIPT_DIR}/postrm"  "${staging}/DEBIAN/postrm"
    chmod 0755 "${staging}/DEBIAN/prerm" "${staging}/DEBIAN/postrm"

    # ── Build the .deb ──
    dpkg-deb --build --root-owner-group "$staging" "$out_file" 2>&1 | sed 's/^/       /'
    ok "Created: ${out_file} ($(du -h "$out_file" | cut -f1))"
}

# ─── Main ────────────────────────────────────────────────
main() {
    local target="${1:-all}"

    # Check prerequisites
    if ! command -v dpkg-deb &>/dev/null; then
        echo -e "${Y}[ERROR]${N} dpkg-deb not found. Install with: sudo apt install dpkg-dev"
        exit 1
    fi
    if [ ! -f "${PROJECT_ROOT}/src/ec_su_axb35.c" ]; then
        echo -e "${Y}[ERROR]${N} Source not found at ${PROJECT_ROOT}/src/ec_su_axb35.c"
        exit 1
    fi

    mkdir -p "$DIST_DIR"

    case "$target" in
        standard) build_deb "standard" ;;
        mok)      build_deb "mok" ;;
        all)
            build_deb "standard"
            echo ""
            build_deb "mok"
            ;;
        *)
            echo "Usage: $0 [all|standard|mok]"
            exit 1
            ;;
    esac

    echo ""
    ok "All done! Packages in: ${DIST_DIR}/"
    echo ""
    echo "  Install (Standard / Secure Boot OFF):"
    echo "    sudo dpkg -i ${DIST_DIR}/ec-su-axb35-linux_1.1-1_amd64.deb"
    echo ""
    echo "  Install (MOK / Secure Boot ON):"
    echo "    sudo dpkg -i ${DIST_DIR}/ec-su-axb35-linux-mok_1.1-1_amd64.deb"
    echo "    sudo reboot   ← MOK enrollment screen appears"
}

main "$@"
