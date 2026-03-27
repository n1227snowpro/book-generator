# 📚 Book Generator

Converts `.docx` manuscripts into **KDP-ready paperback PDFs** and **EPUB3 e-books** — automatically formatted in the Atticus style with mirror margins, custom fonts, chapter decorations, a bonus page, and a table of contents.

---

## Features

- **PDF output** — 6×9 in, mirror margins (0.875" inner / 0.5" outer), EB Garamond body text, Alegreya chapter titles, page numbers
- **EPUB3 output** — Atticus-compatible structure with nav TOC, NCX, custom fonts embedded
- **Web UI** — drag-and-drop file upload or paste a URL (Google Docs, Google Drive, S3, HTTPS)
- **Webhook API** — send a POST request, receive download links for PDF and EPUB
- **URL input** — paste a Google Docs share link and it downloads + converts automatically
- **Auto-formatting** — detects chapter headings, bible verses (italic), subheadings (bold), decoration images

---

## Requirements

| Requirement | Version |
|---|---|
| PHP | 8.0+ |
| Python | 3.8+ |
| Apache / Nginx | Any recent |
| OS | Ubuntu 20.04+ / Debian 11+ |

---

## Server Installation (Ubuntu / Debian)

### 1. Install system packages

```bash
sudo apt update && sudo apt install -y \
    apache2 libapache2-mod-php \
    php php-curl php-zip \
    python3 python3-pip
```

### 2. Install Python dependencies

```bash
pip3 install python-docx reportlab lxml defusedxml requests gdown
```

> For S3 URL support, also install: `pip3 install boto3`

### 3. Clone the repository

```bash
cd /var/www/html
sudo git clone https://github.com/n1227snowpro/book-generator.git bookgen
sudo chown -R www-data:www-data bookgen
```

### 4. Install fonts

Download these Google Fonts and place them in `~/.local/share/fonts/BookFonts/`:

