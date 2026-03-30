<?php
// ════════════════════════════════════════════════════════════════════════════
//  Book Generator — Webhook Endpoint
//  POST /webhook.php
//
//  Request body (JSON):
//  {
//    "project_id": "proj_abc123",
//    "title":      "My Book Title",
//    "subtitle":   "An Optional Subtitle",
//    "author":     "Author Name",
//    "book_url":   "https://docs.google.com/document/d/.../edit"
//  }
//
//  Response (JSON):
//  {
//    "project_id":    "proj_abc123",
//    "success":       true,
//    "pdf_filename":  "My_Book_Title_paperback.pdf",
//    "epub_filename": "My_Book_Title.epub",
//    "pdf_url":       "https://your-server/download.php?job=wh_proj_abc123_xx&type=pdf",
//    "epub_url":      "https://your-server/download.php?job=wh_proj_abc123_xx&type=epub",
//    "expires_at":    "2026-03-28T10:00:00+00:00",
//    "error":         ""
//  }
//
//  Optional authentication: set WEBHOOK_SECRET in config.php and pass it
//  as the  X-Webhook-Secret  request header.
// ════════════════════════════════════════════════════════════════════════════

require __DIR__ . '/config.php';

header('Content-Type: application/json; charset=utf-8');
set_time_limit(600); // allow up to 10 min for large books

