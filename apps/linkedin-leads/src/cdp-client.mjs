/**
 * Chrome DevTools Protocol client for LinkedIn.
 *
 * Extracted and adapted from gravity-pulse/scrapers/slack-listener.mjs.
 * Connects to an already-running Chrome instance with --remote-debugging-port=9222
 * and provides helpers to evaluate JS inside the authenticated LinkedIn tab.
 */

import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const WebSocket = require('ws');

const CDP_PORT = process.env.CDP_PORT || 9222;
const CDP_BASE = process.env.CDP_URL || `http://localhost:${CDP_PORT}`;

/**
 * Find the LinkedIn tab in Chrome's CDP endpoint.
 * Returns the webSocketDebuggerUrl for that tab.
 */
export async function findLinkedInTab() {
  const resp = await fetch(`${CDP_BASE}/json`);
  const tabs = await resp.json();

  // Filter to real LinkedIn pages (not tracking iframes or third-party embeds)
  const linkedInPages = tabs.filter(t =>
    t.type === 'page' &&
    t.url?.match(/^https:\/\/(www\.)?linkedin\.com\//)
  );

  // Prefer a general page (feed, messaging index) over a specific thread
  // since thread pages may be in a stale navigation state
  const preferred = linkedInPages.find(t => !t.url.includes('/messaging/thread/'));
  const tab = preferred || linkedInPages[0];

  if (!tab) {
    throw new Error(
      'No LinkedIn tab found in Chrome. Make sure Chrome is running with ' +
      `--remote-debugging-port=${CDP_PORT} and you have linkedin.com open.`
    );
  }

  let wsUrl = tab.webSocketDebuggerUrl;
  if (CDP_BASE !== `http://localhost:${CDP_PORT}`) {
    const cdpHost = new URL(CDP_BASE);
    wsUrl = wsUrl.replace(/ws:\/\/[^/]+/, `ws://${cdpHost.host}`);
  }
  return wsUrl;
}

/**
 * Open a WebSocket connection to a CDP target.
 */
export function connectCDP(wsUrl, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => {
      ws.terminate();
      reject(new Error('CDP connection timeout'));
    }, timeoutMs);
    ws.on('open', () => { clearTimeout(timer); resolve(ws); });
    ws.on('error', (e) => { clearTimeout(timer); reject(e); });
  });
}

/**
 * Create an evaluate() helper bound to a CDP WebSocket.
 * Executes arbitrary JS inside the browser tab and returns the result.
 *
 * Mirrors the pattern from gravity-pulse's refreshMapsViaCDP.
 */
export function makeEvaluator(cdp) {
  let nextId = 1000;

  return function evaluate(expression, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      const myId = nextId++;
      const timer = setTimeout(() => {
        cdp.off('message', handler);
        reject(new Error('evaluate() timeout'));
      }, timeoutMs);

      function handler(raw) {
        try {
          const msg = JSON.parse(raw.toString());
          if (msg.id === myId) {
            cdp.off('message', handler);
            clearTimeout(timer);
            if (msg.error) {
              reject(new Error(msg.error.message || 'CDP evaluation error'));
            } else {
              resolve(msg.result?.result?.value ?? null);
            }
          }
        } catch {}
      }

      cdp.on('message', handler);
      cdp.send(JSON.stringify({
        id: myId,
        method: 'Runtime.evaluate',
        params: {
          expression,
          returnByValue: true,
          awaitPromise: true,
        },
      }));
    });
  };
}

/**
 * High-level: connect to the LinkedIn tab and return { cdp, evaluate }.
 * Caller is responsible for closing the cdp WebSocket when done.
 */
export async function connectToLinkedIn() {
  const wsUrl = await findLinkedInTab();
  const cdp = await connectCDP(wsUrl);
  const evaluate = makeEvaluator(cdp);
  return { cdp, evaluate, wsUrl };
}

/**
 * Extract the LinkedIn CSRF token (JSESSIONID cookie value) needed for API calls.
 * LinkedIn's Voyager API requires this as the csrf-token header.
 */
export async function getCSRFToken(evaluate) {
  const token = await evaluate(`
    (function() {
      const match = document.cookie.match(/JSESSIONID="?([^";]+)"?/);
      return match ? match[1] : null;
    })()
  `);
  if (!token) {
    throw new Error('Could not extract CSRF token. Are you logged into LinkedIn?');
  }
  return token;
}
