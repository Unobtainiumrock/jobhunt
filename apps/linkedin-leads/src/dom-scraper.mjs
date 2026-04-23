/**
 * DOM-based message scraper for LinkedIn conversations.
 *
 * The LinkedIn MessagingGraphQL API caps messages at ~20 per conversation.
 * This module navigates to each conversation's thread URL via CDP Page.navigate,
 * scrolls up using real mouseWheel events to trigger React lazy-loading,
 * then extracts all messages from the DOM.
 */

let _nextCdpId = 200000;

/**
 * Send a CDP command over the WebSocket and wait for the response.
 */
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

/**
 * Wait for a CDP event.
 */
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
 * Extract the thread ID from a conversation URN.
 * URN: urn:li:msg_conversation:(urn:li:fsd_profile:ACoAA...,2-ZTk4...)
 * Thread URL: /messaging/thread/2-ZTk4.../
 */
function threadIdFromUrn(conversationUrn) {
  const match = conversationUrn.match(/,([^)]+)\)$/);
  if (match) return match[1];
  return conversationUrn.split(':').pop();
}

/**
 * Navigate to a URL using CDP Page.navigate and wait for load.
 */
async function cdpNavigate(cdp, url) {
  await cdpSend(cdp, 'Page.enable', {}, 5000);
  await cdpSend(cdp, 'Page.navigate', { url }, 15000);
  try {
    await cdpWaitForEvent(cdp, 'Page.loadEventFired', 30000);
  } catch {}
  await new Promise(r => setTimeout(r, 2000));
}

/**
 * Scroll the message panel up using CDP mouseWheel to trigger React lazy-loading.
 * Returns { count, reason }.
 */
async function scrollMessagesToTop(cdp, evaluate) {
  // Get position of the message list for mouse targeting
  const posRaw = await evaluate(`
    (function() {
      const el = document.querySelector('.msg-s-message-list');
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      return JSON.stringify({ x: rect.x + rect.width/2, y: rect.y + 50 });
    })()
  `);
  if (!posRaw) return { count: 0, reason: 'no_container' };
  const { x, y } = JSON.parse(posRaw);

  let prevCount = await evaluate(
    `document.querySelectorAll('li.msg-s-message-list__event').length`
  ) || 0;
  let stableRounds = 0;

  // Scroll up repeatedly. Each scroll event triggers LinkedIn's React handlers
  // which may lazy-load older messages. We keep scrolling until the count stabilizes.
  for (let i = 0; i < 100; i++) {
    try {
      await cdpSend(cdp, 'Input.dispatchMouseEvent', {
        type: 'mouseWheel',
        x: Math.round(x),
        y: Math.round(y),
        deltaX: 0,
        deltaY: -800,
      }, 5000);
    } catch {
      // Scroll event timeout is non-fatal — page might be busy rendering
      await new Promise(r => setTimeout(r, 2000));
      continue;
    }

    // Give LinkedIn time to fetch and render additional messages
    await new Promise(r => setTimeout(r, 1200));

    const currentCount = await evaluate(
      `document.querySelectorAll('li.msg-s-message-list__event').length`
    ) || 0;

    if (currentCount === prevCount) {
      stableRounds++;
      // Wait long enough to catch slow lazy-loads but not forever
      if (stableRounds >= 8) {
        return { count: currentCount, reason: 'stable' };
      }
    } else {
      stableRounds = 0;
      prevCount = currentCount;
    }
  }

  return { count: prevCount, reason: 'max_iterations' };
}

/**
 * Resolve relative date headings ("Today", "Friday", "Mar 5") to YYYY-MM-DD.
 */
function resolveDateHeading(text) {
  const now = new Date();
  const lower = text.toLowerCase().trim();

  if (lower === 'today') return formatDate(now);
  if (lower === 'yesterday') {
    const d = new Date(now);
    d.setDate(d.getDate() - 1);
    return formatDate(d);
  }

  const days = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
  const dayIdx = days.indexOf(lower);
  if (dayIdx >= 0) {
    const d = new Date(now);
    const diff = (now.getDay() - dayIdx + 7) % 7 || 7;
    d.setDate(d.getDate() - diff);
    return formatDate(d);
  }

  // "Mar 5", "March 5", etc.
  const withYear = `${text} ${now.getFullYear()}`;
  const parsed = new Date(withYear);
  if (!isNaN(parsed.getTime())) {
    if (parsed > now) parsed.setFullYear(parsed.getFullYear() - 1);
    return formatDate(parsed);
  }

  return null;
}

function formatDate(d) {
  return d.toISOString().split('T')[0];
}

/**
 * Extract all messages from the conversation panel DOM.
 */
