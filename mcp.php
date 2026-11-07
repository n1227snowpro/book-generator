<?php
/*  mcp.php — MCP Streamable HTTP server for the Book Generator.
    Transport: JSON-RPC 2.0 over POST, responses as application/json.
    Auth: Bearer token or ?key= query param, compared to BOOKGEN_MCP_TOKEN
    from env or /etc/bookgen/mcp_token (file outside the web root).
    Protocol: speaks 2025-06-18 and echoes back older client versions. */

declare(strict_types=1);
require __DIR__ . '/config.php';

// ── Token (env var, then /etc/bookgen/mcp_token) ──────────────────────────────
function mcp_token(): ?string {
    $t = getenv('BOOKGEN_MCP_TOKEN');
    if (is_string($t) && $t !== '') return trim($t);
    $f = '/etc/bookgen/mcp_token';
    if (is_readable($f)) {
        $v = trim((string)file_get_contents($f));
        if ($v !== '') return $v;
    }
    return null;
}

function presented_token(): ?string {
    // Apache often strips Authorization — check every surface it can leak through.
    $sources = [
        $_SERVER['HTTP_AUTHORIZATION'] ?? '',
        $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '',
    ];
    if (function_exists('getallheaders')) {
        foreach (getallheaders() as $k => $v) {
            if (strcasecmp($k, 'Authorization') === 0) $sources[] = (string)$v;
        }
    }
    foreach ($sources as $h) {
        if (is_string($h) && stripos($h, 'bearer ') === 0) return trim(substr($h, 7));
    }
    if (isset($_GET['key']) && is_string($_GET['key']) && $_GET['key'] !== '') return $_GET['key'];
    return null;
}

// Protocol version Claude sent, so we can echo it back
$CLIENT_PROTOCOL = null;

