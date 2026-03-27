<?php
// ════════════════════════════════════════════════════════
//  BOOK GENERATOR — Web Interface
// ════════════════════════════════════════════════════════
require_once __DIR__ . '/config.php';

// ── Helper: GitHub API call ──────────────────────────────────────────────────
function github_api(string $method, string $url, ?array $data, string $token): array {
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_HTTPHEADER     => [
            'Authorization: token ' . $token,
            'Accept: application/vnd.github.v3+json',
            'Content-Type: application/json',
            'User-Agent: BookGeneratorWebApp/1.0',
        ],
    ]);
    if ($data !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
    }
    $body     = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ['code' => $httpCode, 'body' => json_decode($body, true) ?? []];
}

// ── Helper: Upload files to GitHub ──────────────────────────────────────────
function upload_to_github(string $repo_name, string $title, string $author, array $files): array {
    $token    = GITHUB_TOKEN;
    $username = GITHUB_USERNAME;
    $base_url = 'https://api.github.com';
    $result   = ['success' => false, 'repo_url' => '', 'uploaded' => [], 'error' => ''];

    // 1. Create the repository
    $create = github_api('POST', $base_url . '/user/repos', [
        'name'        => $repo_name,
        'description' => $title . ' by ' . $author,
        'private'     => false,
        'auto_init'   => true,
    ], $token);

    if (!in_array($create['code'], [201, 422])) {
        // 422 means repo may already exist — treat as non-fatal and continue
        $result['error'] = 'Failed to create GitHub repo (HTTP ' . $create['code'] . ')';
        return $result;
    }

    $repo_url = 'https://github.com/' . $username . '/' . $repo_name;
    $result['repo_url'] = $repo_url;

    // Wait for auto_init to settle
    sleep(2);

    // 2. Upload each file
    foreach ($files as $file_path) {
        if (!file_exists($file_path)) {
            continue;
        }
        $filename = basename($file_path);
        $content  = base64_encode(file_get_contents($file_path));

        // Check if file already exists (to get SHA for update)
        $check = github_api(
            'GET',
            $base_url . '/repos/' . $username . '/' . $repo_name . '/contents/' . $filename,
            null,
            $token
        );
        $sha = ($check['code'] === 200 && isset($check['body']['sha']))
            ? $check['body']['sha']
            : null;

        $put_data = [
            'message' => 'Add ' . $filename,
            'content' => $content,
        ];
        if ($sha !== null) {
            $put_data['sha'] = $sha;
        }

        $put = github_api(
            'PUT',
            $base_url . '/repos/' . $username . '/' . $repo_name . '/contents/' . $filename,
            $put_data,
            $token
        );

        if (in_array($put['code'], [200, 201])) {
            $result['uploaded'][] = $filename;
        }
    }

    $result['success'] = count($result['uploaded']) > 0;
    return $result;
}

// ── Download Handler ─────────────────────────────────────────────────────────
if (isset($_GET['action']) && $_GET['action'] === 'download') {
    $job_id = preg_replace('/[^a-z0-9_]/', '', strtolower($_GET['job'] ?? ''));
    $type   = $_GET['type'] ?? '';

    if (empty($job_id) || !in_array($type, ['pdf', 'epub', 'zip'])) {
        http_response_code(400);
        exit('Invalid request.');
    }

    $output_dir = TMP_BASE . '/' . $job_id . '/output';

    // Find the matching file
    $file_path = null;
    if (is_dir($output_dir)) {
        $files = scandir($output_dir);
        foreach ($files as $f) {
            $ext = strtolower(pathinfo($f, PATHINFO_EXTENSION));
            if ($type === 'pdf'  && $ext === 'pdf')  { $file_path = $output_dir . '/' . $f; break; }
            if ($type === 'epub' && $ext === 'epub') { $file_path = $output_dir . '/' . $f; break; }
            if ($type === 'zip'  && $ext === 'zip')  { $file_path = $output_dir . '/' . $f; break; }
        }
    }

    if ($file_path === null || !file_exists($file_path)) {
        http_response_code(404);
        exit('File not found.');
    }

    // Security: verify resolved path is inside TMP_BASE
    $real = realpath($file_path);
    $base = realpath(TMP_BASE);
    if ($real === false || $base === false || strpos($real, $base . DIRECTORY_SEPARATOR) !== 0) {
        http_response_code(403);
        exit('Access denied.');
    }

    $mime_map = [
        'pdf'  => 'application/pdf',
        'epub' => 'application/epub+zip',
        'zip'  => 'application/zip',
    ];

    header('Content-Type: ' . $mime_map[$type]);
    header('Content-Disposition: attachment; filename="' . basename($real) . '"');
    header('Content-Length: ' . filesize($real));
    header('Cache-Control: no-cache, must-revalidate');
    readfile($real);
    exit;
}