| Font file | Download from |
|---|---|
| `EBGaramond-Regular.ttf` | [Google Fonts — EB Garamond](https://fonts.google.com/specimen/EB+Garamond) |
| `EBGaramond-Italic.ttf` | Same package |
| `EBGaramond-Bold.ttf` | Same package |
| `Alegreya-Regular.ttf` | [Google Fonts — Alegreya](https://fonts.google.com/specimen/Alegreya) |
| `Alegreya-Italic.ttf` | Same package |
| `Aldrich-Regular.ttf` | [Google Fonts — Aldrich](https://fonts.google.com/specimen/Aldrich) |

```bash
mkdir -p ~/.local/share/fonts/BookFonts
# Copy all 6 TTF files into that folder
cp *.ttf ~/.local/share/fonts/BookFonts/
```

> **Tip:** The generator falls back to Times New Roman if fonts are not found — output will still work but won't match the Atticus style.

### 5. Create the config file

```bash
cd /var/www/html/bookgen
cp config.example.php config.php
nano config.php
```

Fill in your values (see [Configuration](#configuration) below).

### 6. Set PHP upload limits

```bash
sudo nano /etc/php/*/apache2/php.ini
```

Update these lines:
```ini
upload_max_filesize = 100M
post_max_size       = 110M
max_execution_time  = 300
memory_limit        = 512M
```

```bash
sudo systemctl restart apache2
```

### 7. Set folder permissions

```bash
sudo chown -R www-data:www-data /var/www/html/bookgen
sudo chmod -R 755 /var/www/html/bookgen
```

Visit `http://your-server-ip/bookgen/` — done.

---

## Configuration

Create `config.php` (never commit this file — it's in `.gitignore`):

```php
<?php
// Path to Python 3 interpreter
define('PYTHON_BIN',      '/usr/bin/python3');

// Path to book_generator_2.py
define('GENERATOR_SCRIPT', __DIR__ . '/book_generator_2.py');

// Temp directory for uploads and generated files
define('TMP_BASE',         sys_get_temp_dir() . '/book_gen');

// GitHub Personal Access Token (needs 'repo' scope)
define('GITHUB_TOKEN',     'your_token_here');

// Your GitHub username
define('GITHUB_USERNAME',  'your_username');

// Max upload size in MB
define('MAX_UPLOAD_MB',    100);

// Webhook secret (leave empty to disable auth)
define('WEBHOOK_SECRET',   '');

// Public base URL (leave empty to auto-detect)
define('SERVER_BASE_URL',  'https://your-domain.com/bookgen');

// How long generated files are kept before expiry (hours)
define('FILE_TTL_HOURS',   24);
```

---

## Local Development (macOS)

```bash
# Install PHP
brew install php

# Install Python deps
pip3 install python-docx reportlab lxml defusedxml requests gdown

# Clone repo
git clone https://github.com/n1227snowpro/book-generator.git
cd book-generator

# Create config
cp config.example.php config.php
# Edit config.php with your values

# Start local server
php -d upload_max_filesize=100M \
    -d post_max_size=110M \
    -d max_execution_time=300 \
    -S localhost:8080
```

Open **http://localhost:8080** in your browser.

---

## Web UI

Visit `http://your-server/bookgen/` and:

1. Choose **Upload File** or **URL** (Google Docs / Drive / S3 / HTTPS)
2. Fill in Title, Author, and optional Subtitle
3. Click **Generate Book**
4. Download the PDF and EPUB when ready

### Supported URL formats

| Type | Example |
|---|---|
| Google Docs | `https://docs.google.com/document/d/FILE_ID/edit` |
| Google Drive | `https://drive.google.com/file/d/FILE_ID/view` |
| Amazon S3 | `s3://my-bucket/path/to/manuscript.docx` |
| Direct link | `https://example.com/manuscript.docx` |

> **Google Docs/Drive:** File must be shared as **Anyone with the link → Viewer**.

---

## Webhook API

### Endpoint

```
POST /webhook.php
Content-Type: application/json
```

### Request body

```json
{
  "project_id": "proj_abc123",
  "title":      "My Book Title",
  "subtitle":   "An Optional Subtitle",
  "author":     "Author Name",
  "book_url":   "https://docs.google.com/document/d/FILE_ID/edit"
}
```

### Response

```json
{
  "project_id":    "proj_abc123",
  "success":       true,
  "pdf_filename":  "My_Book_Title_paperback.pdf",
  "epub_filename": "My_Book_Title.epub",
  "pdf_url":       "https://your-server/download.php?job=wh_proj_abc123_a1b2&type=pdf",
  "epub_url":      "https://your-server/download.php?job=wh_proj_abc123_a1b2&type=epub",
  "expires_at":    "2026-03-28T10:00:00+00:00",
  "error":         ""
}
```

Download links expire after `FILE_TTL_HOURS` (default: 24 hours).

### Optional authentication

Set `WEBHOOK_SECRET` in `config.php`, then include the header:

```
X-Webhook-Secret: your-secret-here
```

### curl example

```bash
curl -X POST https://your-server/bookgen/webhook.php \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-secret" \
  -d '{
    "project_id": "proj_001",
    "title":      "God'\''s Man",
    "author":     "Your Name",
    "book_url":   "https://docs.google.com/document/d/FILE_ID/edit"
  }'
```

### Decoding the files (Node.js)

```js
const res  = await fetch('https://your-server/bookgen/webhook.php', { method: 'POST', ... });
const data = await res.json();

// Download the files
const pdf  = await fetch(data.pdf_url);
const epub = await fetch(data.epub_url);
```

---

## Manuscript Format

The generator works best when your `.docx` file uses proper Word styles:

| Content | Word Style |
|---|---|
| Day/chapter titles | **Heading 1** |
| Section headers (Prayer, Practice) | **Heading 2** |
| Bible verses / opening quotes | *Italic* runs |
| Body text | Normal |

If your manuscript uses plain text (e.g. exported from n8n), run the included `fix_gods_man_v2.py` script first to auto-apply correct styles.

---

## File Structure

```
book-generator/
├── book_generator_2.py   # Core converter (docx → PDF + EPUB)
├── index.php             # Web UI
├── webhook.php           # Webhook endpoint
├── download.php          # File download server
├── decoration.png        # Chapter decoration image
├── config.php            # ⚠️  Your config (not in git)
└── .gitignore
```

---

## License

MIT — free to use, modify, and deploy.
# Auto-deploy active