// ── JSON-RPC helpers ──────────────────────────────────────────────────────────
function rpc_result($id, $result): array {
    return ['jsonrpc' => '2.0', 'id' => $id, 'result' => $result];
}
function rpc_error($id, int $code, string $msg, $data = null): array {
    $err = ['code' => $code, 'message' => $msg];
    if ($data !== null) $err['data'] = $data;
    return ['jsonrpc' => '2.0', 'id' => $id, 'error' => $err];
}
function text_content(string $s): array {
    return ['content' => [['type' => 'text', 'text' => $s]]];
}
function json_as_text(array $data): array {
    return text_content(json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
}

// ── Job dir helpers ───────────────────────────────────────────────────────────
function valid_job_id(string $j): bool {
    // wh_<project_sanitized>_<6 hex>; allow letters/digits/_-
    return (bool)preg_match('/^wh_[\w-]+_[0-9a-fA-F]{6,}$/', $j);
}

function job_dir(string $j): ?string {
    if (!valid_job_id($j)) return null;
    $p = realpath(TMP_BASE . '/' . $j);
    $root = realpath(TMP_BASE);
    if ($p === false || $root === false) return null;
    return (strpos($p, $root . DIRECTORY_SEPARATOR) === 0 || $p === $root) ? $p : null;
}

function base_url(): string {
    if (defined('SERVER_BASE_URL') && SERVER_BASE_URL !== '') return rtrim(SERVER_BASE_URL, '/');
    $scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    $host   = $_SERVER['HTTP_HOST'] ?? 'localhost';
    $dir    = rtrim(dirname($_SERVER['SCRIPT_NAME']), '/');
    return $scheme . '://' . $host . $dir;
}

function read_status(string $dir): array {
    $sf = $dir . '/status.json';
    if (!is_file($sf)) return ['status' => 'processing'];
    $d = json_decode((string)file_get_contents($sf), true);
    return is_array($d) ? $d : ['status' => 'processing'];
}

function read_job_meta(string $dir): array {
    $f = $dir . '/job.json';
    if (!is_file($f)) return [];
    $d = json_decode((string)file_get_contents($f), true);
    return is_array($d) ? $d : [];
}

function urls_for(string $job): array {
    $b = base_url();
    return [
        'pdf_url'  => $b . '/download.php?job=' . urlencode($job) . '&type=pdf',
        'epub_url' => $b . '/download.php?job=' . urlencode($job) . '&type=epub',
        'poll_url' => $b . '/webhook.php?job=' . urlencode($job),
    ];
}

// ── Run mcp_tools.py ──────────────────────────────────────────────────────────
function run_py(array $argv, int $timeout = 1800): array {
    $cmd = escapeshellarg('/usr/bin/python3') . ' ' . escapeshellarg(__DIR__ . '/mcp_tools.py');
    foreach ($argv as $a) $cmd .= ' ' . escapeshellarg((string)$a);
    $desc = [1 => ['pipe','w'], 2 => ['pipe','w']];
    $proc = proc_open($cmd . ' 2>&1', $desc, $pipes, __DIR__);
    if (!is_resource($proc)) return ['error' => 'could not start python'];
    stream_set_blocking($pipes[1], false);
    $out = ''; $start = time();
    while (true) {
        $chunk = fread($pipes[1], 65536);
        if ($chunk !== '' && $chunk !== false) $out .= $chunk;
        $st = proc_get_status($proc);
        if (!$st['running']) {
            $out .= stream_get_contents($pipes[1]);
            break;
        }
        if (time() - $start > $timeout) { proc_terminate($proc); break; }
        usleep(50000);
    }
    foreach ($pipes as $p) @fclose($p);
    @proc_close($proc);
    $out = trim($out);
    $decoded = json_decode($out, true);
    return is_array($decoded) ? $decoded : ['error' => 'python tool failed', 'raw' => substr($out, 0, 2000)];
}

// ─────────────────────────────────────────────────────────────────────────────
// Tool implementations
// ─────────────────────────────────────────────────────────────────────────────

function tool_generate_book(array $args): array {
    foreach (['title', 'author', 'manuscript_url'] as $k) {
        if (empty($args[$k]) || !is_string($args[$k])) return json_as_text(['error' => "missing: $k"]);
    }
    $body = [
        'project_id'   => $args['project_id'] ?? ('mcp_' . bin2hex(random_bytes(3))),
        'title'        => (string)$args['title'],
        'subtitle'     => (string)($args['subtitle'] ?? ''),
        'author'       => (string)$args['author'],
        'book_url'     => (string)$args['manuscript_url'],
        'bonus_type'   => in_array(($args['bonus_page'] ?? 'prayer'),
                                   ['prayer','affirmation','none'], true)
                          ? $args['bonus_page'] : 'prayer',
        'toc'          => !empty($args['include_toc']),
        'para_spacing' => isset($args['paragraph_spacing']) ? (int)$args['paragraph_spacing'] : 14,
    ];
    if ($body['bonus_type'] === 'none') $body['bonus'] = false;

    // POST to our own webhook.php (keeps all its existing logic).
    // Forward X-Webhook-Secret from config.php when set, so webhook.php's
    // own auth accepts us. The secret is never put into tool output.
    $url     = base_url() . '/webhook.php';
    $headers = ['Content-Type: application/json'];
    if (defined('WEBHOOK_SECRET') && WEBHOOK_SECRET !== '') {
        $headers[] = 'X-Webhook-Secret: ' . WEBHOOK_SECRET;
    }
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => json_encode($body, JSON_UNESCAPED_SLASHES),
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 30,
    ]);
    $resp = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($code < 200 || $code >= 300) {
        return json_as_text(['error' => "webhook returned HTTP $code", 'body' => $resp]);
    }
    $data = json_decode((string)$resp, true);
    if (!is_array($data) || empty($data['job_id'])) {
        return json_as_text(['error' => 'webhook did not return a job_id', 'body' => $resp]);
    }
    $job = $data['job_id'];
    return json_as_text(array_merge($data, urls_for($job)));
}

function tool_get_job(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $dir = job_dir($job);
    if ($dir === null) return json_as_text(['error' => 'bad job_id']);
    if (!is_dir($dir)) return json_as_text(['error' => 'job not found']);
    $s = read_status($dir);
    $m = read_job_meta($dir);
    $created = @filemtime($dir);
    // page count (if PDF exists, read ReportLab's /Count)
    $pages = null;
    $out_dir = $dir . '/output';
    if (is_dir($out_dir)) {
        foreach (scandir($out_dir) as $f) {
            if (str_ends_with($f, '_paperback.pdf')) {
                $bytes = @file_get_contents($out_dir . '/' . $f, false, null, 0, 1024 * 128);
                if ($bytes !== false && preg_match('/\/Type\s*\/Pages[^>]*\/Count\s+(\d+)/', $bytes, $mm)) {
                    $pages = (int)$mm[1];
                }
                break;
            }
        }
    }
    $has_docx = is_file($dir . '/source.docx');
    return json_as_text(array_merge([
        'job_id'    => $job,
        'status'    => $s['status'] ?? 'unknown',
        'error'     => $s['error'] ?? null,
        'created'   => $created ? gmdate('c', $created) : null,
        'title'     => $m['title'] ?? null,
        'page_count'=> $pages,
        'editable'  => $has_docx,
    ], urls_for($job), [
        'docx_url' => $has_docx ? (base_url() . '/download.php?job=' . urlencode($job) . '&type=docx') : null,
    ]));
}

