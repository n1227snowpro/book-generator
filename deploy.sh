#!/bin/bash
# ============================================================
#  Book Generator — Auto Deploy Script
#  Run as root:
#  bash <(curl -s https://raw.githubusercontent.com/n1227snowpro/book-generator/main/deploy.sh)
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()    { echo -e "${RED}[ERR]${NC}  $1"; exit 1; }

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║     Book Generator — Deploy v1.1     ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── 0. Must be root ───────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    fail "Please run as root: sudo bash <(curl -s ...)"
fi
SUDO=""   # already root, no sudo needed

# ── 1. Detect environment ─────────────────────────────────────
info "Detecting server environment..."

WEB_SERVER="none"
WEB_ROOT="/var/www/html"

if systemctl is-active --quiet apache2 2>/dev/null; then
    WEB_SERVER="apache2"
elif systemctl is-active --quiet nginx 2>/dev/null; then
    WEB_SERVER="nginx"
fi

INSTALL_DIR="${WEB_ROOT}/bookgen"
info "Web server : ${WEB_SERVER}"
info "Install dir: ${INSTALL_DIR}"

# ── 2. Install system packages ────────────────────────────────
info "Installing required system packages..."
apt-get update -qq

PKGS=""
command -v php     >/dev/null 2>&1 || PKGS="$PKGS php"
command -v python3 >/dev/null 2>&1 || PKGS="$PKGS python3"
dpkg -l python3-pip  2>/dev/null | grep -q '^ii' || PKGS="$PKGS python3-pip"
dpkg -l php-curl     2>/dev/null | grep -q '^ii' || PKGS="$PKGS php-curl"
dpkg -l php-zip      2>/dev/null | grep -q '^ii' || PKGS="$PKGS php-zip"
dpkg -l php-mbstring 2>/dev/null | grep -q '^ii' || PKGS="$PKGS php-mbstring"
dpkg -l unzip        2>/dev/null | grep -q '^ii' || PKGS="$PKGS unzip"
dpkg -l wget         2>/dev/null | grep -q '^ii' || PKGS="$PKGS wget"
dpkg -l git          2>/dev/null | grep -q '^ii' || PKGS="$PKGS git"

if [ "$WEB_SERVER" = "none" ]; then
    PKGS="$PKGS apache2 libapache2-mod-php"
fi

if [ -n "$PKGS" ]; then
    apt-get install -y -qq $PKGS
    success "Installed:$PKGS"
else
    success "All system packages already present"
fi

# ── 3. Install Python packages ────────────────────────────────
info "Installing Python packages..."
PYTHON=$(command -v python3)
PY_VER=$($PYTHON -c "import sys; print(sys.version_info.minor)")

# PEP 668: newer distros (Python 3.11+) mark system Python as externally managed
PIP_FLAGS="--quiet"
if $PYTHON -m pip install --quiet --dry-run pip 2>&1 | grep -q "externally-managed"; then
    PIP_FLAGS="--quiet --break-system-packages"
fi

$PYTHON -m pip install $PIP_FLAGS python-docx lxml defusedxml requests gdown

if [ "$PY_VER" -le 8 ]; then
    $PYTHON -m pip install $PIP_FLAGS "reportlab==3.6.13"
else
    $PYTHON -m pip install $PIP_FLAGS reportlab
fi
success "Python packages installed"

# ── 4. Install fonts ──────────────────────────────────────────
info "Installing fonts..."
FONT_DIR="/root/.local/share/fonts/BookFonts"
mkdir -p "$FONT_DIR"

BASE="https://github.com/google/fonts/raw/main"

download_font() {
    local fname="$1"
    local fpath="$2"
    local dest="$FONT_DIR/$fname"
    if [ ! -f "$dest" ]; then
        wget -q -O "$dest" "$BASE/$fpath" && echo "  + $fname" || warn "  ! Failed: $fname"
    else
        echo "  ✓ $fname"
    fi
}

