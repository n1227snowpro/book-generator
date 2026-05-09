<?php
header('Content-Type: text/plain');
echo "PHP version: " . phpversion() . "\n";
echo "shell_exec enabled: " . (function_exists('shell_exec') ? 'yes' : 'no') . "\n";
echo "exec enabled: " . (function_exists('exec') ? 'yes' : 'no') . "\n";
echo "Git path: " . shell_exec('which git') . "\n";
echo "CWD: " . __DIR__ . "\n";
echo "Git log: " . shell_exec('cd ' . escapeshellarg(__DIR__) . ' && git log --oneline -3 2>&1') . "\n";
echo "Git remote: " . shell_exec('cd ' . escapeshellarg(__DIR__) . ' && git remote -v 2>&1') . "\n";
echo "Git status: " . shell_exec('cd ' . escapeshellarg(__DIR__) . ' && git status 2>&1') . "\n";