function tool_list_jobs(array $args): array {
    $limit = max(1, min(50, (int)($args['limit'] ?? 20)));
    $q = strtolower(trim((string)($args['query'] ?? '')));
    if (!is_dir(TMP_BASE)) return json_as_text(['jobs' => []]);
    $rows = [];
    foreach (scandir(TMP_BASE) as $d) {
        if (!str_starts_with($d, 'wh_')) continue;
        $full = TMP_BASE . '/' . $d;
        if (!is_dir($full)) continue;
        $s = read_status($full);
        $m = read_job_meta($full);
        $row = [
            'job_id'  => $d,
            'status'  => $s['status'] ?? 'unknown',
            'title'   => $m['title'] ?? null,
            'created' => gmdate('c', (int)@filemtime($full)),
        ];
        if ($q !== '' && !($row['title'] && str_contains(strtolower($row['title']), $q))) continue;
        $rows[] = $row;
    }
    usort($rows, fn($a, $b) => strcmp($b['created'], $a['created']));
    return json_as_text(['jobs' => array_slice($rows, 0, $limit), 'total_shown' => min(count($rows), $limit)]);
}

function tool_qc_job(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $dir = job_dir($job);
    if ($dir === null || !is_dir($dir)) return json_as_text(['error' => 'bad or unknown job_id']);
    $res = run_py(['qc', $dir]);
    return json_as_text($res);
}

function tool_get_entry(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $day = (string)($args['day'] ?? '');
    $dir = job_dir($job);
    if ($dir === null || !is_dir($dir)) return json_as_text(['error' => 'bad or unknown job_id']);
    if ($day === '') return json_as_text(['error' => 'missing: day']);
    $res = run_py(['entry', $dir, $day]);
    return json_as_text($res);
}

function tool_repair_entry(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $day = (string)($args['day'] ?? '');
    $act = (string)($args['action'] ?? '');
    $dir = job_dir($job);
    if ($dir === null || !is_dir($dir)) return json_as_text(['error' => 'bad or unknown job_id']);
    if ($day === '' || $act === '') return json_as_text(['error' => 'missing: day or action']);
    if (!in_array($act, ['truncate_junk', 'replace', 'delete'], true)) {
        return json_as_text(['error' => "action must be truncate_junk | replace | delete"]);
    }
    $payload = ['content' => $args['content'] ?? null];
    $res = run_py(['repair', $dir, $day, $act, json_encode($payload, JSON_UNESCAPED_UNICODE)]);
    return json_as_text($res);
}

function tool_insert_entry(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $dir = job_dir($job);
    if ($dir === null || !is_dir($dir)) return json_as_text(['error' => 'bad or unknown job_id']);
    $required = ['after_day','heading','verse_text','verse_ref',
                 'paragraph1_before_bold','bold_sentence','paragraph1_after_bold',
                 'paragraph2','prayer'];
    foreach ($required as $k) {
        if (empty($args[$k]) || !is_string($args[$k])) return json_as_text(['error' => "missing: $k"]);
    }
    $payload_file = $dir . '/mcp_insert_' . bin2hex(random_bytes(3)) . '.json';
    file_put_contents($payload_file, json_encode($args, JSON_UNESCAPED_UNICODE));
    $res = run_py(['insert', $dir, $payload_file]);
    @unlink($payload_file);
    return json_as_text($res);
}

function tool_rebuild_job(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $dir = job_dir($job);
    if ($dir === null || !is_dir($dir)) return json_as_text(['error' => 'bad or unknown job_id']);
    $res = run_py(['rebuild', $dir]);
    if (!empty($res['ok'])) $res = array_merge($res, urls_for($job));
    return json_as_text($res);
}

