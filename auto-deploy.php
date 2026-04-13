<?php
// ── Auto-deploy endpoint ──────────────────────────────────────────────────────
// Two ways to trigger:
//   1. GitHub webhook (POST, X-Hub-Signature-256 header)
//   2. Manual: GET /auto-deploy.php?token=bookgen_2026_deploy

define('DEPLOY_TOKEN',  'bookgen_2026_deploy');
define('DEPLOY_SECRET', 'bookgen_deploy_a6bd43519369e97b80a5e8ec');
define('WEB_DIR',       __DIR__);
define('BRANCH',        'main');

header('Content-Type: text/plain');

// ── Auth ──────────────────────────────────────────────────────────────────────
$manual = ($_GET['token'] ?? '') === DEPLOY_TOKEN;

if (!$manual) {
    $sig     = $_SERVER['HTTP_X_HUB_SIGNATURE_256'] ?? '';
    $payload = file_get_contents('php://input');
    $expected = 'sha256=' . hash_hmac('sha256', $payload, DEPLOY_SECRET);
    if (!hash_equals($expected, $sig)) {
        http_response_code(401);
        exit("Unauthorized\n");
    }
    // Only deploy on push to main
    $data = json_decode($payload, true);
    $ref  = $data['ref'] ?? '';
    if ($ref && $ref !== 'refs/heads/' . BRANCH) {
        exit("Ignored: not main branch\n");
    }
}

// ── Deploy: fetch + reset --hard in the web dir ───────────────────────────────
$dir = escapeshellarg(WEB_DIR);
$cmd = "cd {$dir} && git config --global --add safe.directory " . WEB_DIR
     . " && git fetch origin " . BRANCH
     . " && git reset --hard origin/" . BRANCH
     . " 2>&1";

$output = shell_exec($cmd);
$ts     = date('Y-m-d H:i:s');
file_put_contents('/tmp/deploy.log', "{$ts}\n{$output}\n", FILE_APPEND);

echo "OK\n{$output}";
