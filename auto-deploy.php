<?php
// ── Auto-deploy webhook ──────────────────────────────────────
// Called by GitHub on every push to main branch.
// Pulls latest code and syncs files to the web root.

define('DEPLOY_SECRET', getenv('DEPLOY_SECRET') ?: (defined('WEBHOOK_SECRET') ? WEBHOOK_SECRET : ''));
define('REPO_DIR',      '/tmp/book-gen-deploy');
define('WEB_DIR',       __DIR__);
define('BRANCH',        'main');
define('REPO_URL',      'https://github.com/n1227snowpro/book-generator.git');
define('LOG_FILE',      '/tmp/book-gen-autodeploy.log');

header('Content-Type: application/json');

function log_msg(string $msg): void {
    $line = '[' . date('Y-m-d H:i:s') . '] ' . $msg . PHP_EOL;
    file_put_contents(LOG_FILE, $line, FILE_APPEND);
}

function respond(bool $ok, string $msg, int $code = 200): void {
    http_response_code($code);
    echo json_encode(['success' => $ok, 'message' => $msg]);
    exit;
}

// ── Verify GitHub signature ───────────────────────────────────
$secret = DEPLOY_SECRET;
if (!empty($secret)) {
    $sig = $_SERVER['HTTP_X_HUB_SIGNATURE_256'] ?? '';
    $payload = file_get_contents('php://input');
    $expected = 'sha256=' . hash_hmac('sha256', $payload, $secret);
    if (!hash_equals($expected, $sig)) {
        log_msg('ERROR: Invalid signature');
        respond(false, 'Unauthorized', 401);
    }
} else {
    $payload = file_get_contents('php://input');
}

// ── Only deploy on push to main ───────────────────────────────
$data = json_decode($payload, true);
$ref  = $data['ref'] ?? '';
if ($ref && $ref !== 'refs/heads/' . BRANCH) {
    respond(true, 'Ignored: not main branch');
}

log_msg('Deploy triggered by push to ' . ($ref ?: 'unknown'));

// ── Pull latest code ──────────────────────────────────────────
$cmds = [];

if (is_dir(REPO_DIR . '/.git')) {
    $cmds[] = 'cd ' . REPO_DIR . ' && git fetch origin && git reset --hard origin/' . BRANCH;
} else {
    @mkdir(REPO_DIR, 0755, true);
    $cmds[] = 'git clone --depth=1 --branch=' . BRANCH . ' ' . REPO_URL . ' ' . REPO_DIR;
}

// ── Sync files (never overwrite config.php) ───────────────────
$files = ['book_generator_2.py', 'index.php', 'webhook.php', 'download.php', 'decoration.png'];
foreach ($files as $f) {
    $cmds[] = 'cp ' . REPO_DIR . '/' . $f . ' ' . WEB_DIR . '/' . $f;
}

$log = [];
foreach ($cmds as $cmd) {
    $output = [];
    $code   = 0;
    exec($cmd . ' 2>&1', $output, $code);
    $line = ($code === 0 ? 'OK' : 'FAIL') . ': ' . $cmd;
    $log[] = $line;
    log_msg($line);
    if ($code !== 0) {
        log_msg('Output: ' . implode(' | ', $output));
        respond(false, 'Deploy failed at: ' . $cmd, 500);
    }
}

// ── Fix permissions ───────────────────────────────────────────
exec('chown -R www-data:www-data ' . WEB_DIR . ' 2>&1');
exec('chmod -R 755 ' . WEB_DIR . ' 2>&1');

log_msg('Deploy complete');
respond(true, 'Deployed successfully');