function tool_import_source(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $url = (string)($args['manuscript_url'] ?? '');
    $dir = job_dir($job);
    if ($dir === null || !is_dir($dir)) return json_as_text(['error' => 'bad or unknown job_id']);
    if ($url === '' || !preg_match('#^https?://#i', $url))
        return json_as_text(['error' => 'manuscript_url must be an http(s) URL']);

    $dest = $dir . '/source.docx';
    if (is_file($dest) && empty($args['overwrite']))
        return json_as_text(['ok' => true, 'note' => 'source.docx already present; pass overwrite=true to replace',
                             'editable' => true]);

    $tmp = $dest . '.part';
    $fh  = fopen($tmp, 'wb');
    if (!$fh) return json_as_text(['error' => 'cannot write to job dir']);
    $ch  = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_FILE           => $fh,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 5,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_TIMEOUT        => 300,
        CURLOPT_FAILONERROR    => true,
    ]);
    $ok   = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err  = curl_error($ch);
    curl_close($ch);
    fclose($fh);
    if (!$ok) { @unlink($tmp); return json_as_text(['error' => "download failed (HTTP $code): $err"]); }

    // Sanity check: DOCX is a ZIP, first bytes must be "PK"
    $head = file_get_contents($tmp, false, null, 0, 4);
    if ($head === false || substr($head, 0, 2) !== 'PK') {
        @unlink($tmp);
        return json_as_text(['error' => 'downloaded file is not a .docx (ZIP header missing)']);
    }
    rename($tmp, $dest);

    // Make sure job.json exists so rebuild_job can run. If missing, reconstruct
    // what we can from status.json + the output filename.
    $meta_path = $dir . '/job.json';
    if (!is_file($meta_path)) {
        $status = read_status($dir);
        $title  = null; $suffix_pdf = '_paperback.pdf';
        $out_dir = $dir . '/output';
        if (is_dir($out_dir)) {
            foreach (scandir($out_dir) as $f) {
                if (str_ends_with($f, $suffix_pdf)) {
                    $title = str_replace('_', ' ', substr($f, 0, -strlen($suffix_pdf)));
                    break;
                }
            }
        }
        file_put_contents($meta_path, json_encode([
            'project_id'   => $status['project_id'] ?? substr($job, 3, 36),
            'title'        => $title ?? $job,
            'subtitle'     => '',
            'author'       => (string)($args['author'] ?? 'Author'),
            'book_url'     => $url,
            'bonus_type'   => (string)($args['bonus_type'] ?? 'prayer'),
            'include_toc'  => (bool)($args['include_toc'] ?? false),
            'para_spacing' => isset($args['para_spacing']) ? (int)$args['para_spacing'] : 14,
            'created_at'   => gmdate('c'),
            'reconstructed'=> true,
        ], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    }

    return json_as_text([
        'ok' => true,
        'job_id' => $job,
        'editable' => true,
        'size_bytes' => filesize($dest),
        'job_json_reconstructed' => !is_file($meta_path) ? false : (json_decode(file_get_contents($meta_path), true)['reconstructed'] ?? false),
    ]);
}

function tool_notify_app(array $args): array {
    $job = (string)($args['job_id'] ?? '');
    $pid = (string)($args['project_id'] ?? '');
    $dir = job_dir($job);
    if ($dir === null || !is_dir($dir)) return json_as_text(['error' => 'bad or unknown job_id']);
    if ($pid === '') return json_as_text(['error' => 'missing: project_id']);
    $u = urls_for($job);
    $body = [
        'project_id'    => $pid,
        'book_pdf_url'  => $u['pdf_url'],
        'book_epub_url' => $u['epub_url'],
        'book_docx_url' => base_url() . '/download.php?job=' . urlencode($job) . '&type=docx',
    ];
    $ch = curl_init('https://zxmgubwhksldxctdrlfs.supabase.co/functions/v1/book-callback');
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => json_encode($body, JSON_UNESCAPED_SLASHES),
        CURLOPT_HTTPHEADER     => ['Content-Type: application/json'],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 20,
    ]);
    $resp = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return json_as_text(['http_code' => $code, 'response' => $resp]);
}

