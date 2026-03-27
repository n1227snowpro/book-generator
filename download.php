<?php
// ════════════════════════════════════════════════════════════════════════════
//  Book Generator — File Download Endpoint
//
//  GET /download.php?job=JOB_ID&type=pdf
//  GET /download.php?job=JOB_ID&type=epub
//
//  Files expire after FILE_TTL_HOURS (configured in config.php).
//  Each file can be downloaded multiple times within that window.
// ════════════════════════════════════════════════════════════════════════════

require __DIR__ . '/config.php';

function dl_error(int $code, string $msg): void {
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode(['error' => $msg]);
    exit;
}

// ── Validate params ───────────────────────────────────────────────────────────
$job  = preg_replace('/[^\w\-]/', '', $_GET['job']  ?? '');
$type = strtolower(trim($_GET['type'] ?? ''));

if (empty($job))                          dl_error(400, 'Missing job parameter.');
if (!in_array($type, ['pdf', 'epub']))    dl_error(400, 'type must be "pdf" or "epub".');

// ── Locate job directory ──────────────────────────────────────────────────────
$job_dir    = TMP_BASE . '/' . $job;
$output_dir = $job_dir . '/output';

if (!is_dir($output_dir))                 dl_error(404, 'Job not found or already expired.');

// ── Check TTL ─────────────────────────────────────────────────────────────────
$ttl = defined('FILE_TTL_HOURS') ? FILE_TTL_HOURS * 3600 : 86400;
if ((time() - filemtime($job_dir)) > $ttl) {
    // Expired — clean up and return 410 Gone
    _dl_rmdir($job_dir);
    dl_error(410, 'Download link has expired. Please regenerate the file.');
}

// ── Find the requested file ───────────────────────────────────────────────────
$target = null;
foreach (scandir($output_dir) as $file) {
    $fp = $output_dir . '/' . $file;
    if (!is_file($fp)) continue;
    if ($type === 'pdf'  && str_ends_with($file, '_paperback.pdf')) { $target = $fp; break; }
    if ($type === 'epub' && str_ends_with($file, '.epub'))           { $target = $fp; break; }
}

if (!$target || !is_file($target))        dl_error(404, "No $type file found for this job.");

// ── Stream the file ───────────────────────────────────────────────────────────
$filename  = basename($target);
$mime_type = $type === 'pdf' ? 'application/pdf' : 'application/epub+zip';
$file_size = filesize($target);

header('Content-Type: '        . $mime_type);
header('Content-Disposition: attachment; filename="' . $filename . '"');
header('Content-Length: '      . $file_size);
header('Cache-Control: private, no-store');
header('X-Job-Id: '            . $job);

readfile($target);
exit;

// ── Utility ───────────────────────────────────────────────────────────────────
function _dl_rmdir(string $dir): void {
    if (!is_dir($dir)) return;
    foreach (scandir($dir) as $item) {
        if ($item === '.' || $item === '..') continue;
        $path = $dir . '/' . $item;
        is_dir($path) ? _dl_rmdir($path) : unlink($path);
    }
    rmdir($dir);
}
