#!/usr/bin/env node
/**
 * Phase 4A: Always-on LinkedIn Message Listener
 *
 * Adapted from gravity-pulse/scrapers/slack-listener.mjs.
 * Observes LinkedIn's messaging WebSocket via CDP Network domain events
 * to detect new incoming messages in real-time.
 *
 * Architecture:
 *   Chrome (linkedin.com) --[WebSocket]--> LinkedIn Servers
 *          |
 *     CDP Network.enable --> Network.webSocketFrameReceived
 *          |
 *     linkedin-listener.mjs --> parse message --> classify/score/notify
 *          |
 *     data/inbox_live.json (append with lockfile)
 *
 * Usage:
 *   node src/linkedin-listener.mjs
 *   node src/linkedin-listener.mjs --pipe-to-embedder
 */

import fs from 'fs';
import http from 'http';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawn } from 'child_process';
import { connectToLinkedIn, getCSRFToken } from './cdp-client.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.join(__dirname, '..', 'data');
const LIVE_FILE = path.join(DATA_DIR, 'inbox_live.json');
const LOCK_FILE = LIVE_FILE + '.lock';

const PIPE_TO_EMBEDDER = process.argv.includes('--pipe-to-embedder');
const AUTO_PIPELINE = process.argv.includes('--auto-pipeline');
const PIPELINE_DEBOUNCE_MS = 30_000;

function log(msg) {
  const ts = new Date().toISOString().substring(11, 19);
  console.log(`[${ts}] ${msg}`);
}

function acquireLock() {
  try {
    fs.writeFileSync(LOCK_FILE, String(process.pid), { flag: 'wx' });
    return true;
  } catch {
    return false;
  }
}

function releaseLock() {
  try { fs.unlinkSync(LOCK_FILE); } catch {}
}

function appendMessage(message) {
  let retries = 5;
  while (retries > 0) {
    if (acquireLock()) {
      try {
        let data = { messages: [] };
        if (fs.existsSync(LIVE_FILE)) {
          data = JSON.parse(fs.readFileSync(LIVE_FILE, 'utf8'));
        }
        data.messages.push(message);
        data.lastUpdated = new Date().toISOString();
        fs.writeFileSync(LIVE_FILE, JSON.stringify(data, null, 2));
        return true;
      } finally {
        releaseLock();
      }
    }
    retries--;
    // Spin-wait briefly
    const start = Date.now();
    while (Date.now() - start < 50) {}
  }
  return false;
}

/**
 * Parse a LinkedIn messaging WebSocket frame for new messages.
 * LinkedIn uses a custom protocol over WebSocket; we look for
 * message delivery events.
 */
function parseLinkedInFrame(payload) {
  try {
    const data = JSON.parse(payload);

    // LinkedIn real-time messaging uses different event shapes.
    // Look for message events in the payload.
    if (data?.topic === '/messaging/conversations' ||
        data?.topic === '/messaging/realtime' ||
        data?.$type === 'com.linkedin.voyager.messaging.event.MessageEvent') {
      return extractMessageFromEvent(data);
    }

    // Some frames contain nested payloads
    if (data?.data?.payload) {
      return parseLinkedInFrame(JSON.stringify(data.data.payload));
    }

    // Check for array of events
    if (Array.isArray(data)) {
      for (const item of data) {
        const result = extractMessageFromEvent(item);
        if (result) return result;
      }
    }
  } catch {
    // Not JSON or unrecognized format
  }
  return null;
}

function extractMessageFromEvent(event) {
  if (!event) return null;

  // Try to extract message body from various LinkedIn event shapes
  const body = event?.body?.text
    || event?.message?.body?.text
    || event?.eventContent?.messageEvent?.body?.text
    || event?.payload?.body?.text;

  if (!body || body.length < 2) return null;

  const sender = event?.actor?.firstName?.text
    || event?.message?.actor?.firstName?.text
    || event?.from?.firstName
    || 'Unknown';

  const senderLast = event?.actor?.lastName?.text
    || event?.message?.actor?.lastName?.text
    || event?.from?.lastName
    || '';

  const conversationUrn = event?.conversationUrn
    || event?.conversation?.entityUrn
    || event?.parentUrn
    || '';

  const timestamp = event?.deliveredAt
    ? new Date(event.deliveredAt).toISOString()
    : new Date().toISOString();

  return {
    sender: `${sender} ${senderLast}`.trim(),
    text: body,
    conversationUrn,
    timestamp,
    subject: event?.subject || '',
  };
}

function startHealthHttpServer() {
  const port = Number(process.env.LISTENER_HEALTH_PORT || 3765);
  return new Promise((resolve, reject) => {
    const server = http.createServer((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('ok\n');
    });
    server.on('error', (err) => {
      log(`Health HTTP server error: ${err.message}`);
      reject(err);
    });
    server.listen(port, '0.0.0.0', () => {
      log(`Health check HTTP listening on 0.0.0.0:${port}`);
      resolve(server);
    });
  });
}