// ─────────────────────────────────────────────────────────────────────────────
// Tool catalogue (for tools/list)
// ─────────────────────────────────────────────────────────────────────────────
$TOOLS = [
  [
    'name' => 'generate_book',
    'description' => 'Start a new book generation job and return the job id and download URLs.',
    'inputSchema' => [
        'type' => 'object',
        'properties' => [
            'title'             => ['type' => 'string'],
            'subtitle'          => ['type' => 'string'],
            'author'            => ['type' => 'string'],
            'manuscript_url'    => ['type' => 'string', 'description' => 'http(s) URL to the .docx manuscript'],
            'paragraph_spacing' => ['type' => 'integer', 'default' => 14],
            'bonus_page'        => ['type' => 'string', 'enum' => ['prayer','affirmation','none'], 'default' => 'prayer'],
            'include_toc'       => ['type' => 'boolean', 'default' => false],
            'repo_name'         => ['type' => 'string'],
            'project_id'        => ['type' => 'string'],
        ],
        'required' => ['title', 'author', 'manuscript_url'],
    ],
  ],
  ['name' => 'get_job', 'description' => 'Get the status, page count, title and download URLs for a job.',
   'inputSchema' => ['type' => 'object', 'properties' => ['job_id' => ['type' => 'string']], 'required' => ['job_id']]],
  ['name' => 'list_jobs', 'description' => 'List recent jobs newest first. Filter by title with query.',
   'inputSchema' => ['type' => 'object', 'properties' => [
      'limit' => ['type' => 'integer', 'default' => 20, 'minimum' => 1, 'maximum' => 50],
      'query' => ['type' => 'string'],
   ]]],
  ['name' => 'qc_job', 'description' => 'Quality-check a book: missing/duplicate/out-of-order days, JSON leaks, model chatter, duplicate prayer headings.',
   'inputSchema' => ['type' => 'object', 'properties' => ['job_id' => ['type' => 'string']], 'required' => ['job_id']]],
  ['name' => 'get_entry', 'description' => 'Fetch a single day\'s text (e.g. day = "January 26").',
   'inputSchema' => ['type' => 'object', 'properties' => [
      'job_id' => ['type' => 'string'], 'day' => ['type' => 'string'],
   ], 'required' => ['job_id', 'day']]],
  ['name' => 'repair_entry', 'description' => 'Repair one day\'s entry: truncate_junk | replace | delete. For replace, pass content as array of paragraphs (first may be the heading).',
   'inputSchema' => ['type' => 'object', 'properties' => [
      'job_id' => ['type' => 'string'],
      'day'    => ['type' => 'string'],
      'action' => ['type' => 'string', 'enum' => ['truncate_junk','replace','delete']],
      'content'=> ['type' => 'array', 'items' => ['type' => 'string']],
   ], 'required' => ['job_id','day','action']]],
  ['name' => 'insert_entry', 'description' => 'Insert a new day entry after after_day. Keeps the standard formatting: italic verse + " — Ref (WEB)", one bold sentence inside paragraph 1, a bold "The Prayer of the Day" heading, then a one-sentence prayer.',
   'inputSchema' => ['type' => 'object', 'properties' => array_fill_keys(
      ['after_day','heading','verse_text','verse_ref','paragraph1_before_bold',
       'bold_sentence','paragraph1_after_bold','paragraph2','prayer'],
      ['type' => 'string']),
      'required' => ['after_day','heading','verse_text','verse_ref',
                     'paragraph1_before_bold','bold_sentence','paragraph1_after_bold',
                     'paragraph2','prayer']]],
  ['name' => 'rebuild_job', 'description' => 'Rebuild PDF + EPUB from the (possibly edited) source.docx using the job\'s original settings. Keeps the same job_id and URLs.',
   'inputSchema' => ['type' => 'object', 'properties' => ['job_id' => ['type' => 'string']], 'required' => ['job_id']]],
  ['name' => 'import_source', 'description' => 'For an existing job that has no source.docx (older jobs), download the manuscript from manuscript_url, store it as the job\'s source.docx, and mark the job editable. Optional fields (author, bonus_type, include_toc, para_spacing) only matter when the job also has no job.json and must be reconstructed. Pass overwrite=true to replace an existing source.docx.',
   'inputSchema' => ['type' => 'object', 'properties' => [
      'job_id'         => ['type' => 'string'],
      'manuscript_url' => ['type' => 'string'],
      'overwrite'      => ['type' => 'boolean', 'default' => false],
      'author'         => ['type' => 'string'],
      'bonus_type'     => ['type' => 'string', 'enum' => ['prayer','affirmation','none']],
      'include_toc'    => ['type' => 'boolean'],
      'para_spacing'   => ['type' => 'integer'],
   ], 'required' => ['job_id', 'manuscript_url']]],
  ['name' => 'notify_app', 'description' => 'POST the job\'s download URLs to the Supabase book-callback (the same payload n8n sends).',
   'inputSchema' => ['type' => 'object', 'properties' => [
      'job_id' => ['type' => 'string'], 'project_id' => ['type' => 'string'],
   ], 'required' => ['job_id','project_id']]],
];

