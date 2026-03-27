#!/bin/bash
# ============================================================
#  Book Generator — Auto Deploy Script
#  Run this from your server terminal:
#  bash <(curl -s https://raw.githubusercontent.com/n1227snowpro/book-generator/main/deploy.sh)
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERR]${NC}  $1"; exit 1; }

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║     Book Generator — Deploy v1.0     ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── 1. Detect environment ────────────────────────────────────
info "Detecting server environment..."

OS=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"')
WEB_SERVER=""
WEB_ROOT=""
PHP_INI=""

if systemctl is-active --quiet apache2 2>/dev/null; then
    WEB_SERVER="apache2"
    WEB_ROOT=$(apache2ctl -S 2>/dev/null | grep "DocumentRoot" | head -1 | awk '{print $2}' || echo "/var/www/html")
    PHP_INI=$(php --ini 2>/dev/null | grep "Loaded Configuration" | awk '{print $NF}' || echo "")
elif systemctl is-active --quiet nginx 2>/dev/null; then
    WEB_SERVER="nginx"
    WEB_ROOT=$(nginx -T 2>/dev/null | grep -m1 "root " | awk '{print $2}' | tr -d ';' || echo "/var/www/html")
else
    warn "No web server detected — will install Apache2"
    WEB_SERVER="none"
    WEB_ROOT="/var/www/html"
fi

INSTALL_DIR="${WEB_ROOT}/bookgen"
info "Web server : ${WEB_SERVER}"
info "Web root   : ${WEB_ROOT}"
info "Install dir: ${INSTALL_DIR}"

# ── 2. Install system packages (safe — won't upgrade existing) ──
info "Installing required system packages..."
apt-get update -qq
# Only install what's missing
PKGS=""
command -v php    >/dev/null 2>&1 || PKGS="$PKGS php"
command -v python3 >/dev/null 2>&1 || PKGS="$PKGS python3"
dpkg -l python3-pip &>/dev/null   || PKGS="$PKGS python3-pip"
dpkg -l php-curl   &>/dev/null    || PKGS="$PKGS php-curl"
dpkg -l php-zip    &>/dev/null    || PKGS="$PKGS php-zip"
dpkg -l php-mbstring &>/dev/null  || PKGS="$PKGS php-mbstring"
dpkg -l unzip      &>/dev/null    || PKGS="$PKGS unzip"
dpkg -l wget       &>/dev/null    || PKGS="$PKGS wget"

if [ "$WEB_SERVER" = "none" ]; then
    PKGS="$PKGS apache2 libapache2-mod-php"
fi

if [ -n "$PKGS" ]; then
    apt-get install -y -qq $PKGS
    success "Installed: $PKGS"
else
    success "All system packages already present"
fi

# ── 3. Install Python packages ────────────────────────────────
info "Installing Python packages..."
PYTHON=$(command -v python3)
PIP="$PYTHON -m pip install --quiet --user"

$PIP python-docx lxml defusedxml requests gdown 2>&1 | tail -2

# reportlab: use 3.6.x for Python 3.8, newer otherwise
PY_VER=$($PYTHON -c "import sys; print(sys.version_info.minor)")
if [ "$PY_VER" -le 8 ]; then
    $PIP "reportlab==3.6.13" 2>&1 | tail -2
else
    $PIP reportlab 2>&1 | tail -2
fi
success "Python packages installed"

# ── 4. Install fonts ──────────────────────────────────────────
info "Installing fonts..."
FONT_DIR="$HOME/.local/share/fonts/BookFonts"
mkdir -p "$FONT_DIR"

BASE="https://github.com/google/fonts/raw/main"
declare -A FONTS=(
    ["EBGaramond-Regular.ttf"]="ofl/ebgaramond/EBGaramond-Regular.ttf"
    ["EBGaramond-Italic.ttf"]="ofl/ebgaramond/EBGaramond-Italic.ttf"
    ["EBGaramond-Bold.ttf"]="ofl/ebgaramond/EBGaramond-Bold.ttf"
    ["Alegreya-Regular.ttf"]="ofl/alegreya/Alegreya-Regular.ttf"
    ["Alegreya-Italic.ttf"]="ofl/alegreya/Alegreya-Italic.ttf"
    ["Aldrich-Regular.ttf"]="ofl/aldrich/Aldrich-Regular.ttf"
)