async function main() {
  log('LinkedIn Listener starting...');
  await startHealthHttpServer();
  log('Connecting to Chrome via CDP...');

  let cdp;
  try {
    const conn = await connectToLinkedIn();
    cdp = conn.cdp;
  } catch (err) {
    console.error(
      'Could not connect to Chrome.\n' +
      'Make sure Chrome is running with: node src/launch-chrome.mjs\n' +
      `Error: ${err.message}`
    );
    process.exit(1);
  }

  // Enable Network domain to observe WebSocket traffic
  let nextId = 500000;
  function cdpSend(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => reject(new Error(`${method} timeout`)), 10000);
      function handler(raw) {
        try {
          const msg = JSON.parse(raw.toString());
          if (msg.id === id) {
            cdp.off('message', handler);
            clearTimeout(timer);
            if (msg.error) reject(new Error(msg.error.message));
            else resolve(msg.result);
          }
        } catch {}
      }
      cdp.on('message', handler);
      cdp.send(JSON.stringify({ id, method, params }));
    });
  }

  await cdpSend('Network.enable');
  log('Network domain enabled — listening for WebSocket frames...');

  // Optionally spawn the Python embed worker
  let embedWorker = null;
  if (PIPE_TO_EMBEDDER) {
    log('Spawning embed worker...');
    embedWorker = spawn('python', ['-m', 'pipeline.embed_conversations', '--worker'], {
      cwd: path.join(__dirname, '..'),
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    embedWorker.stdout.on('data', (data) => {
      const line = data.toString().trim();
      if (line === 'READY') {
        log('Embed worker ready.');
      }
    });
  }

  let messageCount = 0;
  let pipelineTimer = null;
  let pipelineRunning = false;
  fs.mkdirSync(DATA_DIR, { recursive: true });

  function schedulePipeline() {
    if (!AUTO_PIPELINE || pipelineRunning) return;
    if (pipelineTimer) clearTimeout(pipelineTimer);
    pipelineTimer = setTimeout(runPipeline, PIPELINE_DEBOUNCE_MS);
    log(`Pipeline scheduled in ${PIPELINE_DEBOUNCE_MS / 1000}s (debouncing)`);
  }

  function runPipeline() {
    if (pipelineRunning) return;
    pipelineRunning = true;
    log('--- AUTO-PIPELINE: scraping + classify + score + reply ---');
    const pipeline = spawn('bash', ['-c',
      'node src/scrape-inbox.mjs --api-only' +
      ' && python -m pipeline.classify_leads' +
      ' && python -m pipeline.score_leads' +
      ' && python -m pipeline.generate_reply' +
      ' && python -m pipeline.export_csv'
    ], {
      cwd: path.join(__dirname, '..'),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    pipeline.stdout.on('data', (d) => {
      for (const line of d.toString().trim().split('\n')) {
        if (line) log(`[pipeline] ${line}`);
      }
    });
    pipeline.stderr.on('data', (d) => {
      for (const line of d.toString().trim().split('\n')) {
        if (line) log(`[pipeline:err] ${line}`);
      }
    });
    pipeline.on('close', (code) => {
      pipelineRunning = false;
      log(`--- AUTO-PIPELINE: finished (exit ${code}) ---`);
    });
  }

  // Listen for WebSocket frames
  cdp.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw.toString());

      if (msg.method === 'Network.webSocketFrameReceived') {
        const payload = msg.params?.response?.payloadData;
        if (!payload) return;

        const message = parseLinkedInFrame(payload);
        if (!message) return;

        messageCount++;
        log(`[MSG #${messageCount}] ${message.sender}: ${message.text.substring(0, 80)}...`);

        // Append to live file
        const saved = appendMessage({
          ...message,
          receivedAt: new Date().toISOString(),
        });
        if (!saved) {
          log('WARNING: Failed to write message (lock contention)');
        }

        // Trigger debounced pipeline
        schedulePipeline();

        // Pipe to embed worker
        if (embedWorker?.stdin?.writable) {
          embedWorker.stdin.write(JSON.stringify({
            sender: message.sender,
            channel: message.conversationUrn,
            timestamp: message.timestamp,
            text: message.text,
          }) + '\n');
        }
      }
    } catch {}
  });

  // Keep alive
  log('Listener active. Press Ctrl+C to stop.');

  process.on('SIGINT', () => {
    log(`Shutting down. Received ${messageCount} messages.`);
    if (embedWorker) {
      embedWorker.stdin.end();
      embedWorker.kill();
    }
    releaseLock();
    try { cdp.close(); } catch {}
    process.exit(0);
  });

  // Heartbeat
  setInterval(() => {
    log(`Heartbeat: ${messageCount} messages received`);
  }, 60000);
}

main().catch(e => {
  console.error(`FATAL: ${e.message}`);
  process.exit(1);
});