function dispatch_tool(string $name, array $args): array {
    switch ($name) {
        case 'generate_book': return tool_generate_book($args);
        case 'get_job':       return tool_get_job($args);
        case 'list_jobs':     return tool_list_jobs($args);
        case 'qc_job':        return tool_qc_job($args);
        case 'get_entry':     return tool_get_entry($args);
        case 'repair_entry':  return tool_repair_entry($args);
        case 'insert_entry':  return tool_insert_entry($args);
        case 'rebuild_job':   return tool_rebuild_job($args);
        case 'import_source': return tool_import_source($args);
        case 'notify_app':    return tool_notify_app($args);
    }
    return json_as_text(['error' => "unknown tool: $name"]);
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────
header('Content-Type: application/json; charset=utf-8');

// Auth
$expected = mcp_token();
if ($expected === null) {
    http_response_code(500);
    echo json_encode(['error' => 'server misconfigured: BOOKGEN_MCP_TOKEN not set']);
    exit;
}
$given = presented_token();
if ($given === null || !hash_equals($expected, $given)) {
    http_response_code(401);
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

// GET (health probe for the user / Claude connector add page)
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    echo json_encode([
        'name' => 'bookgen-mcp',
        'transport' => 'streamable-http',
        'protocolVersion' => '2025-06-18',
        'ok' => true,
    ]);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'method not allowed']);
    exit;
}

$raw = file_get_contents('php://input');
$req = json_decode($raw, true);
if (!is_array($req)) {
    http_response_code(400);
    echo json_encode(rpc_error(null, -32700, 'parse error'));
    exit;
}

// Batch support (per JSON-RPC 2.0). Rare for MCP, but harmless to handle.
$is_batch = array_is_list($req) && !empty($req);
$items = $is_batch ? $req : [$req];
$out = [];

foreach ($items as $r) {
    $id = $r['id'] ?? null;
    $method = (string)($r['method'] ?? '');
    $params = $r['params'] ?? [];

    // notifications/* have no id and expect no response
    if ($method === '' || strpos($method, 'notifications/') === 0) {
        // Just ack silently
        continue;
    }

    switch ($method) {
        case 'initialize':
            $cp = $params['protocolVersion'] ?? '2025-06-18';
            $out[] = rpc_result($id, [
                'protocolVersion' => $cp,
                'capabilities' => [
                    'tools' => new stdClass(),
                ],
                'serverInfo' => [
                    'name'    => 'bookgen-mcp',
                    'version' => '0.1.0',
                ],
                'instructions' => "Book Generator MCP. Use generate_book to start a job, qc_job to inspect it, repair_entry/insert_entry to fix content, rebuild_job to re-render, notify_app to tell the app the files changed.",
            ]);
            break;

        case 'ping':
            $out[] = rpc_result($id, new stdClass());
            break;

        case 'tools/list':
            $out[] = rpc_result($id, ['tools' => $GLOBALS['TOOLS']]);
            break;

        case 'tools/call':
            $name = (string)($params['name'] ?? '');
            $args = (array)($params['arguments'] ?? []);
            try {
                $res = dispatch_tool($name, $args);
                $out[] = rpc_result($id, $res);
            } catch (Throwable $e) {
                $out[] = rpc_result($id, text_content('tool error: ' . $e->getMessage()));
            }
            break;

        default:
            $out[] = rpc_error($id, -32601, "method not found: $method");
    }
}

// If all items were notifications, nothing to send (204 is also valid)
if (empty($out)) {
    http_response_code(202);
    exit;
}

echo json_encode($is_batch ? $out : $out[0],
    JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
