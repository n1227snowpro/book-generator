<?php
// ════════════════════════════════════════════════════════
//  BOOK GENERATOR — Server Configuration
//  Copy this file to config.php and fill in your values.
//  config.php is gitignored and should NEVER be committed.
// ════════════════════════════════════════════════════════

// Path to Python 3 interpreter
define('PYTHON_BIN',        '/usr/bin/python3');

// Path to book_generator_2.py on the server
define('GENERATOR_SCRIPT',  __DIR__ . '/book_generator_2.py');

// Temp directory for uploads and generated files (must be writable)
define('TMP_BASE',          sys_get_temp_dir() . '/book_gen');

// GitHub Personal Access Token (needs 'repo' scope)
// Create at: https://github.com/settings/tokens
define('GITHUB_TOKEN',      'your_github_token_here');

// Your GitHub username
define('GITHUB_USERNAME',   'your_github_username');

// Max upload size in MB (must also match php.ini upload_max_filesize)
define('MAX_UPLOAD_MB',     100);

// Webhook secret — set any random string here and send it as
// the X-Webhook-Secret header from your calling system.
// Leave empty ('') to disable authentication.
define('WEBHOOK_SECRET',    '');

// Public base URL of this server (no trailing slash).
// Used to build download links returned by the webhook.
// Leave empty to auto-detect from the request.
define('SERVER_BASE_URL',   '');

// How long (in hours) to keep generated files before they expire.
define('FILE_TTL_HOURS',    24);
