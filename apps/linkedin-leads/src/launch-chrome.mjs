#!/usr/bin/env node
/**
 * Launch Chrome with remote debugging enabled for CDP access.
 *
 * Adapted from gravity-pulse's chrome launcher. Uses a dedicated profile
 * directory to preserve your LinkedIn session across restarts.
 *
 * Usage:
 *   node src/launch-chrome.mjs
 *   node src/launch-chrome.mjs --port 9333
 */

import { execSync, spawn } from 'child_process';
import fs from 'fs';
import path from 'path';
import os from 'os';

const args = process.argv.slice(2);
const PORT_IDX = args.indexOf('--port');
const CDP_PORT = PORT_IDX >= 0 ? args[PORT_IDX + 1] : '9222';

const PROFILE_DIR = path.join(os.homedir(), '.linkedin-cdp-chrome-profile');

// Detect Chrome binary
function findChrome() {
  const candidates = [
    'google-chrome',
    'google-chrome-stable',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/opt/google/chrome/chrome',
    // macOS
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];

  for (const bin of candidates) {
    try {
      execSync(`which "${bin}" 2>/dev/null || test -x "${bin}"`, { stdio: 'ignore' });
      return bin;
    } catch {}
  }
  throw new Error('Chrome not found. Install Google Chrome or set CHROME_BIN env var.');
}

const chromeBin = process.env.CHROME_BIN || findChrome();

console.log(`Chrome: ${chromeBin}`);
console.log(`Profile: ${PROFILE_DIR}`);
console.log(`CDP port: ${CDP_PORT}`);
console.log();
console.log('Once Chrome opens, navigate to linkedin.com and log in.');
console.log('Your session will persist in the profile directory.');
console.log();

const chromeArgs = [
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${PROFILE_DIR}`,
  '--no-first-run',
  '--no-default-browser-check',
  'https://www.linkedin.com/messaging/',
];

const child = spawn(chromeBin, chromeArgs, {
  stdio: 'ignore',
  detached: true,
});

child.unref();
console.log(`Chrome launched (PID ${child.pid}). You can close this terminal.`);