function extractMessagesExpr() {
  return `
    (function() {
      const messages = [];
      let currentSender = "Unknown";
      let currentDateText = "";

      const allItems = document.querySelectorAll('ul.msg-s-message-list-content > li');

      for (const li of allItems) {
        if (!li.classList.contains('msg-s-message-list__event')) continue;

        const timeHeading = li.querySelector('time.msg-s-message-list__time-heading');
        if (timeHeading) {
          currentDateText = timeHeading.textContent.trim();
        }

        const nameEl = li.querySelector('.msg-s-message-group__name');
        if (nameEl) {
          currentSender = nameEl.textContent.trim();
        }

        const bodyEl = li.querySelector('.msg-s-event-listitem__body');
        if (!bodyEl) continue;
        let text = bodyEl.textContent.trim();
        if (!text) continue;

        let timeText = "";
        const tsEl = li.querySelector('time.msg-s-message-group__timestamp');
        if (tsEl) {
          timeText = tsEl.textContent.trim();
        }

        const subjectEl = li.querySelector('h3.msg-s-event-listitem__subject');
        const subject = subjectEl ? subjectEl.textContent.trim() : "";

        messages.push({
          sender: currentSender,
          text: text,
          subject: subject,
          dateHeading: currentDateText,
          time: timeText,
        });
      }

      return JSON.stringify(messages);
    })()
  `;
}

/**
 * Scrape full message history for a single conversation via its thread URL.
 *
 * @param {WebSocket} cdp - CDP WebSocket
 * @param {Function} evaluate - CDP evaluate function
 * @param {string} conversationUrn - The conversation URN
 * @param {Function} log - Logging function
 * @returns {Array} Message objects with resolved timestamps
 */
export async function scrapeConversation(cdp, evaluate, conversationUrn, log) {
  const threadId = threadIdFromUrn(conversationUrn);
  if (!threadId) {
    log(`    Could not extract thread ID from ${conversationUrn}`);
    return null;
  }

  const url = `https://www.linkedin.com/messaging/thread/${threadId}/`;

  // Navigate via CDP (preserves execution context)
  await cdpNavigate(cdp, url);

  // Wait for the message list to render
  const listReady = await evaluate(`
    (async () => {
      for (let i = 0; i < 15; i++) {
        await new Promise(r => setTimeout(r, 1000));
        const msgs = document.querySelectorAll('li.msg-s-message-list__event');
        if (msgs.length > 0) return "ready:" + msgs.length;
      }
      return "timeout";
    })()
  `, 20000);

  if (!listReady || listReady === 'timeout') {
    log(`    Message list did not load`);
    return null;
  }

  // Scroll up to load all messages
  const scrollResult = await scrollMessagesToTop(cdp, evaluate);
  if (scrollResult.count === 0) {
    log(`    No messages after scroll`);
    return null;
  }
  log(`    Loaded ${scrollResult.count} messages (${scrollResult.reason})`);

  // Extract messages from DOM
  const raw = await evaluate(extractMessagesExpr(), 30000);
  const messages = raw ? JSON.parse(raw) : [];

  // Resolve timestamps
  for (const msg of messages) {
    const dateStr = resolveDateHeading(msg.dateHeading);
    if (dateStr && msg.time) {
      const combined = new Date(`${dateStr} ${msg.time}`);
      if (!isNaN(combined.getTime())) {
        msg.timestamp = combined.toISOString();
      } else {
        msg.timestamp = `${dateStr} ${msg.time}`;
      }
    } else if (dateStr) {
      msg.timestamp = dateStr;
    } else if (msg.time) {
      msg.timestamp = msg.time;
    } else {
      msg.timestamp = null;
    }
    delete msg.dateHeading;
    delete msg.time;
  }

  return messages;
}

/**
 * Scrape all conversations by navigating to each thread URL.
 * Retries once on failure per conversation. Errors are isolated so one
 * bad thread doesn't kill the entire run.
 */
export async function scrapeAllConversations(cdp, evaluate, conversations, log) {
  let failures = 0;

  for (let i = 0; i < conversations.length; i++) {
    const convo = conversations[i];
    if (!convo.conversationUrn) continue;

    const names = convo.participants.map(p => p.name).join(', ');
    log(`  [${i + 1}/${conversations.length}] ${names}...`);

    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const messages = await scrapeConversation(cdp, evaluate, convo.conversationUrn, log);
        if (messages && messages.length > 0) {
          convo.messages = messages;
        }
        break; // success
      } catch (err) {
        if (attempt === 0) {
          log(`    Error: ${err.message} — retrying...`);
          await new Promise(r => setTimeout(r, 3000));
        } else {
          log(`    Failed after retry: ${err.message}`);
          failures++;
        }
      }
    }

    log(`    ${convo.messages.length} messages captured`);
    await new Promise(r => setTimeout(r, 300));
  }

  if (failures > 0) {
    log(`  ${failures} conversation(s) failed — their messages may be incomplete`);
  }
}