// ── Generate Handler (AJAX POST) ─────────────────────────────────────────────
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_SERVER['HTTP_X_REQUESTED_WITH'])) {
    header('Content-Type: application/json');

    $response = [
        'success' => false,
        'job_id'  => '',
        'pdf'     => false,
        'epub'    => false,
        'zip'     => false,
        'github'  => null,
        'log'     => '',
        'error'   => '',
    ];

    // Validate inputs
    $title    = trim($_POST['title']    ?? '');
    $author   = trim($_POST['author']   ?? '');
    $subtitle = trim($_POST['subtitle'] ?? '');
    $gh_repo  = trim($_POST['gh_repo']  ?? '');

    if (empty($title)) {
        $response['error'] = 'Book title is required.';
        echo json_encode($response);
        exit;
    }
    if (empty($author)) {
        $response['error'] = 'Author name is required.';
        echo json_encode($response);
        exit;
    }

    // Determine source: file upload or URL
    $docx_url = trim($_POST['docx_url'] ?? '');
    $use_url  = !empty($docx_url);

    if ($use_url) {
        // Basic URL sanity check
        $allowed_prefixes = ['http://', 'https://', 's3://'];
        $ok = false;
        foreach ($allowed_prefixes as $p) { if (str_starts_with($docx_url, $p)) { $ok = true; break; } }
        if (!$ok) {
            $response['error'] = 'URL must start with http://, https://, or s3://';
            echo json_encode($response); exit;
        }
    } else {
        // Validate uploaded file
        if (empty($_FILES['docx']['name']) || $_FILES['docx']['error'] !== UPLOAD_ERR_OK) {
            $upload_errors = [
                UPLOAD_ERR_INI_SIZE   => 'File exceeds server upload limit.',
                UPLOAD_ERR_FORM_SIZE  => 'File exceeds form upload limit.',
                UPLOAD_ERR_PARTIAL    => 'File was only partially uploaded.',
                UPLOAD_ERR_NO_FILE    => 'No file was uploaded.',
                UPLOAD_ERR_NO_TMP_DIR => 'Missing temp directory.',
                UPLOAD_ERR_CANT_WRITE => 'Failed to write file to disk.',
                UPLOAD_ERR_EXTENSION  => 'A PHP extension stopped the upload.',
            ];
            $err_code = $_FILES['docx']['error'] ?? UPLOAD_ERR_NO_FILE;
            $response['error'] = $upload_errors[$err_code] ?? 'Unknown upload error.';
            echo json_encode($response); exit;
        }
        $original_name = $_FILES['docx']['name'];
        $ext = strtolower(pathinfo($original_name, PATHINFO_EXTENSION));
        if (!in_array($ext, ['docx', 'doc'])) {
            $response['error'] = 'Only .docx or .doc files are accepted.';
            echo json_encode($response); exit;
        }
        $file_size_mb = $_FILES['docx']['size'] / (1024 * 1024);
        if ($file_size_mb > MAX_UPLOAD_MB) {
            $response['error'] = 'File exceeds maximum allowed size of ' . MAX_UPLOAD_MB . ' MB.';
            echo json_encode($response); exit;
        }
    }

    // Create job directory
    $job_id     = 'book_' . bin2hex(random_bytes(4));
    $job_dir    = TMP_BASE . '/' . $job_id;
    $output_dir = $job_dir . '/output';

    if (!mkdir($output_dir, 0755, true)) {
        $response['error'] = 'Failed to create job directory.';
        echo json_encode($response); exit;
    }

    // Resolve input path (upload → move file; URL → pass directly to Python)
    if ($use_url) {
        $input_arg = $docx_url;  // Python script handles download
    } else {
        $upload_path = $job_dir . '/' . $job_id . '.' . $ext;
        if (!move_uploaded_file($_FILES['docx']['tmp_name'], $upload_path)) {
            $response['error'] = 'Failed to move uploaded file.';
            echo json_encode($response); exit;
        }
        $input_arg = $upload_path;
    }

    // Build python command
    $cmd_parts = [
        escapeshellarg(PYTHON_BIN),
        escapeshellarg(GENERATOR_SCRIPT),
        '--input',   escapeshellarg($input_arg),
        '--title',   escapeshellarg($title),
        '--author',  escapeshellarg($author),
        '--out-dir', escapeshellarg($output_dir),
    ];
    if (!empty($subtitle)) {
        $cmd_parts[] = '--subtitle';
        $cmd_parts[] = escapeshellarg($subtitle);
    }
    $cmd = implode(' ', $cmd_parts) . ' 2>&1';

    // Execute
    exec($cmd, $output_lines, $exit_code);
    $log = implode("\n", $output_lines);
    $response['log'] = $log;

    // Derive expected filenames
    $safe_title = preg_replace('/[^\w\-]/', '_', $title);

    // Locate output files (fallback: scan dir for any pdf/epub)
    $pdf_path  = null;
    $epub_path = null;

    $expected_pdf  = $output_dir . '/' . $safe_title . '_paperback.pdf';
    $expected_epub = $output_dir . '/' . $safe_title . '.epub';

    if (file_exists($expected_pdf))  { $pdf_path  = $expected_pdf; }
    if (file_exists($expected_epub)) { $epub_path = $expected_epub; }

    // Fallback: scan the output directory
    if (($pdf_path === null || $epub_path === null) && is_dir($output_dir)) {
        foreach (scandir($output_dir) as $f) {
            $fext = strtolower(pathinfo($f, PATHINFO_EXTENSION));
            if ($pdf_path  === null && $fext === 'pdf')  { $pdf_path  = $output_dir . '/' . $f; }
            if ($epub_path === null && $fext === 'epub') { $epub_path = $output_dir . '/' . $f; }
        }
    }

    if ($pdf_path === null && $epub_path === null) {
        $response['error'] = 'Generation failed. No output files were produced. Check the log for details.';
        echo json_encode($response);
        exit;
    }

    $response['pdf']  = $pdf_path  !== null;
    $response['epub'] = $epub_path !== null;

    // Create ZIP
    $zip_path = $output_dir . '/' . $safe_title . '_bundle.zip';
    $zip = new ZipArchive();
    if ($zip->open($zip_path, ZipArchive::CREATE) === true) {
        if ($pdf_path  !== null) { $zip->addFile($pdf_path,  basename($pdf_path)); }
        if ($epub_path !== null) { $zip->addFile($epub_path, basename($epub_path)); }
        $zip->close();
        $response['zip'] = file_exists($zip_path);
    }

    // Upload to GitHub if requested
    if (!empty($gh_repo) && GITHUB_TOKEN !== 'ghp_YOUR_TOKEN_HERE') {
        $gh_files = array_filter([$pdf_path, $epub_path], fn($f) => $f !== null);
        $gh_result = upload_to_github($gh_repo, $title, $author, $gh_files);
        $response['github'] = $gh_result;
    }

    $response['success'] = true;
    $response['job_id']  = $job_id;
    echo json_encode($response);
    exit;
}
// ── End PHP logic ─────────────────────────────────────────────────────────────
?><!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Book Generator</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: Georgia, 'Times New Roman', Times, serif;
    background: #f4f1ed;
    color: #2c2c2c;
    min-height: 100vh;
    padding: 40px 16px 80px;
  }

  .container {
    max-width: 720px;
    margin: 0 auto;
  }

  header {
    text-align: center;
    margin-bottom: 36px;
  }
  header h1 {
    font-size: 2.2rem;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: #1a1a1a;
  }
  header p {
    margin-top: 6px;
    font-size: 1rem;
    color: #666;
    font-style: italic;
  }

  .card {
    background: #ffffff;
    border-radius: 10px;
    box-shadow: 0 2px 16px rgba(0,0,0,0.09), 0 1px 4px rgba(0,0,0,0.05);
    padding: 40px 44px;
  }

  /* ── Form ── */
  .form-group {
    margin-bottom: 22px;
  }
  label {
    display: block;
    font-size: 0.85rem;
    font-weight: 700;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #444;
    margin-bottom: 7px;
  }
  label .opt {
    font-weight: 400;
    text-transform: none;
    letter-spacing: 0;
    color: #999;
    font-size: 0.78rem;
  }
  input[type="text"] {
    width: 100%;
    padding: 11px 14px;
    border: 1.5px solid #d8d3cc;
    border-radius: 6px;
    font-size: 1rem;
    font-family: Georgia, serif;
    color: #1a1a1a;
    background: #fafafa;
    transition: border-color 0.2s, box-shadow 0.2s;
    outline: none;
  }
  input[type="text"]:focus {
    border-color: #7c6a55;
    box-shadow: 0 0 0 3px rgba(124,106,85,0.12);
    background: #fff;
  }

  /* ── Source toggle ── */
  .source-toggle { display:flex; gap:8px; }
  .src-btn {
    flex:1; padding:10px 16px; border:2px solid #ddd; border-radius:8px;
    background:#fff; font-size:14px; font-weight:600; cursor:pointer;
    color:#555; transition:all .2s;
  }
  .src-btn:hover { border-color:#3498db; color:#3498db; }
  .src-btn.active { border-color:#3498db; background:#ebf5fb; color:#2980b9; }

  /* ── Drop zone ── */
  .drop-zone {
    border: 2px dashed #c8bfb3;
    border-radius: 8px;
    padding: 32px 24px;
    text-align: center;
    background: #faf8f5;
    cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
    position: relative;
  }
  .drop-zone:hover,
  .drop-zone.dragover {
    border-color: #7c6a55;
    background: #f3ede6;
  }
  .drop-zone input[type="file"] {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
    width: 100%;
    height: 100%;
  }
  .drop-icon {
    font-size: 2.2rem;
    display: block;
    margin-bottom: 10px;
    pointer-events: none;
  }
  .drop-text {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #666;
    font-size: 0.95rem;
    pointer-events: none;
  }
  .drop-text strong {
    color: #7c6a55;
  }
  .file-name {
    margin-top: 10px;
    font-size: 0.88rem;
    font-family: monospace;
    color: #3a7c3a;
    font-weight: 600;
    display: none;
    pointer-events: none;
  }

  /* ── Submit button ── */
  .btn-submit {
    width: 100%;
    padding: 14px;
    background: #3d2e1e;
    color: #fff;
    border: none;
    border-radius: 7px;
    font-size: 1.05rem;
    font-family: Georgia, serif;
    font-weight: 700;
    letter-spacing: 0.03em;
    cursor: pointer;
    margin-top: 8px;
    transition: background 0.2s, opacity 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }
  .btn-submit:hover:not(:disabled) { background: #5c4430; }
  .btn-submit:disabled { opacity: 0.65; cursor: not-allowed; }

  .spinner {
    display: none;
    width: 18px;
    height: 18px;
    border: 2.5px solid rgba(255,255,255,0.35);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Divider ── */
  .divider {
    border: none;
    border-top: 1px solid #e8e3dc;
    margin: 32px 0;
  }

  /* ── Results ── */
  #results { display: none; }

  .success-banner {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 1.05rem;
    color: #2d6a2d;
    font-weight: 600;
    margin-bottom: 20px;
  }

  .download-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 18px;
  }
  .btn-dl {
    padding: 10px 20px;
    border-radius: 6px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 0.9rem;
    font-weight: 600;
    text-decoration: none;
    border: 1.5px solid transparent;
    cursor: pointer;
    transition: background 0.18s, border-color 0.18s;
    display: inline-block;
  }
  .btn-dl-pdf  { background: #eef2ff; color: #3045a8; border-color: #b0bdf0; }
  .btn-dl-epub { background: #f0faf0; color: #1f6e2e; border-color: #9fd4a0; }
  .btn-dl-zip  { background: #fff8ee; color: #8a5100; border-color: #f0c87a; }
  .btn-dl:hover { filter: brightness(0.93); }

  .btn-gh {
    display: inline-block;
    margin-bottom: 18px;
    padding: 9px 18px;
    background: #24292f;
    color: #fff;
    border-radius: 6px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 0.9rem;
    font-weight: 600;
    text-decoration: none;
    transition: background 0.18s;
  }
  .btn-gh:hover { background: #444d56; }

  details {
    margin-top: 6px;
  }
  summary {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 0.85rem;
    color: #7c6a55;
    cursor: pointer;
    user-select: none;
    padding: 4px 0;
  }
  summary:hover { color: #3d2e1e; }
  pre#log-output {
    margin-top: 10px;
    background: #1e1e1e;
    color: #d4d4d4;
    border-radius: 6px;
    padding: 16px;
    font-size: 0.8rem;
    line-height: 1.6;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 320px;
    overflow-y: auto;
  }

  /* ── Error ── */
  #error-box {
    display: none;
    background: #fff0f0;
    border: 1.5px solid #f5b8b8;
    border-radius: 7px;
    padding: 14px 18px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 0.95rem;
    color: #8b1a1a;
    margin-top: 20px;
  }

  @media (max-width: 540px) {
    .card { padding: 28px 20px; }
    header h1 { font-size: 1.7rem; }
  }
</style>
</head>
<body>
<div class="container">

  <header>
    <h1>📚 Book Generator</h1>
    <p>Upload a manuscript and generate a print-ready PDF &amp; EPUB in seconds.</p>
  </header>

  <div class="card">

    <form id="gen-form" novalidate>

      <!-- Source Toggle -->
      <div class="form-group">
        <label>Manuscript Source</label>
        <div class="source-toggle">
          <button type="button" class="src-btn active" id="src-upload">📄 Upload File</button>
          <button type="button" class="src-btn" id="src-url">🔗 URL</button>
        </div>
      </div>

      <!-- File Upload -->
      <div class="form-group" id="group-upload">
        <label>Manuscript File <span class="opt">.docx or .doc</span></label>
        <div class="drop-zone" id="drop-zone">
          <input type="file" name="docx" id="docx-input" accept=".docx,.doc">
          <span class="drop-icon">📄</span>
          <div class="drop-text">
            <strong>Click to browse</strong> or drag &amp; drop your file here
          </div>
          <div class="file-name" id="file-name"></div>
        </div>
      </div>

      <!-- URL Input -->
      <div class="form-group" id="group-url" style="display:none">
        <label>File URL <span class="opt">Google Drive · S3 · Direct HTTPS</span></label>
        <input type="text" name="docx_url" id="docx-url" placeholder="https://drive.google.com/file/d/…/view  or  s3://bucket/file.docx" style="width:100%;padding:10px;font-size:14px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box">
        <small style="color:#888;display:block;margin-top:4px">Google Drive: File → Share → <em>Anyone with the link</em> → Copy link</small>
      </div>

      <!-- Book Title -->
      <div class="form-group">
        <label for="title">Book Title <span style="color:#c0392b">*</span></label>
        <input type="text" id="title" name="title" placeholder="e.g. The Art of Stillness" required>
      </div>

      <!-- Subtitle -->
      <div class="form-group">
        <label for="subtitle">Subtitle <span class="opt">optional</span></label>
        <input type="text" id="subtitle" name="subtitle" placeholder="e.g. A Guide to Inner Peace">
      </div>

      <!-- Author -->
      <div class="form-group">
        <label for="author">Author <span style="color:#c0392b">*</span></label>
        <input type="text" id="author" name="author" placeholder="e.g. Jane Smith" required>
      </div>

      <!-- GitHub Repo -->
      <div class="form-group">
        <label for="gh_repo">GitHub Repository Name <span class="opt">optional — uploads PDF &amp; EPUB</span></label>
        <input type="text" id="gh_repo" name="gh_repo" placeholder="e.g. my-devotional-book">
      </div>

      <!-- Submit -->
      <button type="submit" class="btn-submit" id="submit-btn">
        <span class="spinner" id="spinner"></span>
        <span id="btn-label">Generate Book</span>
      </button>

    </form>

    <!-- Results -->
    <div id="results">
      <hr class="divider">
      <div class="success-banner">✅ Book generated successfully!</div>

      <div class="download-buttons" id="download-buttons"></div>

      <div id="gh-link-wrap"></div>

      <details open>
        <summary>View generation log</summary>
        <pre id="log-output"></pre>
      </details>
    </div>

    <!-- Error -->
    <div id="error-box"></div>

  </div><!-- /.card -->
</div><!-- /.container -->

<script>
(function () {
  'use strict';

  // ── Drag & drop ────────────────────────────────────────────────────────────
  const dropZone  = document.getElementById('drop-zone');
  const fileInput = document.getElementById('docx-input');
  const fileLabel = document.getElementById('file-name');

  function showFileName(name) {
    fileLabel.textContent = '✓ ' + name;
    fileLabel.style.display = 'block';
  }

  fileInput.addEventListener('change', function () {
    if (this.files.length > 0) showFileName(this.files[0].name);
  });

  dropZone.addEventListener('dragover', function (e) {
    e.preventDefault();
    dropZone.classList.add('dragover');
  });
  dropZone.addEventListener('dragleave', function () {
    dropZone.classList.remove('dragover');
  });
  dropZone.addEventListener('drop', function (e) {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    const dt = e.dataTransfer;
    if (dt.files.length > 0) {
      // Transfer dragged file to the real input
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(dt.files[0]);
      fileInput.files = dataTransfer.files;
      showFileName(dt.files[0].name);
    }
  });

  // ── Source toggle ──────────────────────────────────────────────────────────
  function setSource(mode) {
    const isUrl = mode === 'url';
    document.getElementById('src-upload').classList.toggle('active', !isUrl);
    document.getElementById('src-url').classList.toggle('active',    isUrl);
    document.getElementById('group-upload').style.display = isUrl ? 'none' : '';
    document.getElementById('group-url').style.display    = isUrl ? ''     : 'none';
  }
  document.getElementById('src-upload').addEventListener('click', function() { setSource('upload'); });
  document.getElementById('src-url').addEventListener('click',    function() { setSource('url'); });

  // ── Form submit ────────────────────────────────────────────────────────────
  const form       = document.getElementById('gen-form');
  const submitBtn  = document.getElementById('submit-btn');
  const spinner    = document.getElementById('spinner');
  const btnLabel   = document.getElementById('btn-label');
  const resultsDiv = document.getElementById('results');
  const errorBox   = document.getElementById('error-box');
  const dlButtons  = document.getElementById('download-buttons');
  const ghLinkWrap = document.getElementById('gh-link-wrap');
  const logOutput  = document.getElementById('log-output');

  function setLoading(loading) {
    submitBtn.disabled = loading;
    spinner.style.display = loading ? 'block' : 'none';
    btnLabel.textContent  = loading ? 'Generating…' : 'Generate Book';
  }

  function showError(msg) {
    errorBox.innerHTML  = '<strong>❌ Error:</strong> ' + escHtml(msg);
    errorBox.style.display = 'block';
    resultsDiv.style.display = 'none';
  }

  function escHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  function makeDlButton(href, label, cls) {
    const a = document.createElement('a');
    a.href      = href;
    a.className = 'btn-dl ' + cls;
    a.textContent = label;
    return a;
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();

    // Hide previous results / errors
    resultsDiv.style.display  = 'none';
    errorBox.style.display    = 'none';
    dlButtons.innerHTML       = '';
    ghLinkWrap.innerHTML      = '';
    logOutput.textContent     = '';

    // Client-side validation
    const title      = document.getElementById('title').value.trim();
    const author     = document.getElementById('author').value.trim();
    const sourceMode = document.getElementById('src-url').classList.contains('active') ? 'url' : 'upload';
    const urlVal     = document.getElementById('docx-url').value.trim();

    if (!title)  { showError('Book title is required.'); return; }
    if (!author) { showError('Author name is required.'); return; }

    if (sourceMode === 'url') {
      if (!urlVal) { showError('Please enter a file URL.'); return; }
    } else {
      if (!fileInput.files || fileInput.files.length === 0) {
        showError('Please select a .docx or .doc file.'); return;
      }
      const ext = fileInput.files[0].name.split('.').pop().toLowerCase();
      if (ext !== 'docx' && ext !== 'doc') {
        showError('Only .docx or .doc files are accepted.'); return;
      }
    }

    setLoading(true);

    const formData = new FormData(form);

    fetch(window.location.pathname, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: formData,
    })
    .then(function (res) {
      if (!res.ok) throw new Error('Server returned HTTP ' + res.status);
      return res.json();
    })
    .then(function (data) {
      setLoading(false);

      if (!data.success) {
        showError(data.error || 'An unknown error occurred.');
        // Still show log if available
        if (data.log) {
          logOutput.textContent = data.log;
          resultsDiv.style.display = 'block';
          document.querySelector('#results .success-banner').style.display = 'none';
          dlButtons.style.display = 'none';
        }
        return;
      }

      // Show success
      document.querySelector('#results .success-banner').style.display = '';
      dlButtons.style.display = '';

      const job = data.job_id;
      if (data.pdf) {
        dlButtons.appendChild(makeDlButton(
          '?action=download&job=' + encodeURIComponent(job) + '&type=pdf',
          '⬇ Download PDF', 'btn-dl-pdf'
        ));
      }
      if (data.epub) {
        dlButtons.appendChild(makeDlButton(
          '?action=download&job=' + encodeURIComponent(job) + '&type=epub',
          '⬇ Download EPUB', 'btn-dl-epub'
        ));
      }
      if (data.zip) {
        dlButtons.appendChild(makeDlButton(
          '?action=download&job=' + encodeURIComponent(job) + '&type=zip',
          '⬇ Download ZIP (both)', 'btn-dl-zip'
        ));
      }

      // GitHub link
      if (data.github && data.github.success && data.github.repo_url) {
        const a = document.createElement('a');
        a.href      = data.github.repo_url;
        a.target    = '_blank';
        a.rel       = 'noopener noreferrer';
        a.className = 'btn-gh';
        a.textContent = '🐙 View on GitHub →';
        ghLinkWrap.appendChild(a);
      }

      // Log
      if (data.log) {
        logOutput.textContent = data.log;
      }

      resultsDiv.style.display = 'block';
      resultsDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
    })
    .catch(function (err) {
      setLoading(false);
      showError(err.message || 'Network error. Please try again.');
    });
  });

})();
</script>
</body>
</html>
