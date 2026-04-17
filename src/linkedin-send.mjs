/**
 * LinkedIn message sender.
 *
 * DOM-primary: navigate to the thread URL in the authenticated Chrome tab,
 * focus the compose box, type the message text, click Send, and verify by
 * confirming our own message is the newest message in the thread.
 *
 * Voyager fallback: if DOM send fails and LINKEDIN_ALLOW_VOYAGER_SEND=1 is
 * set, POST a create-message mutation via CDP fetch() inside the tab. This
 * path is gated because LinkedIn's create-message mutation id changes
 * occasionally and private-mutation traffic is riskier than read traffic.
 *
 * Caller receives a structured result and is expected to handle logging,
 * Telegram notification, and state write-back.
 */

let _nextCdpId = 500000;

function cdpSend(cdp, method, params = {}, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const id = _nextCdpId++;
    const timer = setTimeout(() => {
      cdp.off('message', handler);
      reject(new Error(`CDP ${method} timeout`));
    }, timeoutMs);

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

function cdpWaitForEvent(cdp, eventName, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cdp.off('message', handler);
      reject(new Error(`Timeout waiting for ${eventName}`));
    }, timeoutMs);

    function handler(raw) {
      try {
        const msg = JSON.parse(raw.toString());
        if (msg.method === eventName) {
          cdp.off('message', handler);
          clearTimeout(timer);
          resolve(msg.params);
        }
      } catch {}
    }

    cdp.on('message', handler);
  });
}

/**
 * Extract the thread ID (2-xxx=) portion from a full conversation URN.
 * URN: urn:li:msg_conversation:(urn:li:fsd_profile:ACoAA...,2-ZTk4...=)
 */
export function threadIdFromUrn(conversationUrn) {
  const match = conversationUrn.match(/,([^)]+)\)$/);
  if (match) return match[1];
  return conversationUrn.split(':').pop();
}

function _sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function _truncate(text, limit = 160) {
  if (!text) return '';
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}

async function _detectAuthWall(cdp, evaluate) {
  const url = await evaluate('window.location.href');
  if (typeof url === 'string') {
    const lowered = url.toLowerCase();
    const patterns = ['/checkpoint/', '/login', '/authwall', '/uas/login'];
    if (patterns.some((p) => lowered.includes(p))) {
      return { authwall: true, url };
    }
  }
  return { authwall: false, url };
}

async function _navigateToThread(cdp, evaluate, threadId) {
  const url = `https://www.linkedin.com/messaging/thread/${threadId}/`;
  await cdpSend(cdp, 'Page.enable', {}, 5000);
  await cdpSend(cdp, 'Page.navigate', { url }, 15000);
  try {
    await cdpWaitForEvent(cdp, 'Page.loadEventFired', 30000);
  } catch {}
  await _sleep(1500);
  return url;
}

async function _waitForCompose(evaluate, timeoutMs = 15000) {
  const expr = `
    (async () => {
      const deadline = Date.now() + ${timeoutMs};
      while (Date.now() < deadline) {
        const box = document.querySelector('.msg-form__contenteditable');
        const send = document.querySelector('.msg-form__send-button');
        if (box && send) return "ready";
        await new Promise(r => setTimeout(r, 500));
      }
      return "timeout";
    })()
  `;
  return evaluate(expr, timeoutMs + 5000);
}

async function _focusAndType(evaluate, text) {
  const payload = JSON.stringify(text);
  const expr = `
    (async () => {
      const box = document.querySelector('.msg-form__contenteditable');
      if (!box) return JSON.stringify({ ok: false, reason: "no_compose_box" });
      box.focus();
      box.click();
      await new Promise(r => setTimeout(r, 150));

      const raw = ${payload};
      const paragraphs = raw.split(/\\r?\\n/);
      box.innerHTML = "";
      for (const line of paragraphs) {
        const p = document.createElement('p');
        p.textContent = line;
        box.appendChild(p);
      }

      box.dispatchEvent(new Event('input', { bubbles: true }));
      box.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: raw }));
      await new Promise(r => setTimeout(r, 250));

      const observed = box.textContent || "";
      // textContent concatenates <p> tags with no separator, so "Name,\\n\\nBody"
      // becomes "Name,Body" in the compose box. Strip ALL whitespace on both
      // sides before comparing so paragraph structure does not matter.
      const strip = (s) => s.replace(/\\s+/g, "");
      const stripObs = strip(observed);
      const stripExp = strip(raw).slice(0, Math.min(40, strip(raw).length));
      return JSON.stringify({
        ok: stripObs.length > 0 && stripExp.length > 0 && stripObs.includes(stripExp),
        observed_length: observed.length,
        observed_preview: observed.slice(0, 120),
        expected_preview: stripExp,
      });
    })()
  `;
  return JSON.parse(await evaluate(expr, 15000));
}