// ── Helper ────────────────────────────────────────────────────────────────────
function webhook_error(int $http_code, string $message, string $project_id = ''): void {
    http_response_code($http_code);
    echo json_encode([
        'project_id' => $project_id,
        'success'    => false,
        'error'      => $message,
        'pdf'        => null,
        'epub'       => null,
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

// ── Method check ──────────────────────────────────────────────────────────────
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    webhook_error(405, 'Method not allowed. Use POST.');
}

// ── Auth (optional) ───────────────────────────────────────────────────────────
if (defined('WEBHOOK_SECRET') && WEBHOOK_SECRET !== '') {
    $incoming_secret = $_SERVER['HTTP_X_WEBHOOK_SECRET'] ?? '';
    if (!hash_equals(WEBHOOK_SECRET, $incoming_secret)) {
        webhook_error(401, 'Unauthorized: invalid or missing X-Webhook-Secret header.');
    }
}

// ── Parse JSON body ───────────────────────────────────────────────────────────
$raw  = file_get_contents('php://input');
$data = json_decode($raw, true);

if (!is_array($data)) {
    webhook_error(400, 'Invalid JSON body.');
}

// ── Validate required fields ──────────────────────────────────────────────────
$project_id = trim($data['project_id'] ?? '');
$title      = trim($data['title']      ?? '');
$author     = trim($data['author']     ?? '');
$subtitle   = trim($data['subtitle']   ?? '');
$no_bonus   = isset($data['bonus']) && $data['bonus'] === false;
$book_url   = trim($data['book_url']   ?? '');

if (empty($project_id)) webhook_error(400, 'Missing required field: project_id.');
if (empty($title))      webhook_error(400, 'Missing required field: title.',      $project_id);
if (empty($author))     webhook_error(400, 'Missing required field: author.',     $project_id);
if (empty($book_url))   webhook_error(400, 'Missing required field: book_url.',  $project_id);

// Basic URL sanity
$allowed_prefixes = ['http://', 'https://', 's3://'];
$url_ok = false;
foreach ($allowed_prefixes as $p) {
    if (str_starts_with($book_url, $p)) { $url_ok = true; break; }
}
if (!$url_ok) {
    webhook_error(400, 'book_url must start with http://, https://, or s3://', $project_id);
}

// ── Create job directory ──────────────────────────────────────────────────────
$job_id     = 'wh_' . preg_replace('/[^\w-]/', '_', $project_id) . '_' . bin2hex(random_bytes(3));
$job_dir    = TMP_BASE . '/' . $job_id;
$output_dir = $job_dir . '/output';

if (!mkdir($output_dir, 0755, true)) {
    webhook_error(500, 'Failed to create job directory.', $project_id);
}

// ── Build and run Python command ──────────────────────────────────────────────
$cmd_parts = [
    escapeshellarg(PYTHON_BIN),
    escapeshellarg(GENERATOR_SCRIPT),
    '--input',   escapeshellarg($book_url),
    '--title',   escapeshellarg($title),
    '--author',  escapeshellarg($author),
    '--out-dir', escapeshellarg($output_dir),
];
if (!empty($subtitle)) {
    $cmd_parts[] = '--subtitle';
    $cmd_parts[] = escapeshellarg($subtitle);
}
if ($no_bonus) {
    $cmd_parts[] = '--no-bonus';
}

$cmd          = implode(' ', $cmd_parts) . ' 2>&1';
$output_lines = [];
$exit_code    = 0;
exec($cmd, $output_lines, $exit_code);
$log = implode("\n", $output_lines);

// ── Locate generated files ────────────────────────────────────────────────────
$pdf_path  = null;
$epub_path = null;

if (is_dir($output_dir)) {
    foreach (scandir($output_dir) as $file) {
        $fp = $output_dir . '/' . $file;
        if (str_ends_with($file, '_paperback.pdf') && is_file($fp)) $pdf_path  = $fp;
        if (str_ends_with($file, '.epub')           && is_file($fp)) $epub_path = $fp;
    }
}

if (!$pdf_path && !$epub_path) {
    // Clean up before responding
    _rmdir_recursive($job_dir);
    http_response_code(500);
    echo json_encode([
        'project_id' => $project_id,
        'success'    => false,
        'error'      => 'Generation failed — no output files produced.',
        'log'        => $log,
        'pdf'        => null,
        'epub'       => null,
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

// ── Build download URLs ───────────────────────────────────────────────────────
// Detect server base URL
if (defined('SERVER_BASE_URL') && SERVER_BASE_URL !== '') {
    $base_url = rtrim(SERVER_BASE_URL, '/');
} else {
    $scheme   = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    $host     = $_SERVER['HTTP_HOST'] ?? 'localhost';
    $dir      = rtrim(dirname($_SERVER['SCRIPT_NAME']), '/');
    $base_url = $scheme . '://' . $host . $dir;
}

$dl_base    = $base_url . '/download.php?job=' . urlencode($job_id);
$expires_at = gmdate('c', time() + (FILE_TTL_HOURS * 3600));

$response = [
    'project_id'    => $project_id,
    'success'       => true,
    'error'         => '',
    'pdf_filename'  => $pdf_path  ? basename($pdf_path)  : null,
    'epub_filename' => $epub_path ? basename($epub_path) : null,
    'pdf_url'       => $pdf_path  ? $dl_base . '&type=pdf'  : null,
    'epub_url'      => $epub_path ? $dl_base . '&type=epub' : null,
    'expires_at'    => $expires_at,
    'log'           => $log,
];

// NOTE: job_dir is intentionally NOT deleted here — download.php serves it.
// Files expire after FILE_TTL_HOURS (see config.php).

echo json_encode($response, JSON_UNESCAPED_UNICODE);
exit;

// ── Utilities ─────────────────────────────────────────────────────────────────
function _rmdir_recursive(string $dir): void {
    if (!is_dir($dir)) return;
    foreach (scandir($dir) as $item) {
        if ($item === '.' || $item === '..') continue;
        $path = $dir . '/' . $item;
        is_dir($path) ? _rmdir_recursive($path) : unlink($path);
    }
    rmdir($dir);
}

// ── Cleanup old expired jobs (runs opportunistically on each webhook call) ────
function _cleanup_expired_jobs(string $tmp_base): void {
    if (!is_dir($tmp_base)) return;
    $ttl = defined('FILE_TTL_HOURS') ? FILE_TTL_HOURS * 3600 : 86400;
    foreach (scandir($tmp_base) as $dir) {
        if (!str_starts_with($dir, 'wh_')) continue;
        $path = $tmp_base . '/' . $dir;
        if (is_dir($path) && (time() - filemtime($path)) > $ttl) {
            _rmdir_recursive($path);
        }
    }
}
_cleanup_expired_jobs(TMP_BASE);