download_font "EBGaramond-Regular.ttf" "ofl/ebgaramond/static/EBGaramond-Regular.ttf"
download_font "EBGaramond-Italic.ttf"  "ofl/ebgaramond/static/EBGaramond-Italic.ttf"
download_font "EBGaramond-Bold.ttf"    "ofl/ebgaramond/static/EBGaramond-Bold.ttf"
download_font "Alegreya-Regular.ttf"   "ofl/alegreya/static/Alegreya-Regular.ttf"
download_font "Alegreya-Italic.ttf"    "ofl/alegreya/static/Alegreya-Italic.ttf"
download_font "Aldrich-Regular.ttf"    "ofl/aldrich/Aldrich-Regular.ttf"

success "Fonts ready in $FONT_DIR"

# ── 5. Deploy files ───────────────────────────────────────────
info "Deploying files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

TMP_CLONE=$(mktemp -d)
git clone --quiet https://github.com/n1227snowpro/book-generator.git "$TMP_CLONE"

for f in book_generator_2.py index.php webhook.php download.php decoration.png; do
    cp "$TMP_CLONE/$f" "$INSTALL_DIR/$f" && echo "  + $f"
done

if [ ! -f "$INSTALL_DIR/config.php" ]; then
    cp "$TMP_CLONE/config.example.php" "$INSTALL_DIR/config.php"
    warn "config.php created from example — update it before use!"
else
    success "config.php already exists — not overwritten"
fi

rm -rf "$TMP_CLONE"
success "Files deployed"

# ── 6. PHP limits (non-destructive, separate file) ────────────
info "Configuring PHP limits..."
PHP_CONF=""
for d in /etc/php/*/apache2/conf.d /etc/php/*/cli/conf.d /etc/php/conf.d; do
    if [ -d "$d" ]; then PHP_CONF="$d"; break; fi
done

if [ -n "$PHP_CONF" ]; then
    cat > "$PHP_CONF/99-bookgen.ini" << 'INI'
upload_max_filesize = 100M
post_max_size       = 110M
max_execution_time  = 300
memory_limit        = 512M
INI
    success "PHP limits written to $PHP_CONF/99-bookgen.ini"
else
    warn "Could not find PHP conf.d — set limits in php.ini manually"
fi

# ── 7. Permissions ────────────────────────────────────────────
info "Setting permissions..."
chown -R www-data:www-data "$INSTALL_DIR" 2>/dev/null || true
chmod -R 755 "$INSTALL_DIR"
success "Permissions set"

# ── 8. Reload web server ──────────────────────────────────────
if systemctl is-active --quiet apache2 2>/dev/null; then
    systemctl reload apache2 && success "Apache reloaded"
elif systemctl is-active --quiet nginx 2>/dev/null; then
    systemctl reload nginx && success "Nginx reloaded"
else
    systemctl enable apache2 2>/dev/null || true
    systemctl start apache2  && success "Apache started"
fi

# ── 9. Update config.php paths ────────────────────────────────
PYTHON_PATH=$(command -v python3)
sed -i "s|/usr/bin/python3|$PYTHON_PATH|g" "$INSTALL_DIR/config.php"
success "config.php paths updated"

# ── 10. Quick test ────────────────────────────────────────────
info "Testing Python imports..."
$PYTHON -c "import docx, reportlab, lxml; print('  All packages OK')" \
    && success "Python OK" || warn "Some Python packages missing — check manually"

# ── Done ─────────────────────────────────────────────────────
SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║              Deploy Complete!                    ║"
echo "  ╠══════════════════════════════════════════════════╣"
printf "  ║  Web UI  : http://%-30s║\n" "${SERVER_IP}/bookgen/"
printf "  ║  Webhook : http://%-30s║\n" "${SERVER_IP}/bookgen/webhook.php"
echo "  ╠══════════════════════════════════════════════════╣"
echo "  ║  NEXT: update config.php with your tokens:       ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""
echo "  nano $INSTALL_DIR/config.php"
echo ""