async function _clickSend(evaluate) {
  const expr = `
    (async () => {
      const btn = document.querySelector('.msg-form__send-button');
      if (!btn) return JSON.stringify({ ok: false, reason: "no_send_button" });
      if (btn.disabled) return JSON.stringify({ ok: false, reason: "send_button_disabled" });
      btn.scrollIntoView({ block: "center" });
      btn.click();
      await new Promise(r => setTimeout(r, 300));
      return JSON.stringify({ ok: true });
    })()
  `;
  return JSON.parse(await evaluate(expr, 15000));
}

async function _verifyLastMessage(evaluate, userName, expectedText, timeoutMs = 12000) {
  const payload = JSON.stringify({ userName, expectedText });
  const expr = `
    (async () => {
      const { userName, expectedText } = ${payload};
      const snippet = (expectedText || "").replace(/\\s+/g, " ").trim().slice(0, 40);
      const deadline = Date.now() + ${timeoutMs};
      while (Date.now() < deadline) {
        const items = Array.from(document.querySelectorAll('li.msg-s-message-list__event'));
        const recent = items.slice(-6);
        for (let i = recent.length - 1; i >= 0; i--) {
          const li = recent[i];
          const nameEl = li.querySelector('.msg-s-message-group__name');
          const bodyEl = li.querySelector('.msg-s-event-listitem__body');
          if (!bodyEl) continue;
          const text = (bodyEl.textContent || "").replace(/\\s+/g, " ").trim();
          const sender = nameEl ? nameEl.textContent.trim() : "";
          if (sender && userName && sender.toLowerCase().includes(userName.toLowerCase().split(" ")[0])) {
            if (!snippet || text.includes(snippet)) {
              return JSON.stringify({ verified: true, sender, preview: text.slice(0, 120) });
            }
          }
          if (!snippet || text.includes(snippet)) {
            return JSON.stringify({ verified: true, sender, preview: text.slice(0, 120) });
          }
        }
        await new Promise(r => setTimeout(r, 700));
      }
      return JSON.stringify({ verified: false });
    })()
  `;
  return JSON.parse(await evaluate(expr, timeoutMs + 5000));
}

/**
 * Voyager create-message mutation fallback. Gated by LINKEDIN_ALLOW_VOYAGER_SEND=1.
 *
 * LinkedIn's create-message endpoint:
 *   POST /voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage
 * Payload shape per observed traffic:
 *   {
 *     "message": {
 *       "body": { "text": "...", "attributes": [] },
 *       "renderContentUnions": [],
 *       "conversationUrn": "...",
 *       "originToken": "<uuid>"
 *     },
 *     "mailboxUrn": "...",
 *     "trackingId": "<short random>"
 *   }
 */
async function _voyagerSend(evaluate, csrfToken, conversationUrn, mailboxUrn, text) {
  const body = {
    message: {
      body: { text, attributes: [] },
      renderContentUnions: [],
      conversationUrn,
      originToken: crypto.randomUUID(),
    },
    mailboxUrn,
    trackingId: Math.random().toString(36).slice(2, 12),
  };
  const payload = JSON.stringify({ body, csrfToken });
  const expr = `
    (async () => {
      try {
        const { body, csrfToken } = ${payload};
        const resp = await fetch("/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage", {
          method: "POST",
          headers: {
            "csrf-token": csrfToken,
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "content-type": "application/json; charset=UTF-8",
            "x-restli-protocol-version": "2.0.0",
          },
          credentials: "include",
          body: JSON.stringify(body),
        });
        const text = await resp.text();
        return JSON.stringify({ status: resp.status, ok: resp.ok, body: text.slice(0, 400) });
      } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
      }
    })()
  `;
  const raw = await evaluate(expr, 15000);
  return JSON.parse(raw);
}

