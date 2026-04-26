<?php
// ════════════════════════════════════════════════════════════════════════════
//  Book Generator — Async Webhook Endpoint
//
//  Step 1 — Start a job (returns immediately, no timeout risk):
//  POST /webhook.php
//  {
//    "project_id": "proj_abc123",
//    "title":      "My Book Title",
//    "subtitle":   "An Optional Subtitle",
//    "author":     "Author Name",
//    "book_url":   "https://...",
//    "bonus":      true          // optional, default true
//  }
//  Response: { "job_id": "wh_...", "status": "processing", "poll_url": "..." }
//
//  Step 2 — Poll until done:
//  GET /webhook.php?job=JOB_ID
//  Response (processing): { "status": "processing" }
//  Response (done):        { "status": "done", "project_id": "...",
//                            "pdf_url": "...", "epub_url": "...",
//                            "pdf_filename": "...", "epub_filename": "...",
//                            "expires_at": "..." }
//  Response (failed):      { "status": "failed", "error": "..." }
//
//  Optional auth: X-Webhook-Secret header (set WEBHOOK_SECRET in config.php)
// ════════════════════════════════════════════════════════════════════════════

require __DIR__ . '/config.php';

header('Content-Type: application/json; charset=utf-8');

function json_err(int $code, string $msg, array $extra = []): void {
    http_response_code($code);
    echo json_encode(array_merge(['error' => $msg], $extra), JSON_UNESCAPED_UNICODE);
    exit;
}

// ── Auth (optional) ───────────────────────────────────────────────────────────
if (defined('WEBHOOK_SECRET') && WEBHOOK_SECRET !== '') {
    $incoming = $_SERVER['HTTP_X_WEBHOOK_SECRET'] ?? '';
    if (!hash_equals(WEBHOOK_SECRET, $incoming)) {
        json_err(401, 'Unauthorized: invalid or missing X-Webhook-Secret header.');
    }
}

// ══════════════════════════════════════════════════════════════════════════════
//  GET /webhook.php?job=JOB_ID  →  poll job status
// ══════════════════════════════════════════════════════════════════════════════
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $job = preg_replace('/[^\w\-]/', '', $_GET['job'] ?? '');
    if (empty($job)) json_err(400, 'Missing job parameter.');

    $job_dir    = TMP_BASE . '/' . $job;
    $status_file = $job_dir . '/status.json';

    if (!is_dir($job_dir)) json_err(404, 'Job not found or already expired.');
    if (!is_file($status_file)) {
        echo json_encode(['status' => 'processing']);
        exit;
    }

    $status = json_decode(file_get_contents($status_file), true);
    echo json_encode($status, JSON_UNESCAPED_UNICODE);
    exit;
}

// ══════════════════════════════════════════════════════════════════════════════
//  POST /webhook.php  →  start job (returns immediately)
// ══════════════════════════════════════════════════════════════════════════════
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    json_err(405, 'Method not allowed. Use POST to start a job or GET?job=ID to poll.');
}

$raw  = file_get_contents('php://input');
$data = json_decode($raw, true);
if (!is_array($data)) json_err(400, 'Invalid JSON body.');

$project_id = trim($data['project_id'] ?? '');
$title      = trim($data['title']      ?? '');
$author     = trim($data['author']     ?? '');
$subtitle   = trim($data['subtitle']   ?? '');
$no_bonus   = isset($data['bonus']) && $data['bonus'] === false;
$book_url   = trim($data['book_url']   ?? '');

if (empty($project_id)) json_err(400, 'Missing required field: project_id.');
if (empty($title))      json_err(400, 'Missing required field: title.');
if (empty($author))     json_err(400, 'Missing required field: author.');
if (empty($book_url))   json_err(400, 'Missing required field: book_url.');

$allowed_prefixes = ['http://', 'https://', 's3://'];
$url_ok = false;
foreach ($allowed_prefixes as $p) {
    if (str_starts_with($book_url, $p)) { $url_ok = true; break; }
}
if (!$url_ok) json_err(400, 'book_url must start with http://, https://, or s3://');

// ── Create job directory ──────────────────────────────────────────────────────
$job_id     = 'wh_' . preg_replace('/[^\w-]/', '_', $project_id) . '_' . bin2hex(random_bytes(3));
$job_dir    = TMP_BASE . '/' . $job_id;
$output_dir = $job_dir . '/output';

if (!mkdir($output_dir, 0755, true)) {
    json_err(500, 'Failed to create job directory.');
}