for FNAME in "${!FONTS[@]}"; do
    FPATH="$FONT_DIR/$FNAME"
    if [ ! -f "$FPATH" ]; then
        wget -q -O "$FPATH" "${BASE}/${FONTS[$FNAME]}" && echo "  + $FNAME" || warn "Failed to download $FNAME"
    else
        echo "  ✓ $FNAME (already exists)"
    fi
done
success "Fonts ready in $FONT_DIR"

# ── 5. Deploy files from GitHub ───────────────────────────────
info "Deploying files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

TMP_CLONE=$(mktemp -d)
git clone --quiet https://github.com/n1227snowpro/book-generator.git "$TMP_CLONE"

# Copy files (never overwrite config.php if it already exists)
for f in book_generator_2.py index.php webhook.php download.php decoration.png .gitignore; do
    cp "$TMP_CLONE/$f" "$INSTALL_DIR/$f" 2>/dev/null && echo "  + $f" || warn "Missing: $f"
done

# Copy example config only if config.php doesn't exist yet
if [ ! -f "$INSTALL_DIR/config.php" ]; then
    cp "$TMP_CLONE/config.example.php" "$INSTALL_DIR/config.php"
    warn "config.php created from example — update it before use!"
else
    success "config.php already exists — not overwritten"
fi

rm -rf "$TMP_CLONE"
success "Files deployed"

# ── 6. Configure PHP upload limits (non-destructive) ──────────
info "Configuring PHP limits..."
PHP_CONF_DIR=""
for d in /etc/php/*/apache2 /etc/php/*/cli /etc/php/apache2 /etc/php/cli; do
    [ -d "$d" ] && PHP_CONF_DIR="$d" && break
done

if [ -n "$PHP_CONF_DIR" ]; then
    CUSTOM_INI="$PHP_CONF_DIR/conf.d/99-bookgen.ini"
    cat > "$CUSTOM_INI" << 'INI'
; Book Generator — custom limits (auto-generated, safe to delete)
upload_max_filesize = 100M
post_max_size       = 110M
max_execution_time  = 300
memory_limit        = 512M
INI
    success "PHP limits set via $CUSTOM_INI"
else
    warn "Could not find PHP conf.d — set limits manually in php.ini"
fi

# ── 7. Set permissions ────────────────────────────────────────
info "Setting permissions..."
chown -R www-data:www-data "$INSTALL_DIR" 2>/dev/null || \
chown -R nobody:nobody "$INSTALL_DIR" 2>/dev/null || \
warn "Could not set www-data ownership — check permissions manually"
chmod -R 755 "$INSTALL_DIR"
success "Permissions set"

# ── 8. Restart web server ─────────────────────────────────────
info "Reloading web server..."
if systemctl is-active --quiet apache2; then
    systemctl reload apache2 && success "Apache reloaded"
elif systemctl is-active --quiet nginx; then
    systemctl reload nginx && success "Nginx reloaded"
fi

# ── 9. Update config.php with correct paths ───────────────────
PYTHON_PATH=$(command -v python3)
SCRIPT_PATH="$INSTALL_DIR/book_generator_2.py"

sed -i "s|/usr/bin/python3|$PYTHON_PATH|g" "$INSTALL_DIR/config.php"
sed -i "s|__DIR__ . '/book_generator_2.py'|'$SCRIPT_PATH'|g" "$INSTALL_DIR/config.php"

success "config.php updated with correct paths"

# ── 10. Quick test ────────────────────────────────────────────
info "Running quick Python test..."
$PYTHON -c "import docx, reportlab, lxml; print('  All Python packages OK')" && success "Python imports OK" || warn "Some Python packages missing"

# ── Done ──────────────────────────────────────────────────────
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║              Deploy Complete!                    ║"
echo "  ╠══════════════════════════════════════════════════╣"
echo "  ║  Web UI  : http://${SERVER_IP}/bookgen/          ║"
echo "  ║  Webhook : http://${SERVER_IP}/bookgen/webhook.php║"
echo "  ╠══════════════════════════════════════════════════╣"
echo "  ║  NEXT: Edit config.php and add your:             ║"
echo "  ║    • GITHUB_TOKEN                                ║"
echo "  ║    • GITHUB_USERNAME                             ║"
echo "  ║    • WEBHOOK_SECRET (optional)                   ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""
echo "  nano $INSTALL_DIR/config.php"
echo ""