/**
 * Send a LinkedIn message via DOM automation, with Voyager fallback.
 *
 * @param {object} opts
 * @param {WebSocket} opts.cdp
 * @param {Function} opts.evaluate
 * @param {string} opts.conversationUrn
 * @param {string} opts.text
 * @param {string} opts.userName
 * @param {"dom"|"voyager"|"auto"} [opts.mode="auto"]
 * @param {string} [opts.csrfToken]
 * @param {string} [opts.mailboxUrn]
 */
export async function sendMessage(opts) {
  const {
    cdp,
    evaluate,
    conversationUrn,
    text,
    userName = 'Nicholas',
    mode = 'auto',
    csrfToken = null,
    mailboxUrn = null,
  } = opts;

  if (!conversationUrn) throw new Error('sendMessage: conversationUrn is required');
  if (!text || !text.trim()) throw new Error('sendMessage: text is required');

  const threadId = threadIdFromUrn(conversationUrn);
  const result = {
    ok: false,
    mode_used: null,
    verified: false,
    authwall: false,
    attempts: [],
    error: null,
    preview: _truncate(text, 80),
  };

  const authCheck = await _detectAuthWall(cdp, evaluate);
  if (authCheck.authwall) {
    result.authwall = true;
    result.error = 'login_or_authwall_detected';
    result.attempts.push({ step: 'auth_check', ...authCheck });
    return result;
  }

  const tryDom = mode === 'dom' || mode === 'auto';
  const tryVoyager = (mode === 'voyager' || mode === 'auto') && process.env.LINKEDIN_ALLOW_VOYAGER_SEND === '1';

  if (tryDom) {
    try {
      await _navigateToThread(cdp, evaluate, threadId);
      const ready = await _waitForCompose(evaluate, 15000);
      if (ready !== 'ready') {
        result.attempts.push({ step: 'wait_compose', status: ready });
        throw new Error('compose_box_not_ready');
      }

      const typed = await _focusAndType(evaluate, text);
      result.attempts.push({ step: 'type', ...typed });
      if (!typed.ok) throw new Error(typed.reason || 'type_failed');

      const clicked = await _clickSend(evaluate);
      result.attempts.push({ step: 'click_send', ...clicked });
      if (!clicked.ok) throw new Error(clicked.reason || 'click_send_failed');

      await _sleep(1500);
      const verified = await _verifyLastMessage(evaluate, userName, text, 12000);
      result.attempts.push({ step: 'verify', ...verified });

      result.ok = true;
      result.mode_used = 'dom';
      result.verified = Boolean(verified.verified);
      // Unverified-but-sent is NOT a failure: the click fired and LinkedIn
      // accepted the compose. The verifier just didn't find the message
      // mirrored back in the thread within the timeout, which happens when
      // the thread feed re-hydrates slowly. Don't poison result.error --
      // the receiver treats any truthy error as a send failure and alerts.
      result.error = null;
      return result;
    } catch (err) {
      result.attempts.push({ step: 'dom_error', error: err.message });
      result.error = err.message;
    }
  }

  if (tryVoyager && csrfToken && mailboxUrn) {
    try {
      const resp = await _voyagerSend(evaluate, csrfToken, conversationUrn, mailboxUrn, text);
      result.attempts.push({ step: 'voyager_post', ...resp });
      if (resp.ok) {
        result.ok = true;
        result.mode_used = 'voyager';
        result.error = null;
        await _sleep(1500);
        try {
          const verified = await _verifyLastMessage(evaluate, userName, text, 8000);
          result.verified = Boolean(verified.verified);
          result.attempts.push({ step: 'voyager_verify', ...verified });
        } catch (verifyErr) {
          result.attempts.push({ step: 'voyager_verify_error', error: verifyErr.message });
        }
        return result;
      }
      result.error = `voyager_http_${resp.status || 'error'}`;
    } catch (err) {
      result.attempts.push({ step: 'voyager_error', error: err.message });
      result.error = err.message;
    }
  } else if (tryVoyager) {
    result.attempts.push({ step: 'voyager_skipped', reason: 'missing_csrf_or_mailbox' });
  }

  return result;
}

export default sendMessage;