// ── Detect base URL for poll_url / download URLs ──────────────────────────────
if (defined('SERVER_BASE_URL') && SERVER_BASE_URL !== '') {
    $base_url = rtrim(SERVER_BASE_URL, '/');
} else {
    $scheme   = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    $host     = $_SERVER['HTTP_HOST'] ?? 'localhost';
    $dir      = rtrim(dirname($_SERVER['SCRIPT_NAME']), '/');
    $base_url = $scheme . '://' . $host . $dir;
}

$poll_url = $base_url . '/webhook.php?job=' . urlencode($job_id);

// ── Build Python command ──────────────────────────────────────────────────────
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

// ── Write a runner script into the job dir, then exec it ─────────────────────
// Why a file instead of a one-liner?  On Ubuntu, Apache uses PrivateTmp, so
// /tmp as seen by PHP is really /tmp/systemd-private-…/tmp/.  A nohup'd shell
// runs *outside* that namespace and can't see the job dir.  By writing a PHP
// runner script *inside* the job dir (which PHP can do) and launching it with
// the PHP CLI binary, the child process inherits the same tmp view.
$status_file = $job_dir . '/status.json';
$log_file    = $job_dir . '/generator.log';
$expires_at  = gmdate('c', time() + (FILE_TTL_HOURS * 3600));
$dl_base     = $base_url . '/download.php?job=' . urlencode($job_id);
$python_cmd  = implode(' ', $cmd_parts);

$runner_script = $job_dir . '/runner.php';
$runner_code = '<?php' . "\n"
    . '$cmd = ' . var_export($python_cmd . ' 2>&1', true) . ";\n"
    . '$log = ' . var_export($log_file, true) . ";\n"
    . '$sf  = ' . var_export($status_file, true) . ";\n"
    . '$od  = ' . var_export($output_dir, true) . ";\n"
    . '$pid = ' . var_export($project_id, true) . ";\n"
    . '$dlb = ' . var_export($dl_base, true) . ";\n"
    . '$exp = ' . var_export($expires_at, true) . ";\n"
    . <<<'RUNNER'

$out = []; $ec = 0;
exec($cmd, $out, $ec);
file_put_contents($log, implode("\n", $out));

$pdf = null; $epub = null;
if (is_dir($od)) {
    foreach (scandir($od) as $f) {
        $fp = $od . '/' . $f;
        if (str_ends_with($f, '_paperback.pdf') && is_file($fp)) $pdf = $f;
        if (str_ends_with($f, '.epub')           && is_file($fp)) $epub = $f;
    }
}

if ($ec === 0 && ($pdf || $epub)) {
    $s = [
        'status'        => 'done',
        'project_id'    => $pid,
        'pdf_url'       => $pdf  ? $dlb . '&type=pdf'  : null,
        'epub_url'      => $epub ? $dlb . '&type=epub' : null,
        'pdf_filename'  => $pdf,
        'epub_filename' => $epub,
        'expires_at'    => $exp,
    ];
} else {
    $s = [
        'status' => 'failed',
        'error'  => 'Generation failed (exit ' . $ec . '). See log.',
        'log'    => implode("\n", array_slice($out, -30)),
    ];
}
file_put_contents($sf, json_encode($s, JSON_UNESCAPED_UNICODE));
RUNNER;

file_put_contents($runner_script, $runner_code);
chmod($runner_script, 0755);

// Launch the runner in the background — it inherits Apache's private tmp view.
// Double-fork via a shell so the grandchild is adopted by init (PID 1),
// which reaps it immediately when done — prevents zombie accumulation.
$php_bin = PHP_BINARY ?: '/usr/bin/php';
$cmd = $php_bin . ' ' . escapeshellarg($runner_script) . ' > /dev/null 2>&1';
exec("sh -c '(" . addslashes($cmd) . ") &' > /dev/null 2>&1 &");

// ── Respond immediately ───────────────────────────────────────────────────────
echo json_encode([
    'job_id'     => $job_id,
    'project_id' => $project_id,
    'status'     => 'processing',
    'poll_url'   => $poll_url,
], JSON_UNESCAPED_UNICODE);

// ── Cleanup old expired jobs (opportunistic) ──────────────────────────────────
if (is_dir(TMP_BASE)) {
    $ttl = FILE_TTL_HOURS * 3600;
    foreach (scandir(TMP_BASE) as $dir) {
        if (!str_starts_with($dir, 'wh_')) continue;
        $path = TMP_BASE . '/' . $dir;
        if (is_dir($path) && (time() - filemtime($path)) > $ttl) {
            _wh_rmdir($path);
        }
    }
}

function _wh_rmdir(string $dir): void {
    if (!is_dir($dir)) return;
    foreach (scandir($dir) as $item) {
        if ($item === '.' || $item === '..') continue;
        $path = $dir . '/' . $item;
        is_dir($path) ? _wh_rmdir($path) : unlink($path);
    }
    rmdir($dir);
}
