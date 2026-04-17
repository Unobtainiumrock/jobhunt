#!/usr/bin/env node
/**
 * LinkedIn Inbox Scraper
 *
 * Connects to your authenticated Chrome session via CDP and pulls
 * messaging conversations + messages from the LinkedIn Voyager API.
 * Defaults to the last 2 weeks of activity, always including full messages.
 *
 * Usage:
 *   node src/scrape-inbox.mjs                        # last 2 weeks, all conversations
 *   node src/scrape-inbox.mjs --days 30              # last 30 days instead
 *   node src/scrape-inbox.mjs --count 100            # cap at 100 conversations
 *   node src/scrape-inbox.mjs --thread "Matthew"     # re-scrape only matching thread(s), merge into existing data
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { connectToLinkedIn, getCSRFToken } from './cdp-client.mjs';
import { fetchConversations, fetchMessages, fetchCurrentProfile, INBOX_CATEGORIES } from './linkedin-api.mjs';
import { scrapeAllConversations } from './dom-scraper.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.join(__dirname, '..', 'data');
const INBOX_FILE = path.join(DATA_DIR, 'inbox.json');

const args = process.argv.slice(2);
const DAYS_IDX = args.indexOf('--days');
const LOOKBACK_DAYS = DAYS_IDX >= 0 ? parseInt(args[DAYS_IDX + 1] || '14') : 14;
const COUNT_IDX = args.indexOf('--count');
const MAX_CONVERSATIONS = COUNT_IDX >= 0 ? parseInt(args[COUNT_IDX + 1] || '200') : 200;
const THREAD_IDX = args.indexOf('--thread');
const THREAD_FILTER = THREAD_IDX >= 0 ? (args[THREAD_IDX + 1] || '').toLowerCase() : null;
const API_ONLY = args.includes('--api-only');
// Repeatable `--name "Sunil Phatak"` collects multiple substring filters.
// Unlike --thread (single-shot), --name bypasses the days cutoff so old InMails
// can be backfilled even if they fall outside the rolling window.
const NAME_FILTERS = collectRepeatedFlag(args, '--name').map(s => s.toLowerCase());
const BACKFILL_MODE = NAME_FILTERS.length > 0 || args.includes('--full');
// In backfill mode we ignore the activity cutoff entirely (effective ~10 years).
const EFFECTIVE_LOOKBACK = BACKFILL_MODE
  ? 365 * 10
  : LOOKBACK_DAYS;
const CUTOFF = new Date(Date.now() - EFFECTIVE_LOOKBACK * 24 * 60 * 60 * 1000);

function collectRepeatedFlag(argv, flag) {
  const values = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === flag && i + 1 < argv.length) values.push(argv[i + 1]);
  }
  return values;
}

function log(msg) {
  const ts = new Date().toISOString().substring(11, 19);
  console.log(`[${ts}] ${msg}`);
}

/**
 * Extract participant name from a MessagingParticipant object.
 */
function participantName(p) {
  const member = p?.participantType?.member;
  if (member) {
    const first = member.firstName?.text || '';
    const last = member.lastName?.text || '';
    return `${first} ${last}`.trim() || null;
  }
  return null;
}

/**
 * Extract the conversations collection from the GraphQL response.
 * Handles both the sync-token and category-query response shapes.
 */
function getConversationCollection(raw) {
  if (!raw?.data) return null;
  return raw.data.messengerConversationsByCategoryQuery
    || raw.data.messengerConversationsBySyncToken
    || null;
}

/**
 * Parse conversations from the MessagingGraphQL response.
 */
function parseConversations(raw) {
  if (!raw || raw.error) return [];

  const collection = getConversationCollection(raw);
  const elements = collection?.elements || [];
  const conversations = [];

  for (const el of elements) {
    if (!el) continue;

    const participants = (el.conversationParticipants || [])
      .map(p => ({
        name: participantName(p) || 'Unknown',
        profileUrn: p.hostIdentityUrn || '',
        headline: p.participantType?.member?.headline?.text || '',
      }));

    // Inline messages (usually just the latest 1)
    const inlineMessages = (el.messages?.elements || []).map(msg => ({
      sender: participantName(msg.actor) || participantName(msg.sender) || 'Unknown',
      text: msg.body?.text || '',
      subject: msg.subject || '',
      timestamp: msg.deliveredAt ? new Date(msg.deliveredAt).toISOString() : null,
    }));

    conversations.push({
      conversationUrn: el.entityUrn || '',
      participants,
      lastActivityAt: el.lastActivityAt ? new Date(el.lastActivityAt).toISOString() : null,
      createdAt: el.createdAt ? new Date(el.createdAt).toISOString() : null,
      unreadCount: el.unreadCount || 0,
      read: el.read ?? true,
      title: el.title?.text || '',
      lastMessagePreview: inlineMessages[0]?.text?.substring(0, 200) || '',
      messages: inlineMessages,
    });
  }

  return conversations;
}

/**
 * Parse messages from the MessagingGraphQL messages response.
 */
function parseMessages(raw) {
  if (!raw || raw.error) return [];

  const elements = raw.data?.messengerMessagesBySyncToken?.elements || [];
  const messages = [];

  for (const el of elements) {
    if (!el) continue;
    const body = el.body?.text || '';
    if (!body) continue;

    messages.push({
      sender: participantName(el.actor) || participantName(el.sender) || 'Unknown',
      text: body,
      subject: el.subject || '',
      timestamp: el.deliveredAt ? new Date(el.deliveredAt).toISOString() : null,
    });
  }

  return messages.sort((a, b) =>
    (a.timestamp || '').localeCompare(b.timestamp || '')
  );
}

async function main() {
  log('Connecting to Chrome via CDP...');
  let cdp, evaluate;
  try {
    ({ cdp, evaluate } = await connectToLinkedIn());
  } catch (err) {
    console.error(
      `Could not connect to Chrome.\n` +
      `Make sure Chrome is running with: node src/launch-chrome.mjs\n` +
      `Error: ${err.message}`
    );
    process.exit(1);
  }

  try {
    log('Extracting CSRF token...');
    const csrfToken = await getCSRFToken(evaluate);
    log('CSRF token acquired');

    // Identify current user and get mailbox URN
    const meRaw = await evaluate(fetchCurrentProfile(csrfToken));
    const me = meRaw ? JSON.parse(meRaw) : null;
    const miniProfile = me?.included?.find(i => i.firstName);
    // The /me endpoint returns fs_miniProfile URNs but messaging uses fsd_profile
    const rawUrn = miniProfile?.entityUrn ||
      me?.data?.entityUrn ||
      me?.data?.['*miniProfile'];

    if (miniProfile) {
      log(`Logged in as: ${miniProfile.firstName} ${miniProfile.lastName}`);
    }

    if (!rawUrn) {
      throw new Error(
        'Could not determine your profile URN. Make sure you are logged into LinkedIn.'
      );
    }

    // Convert fs_miniProfile → fsd_profile (same ID, different namespace)
    const profileId = rawUrn.split(':').pop();
    const mailboxUrn = `urn:li:fsd_profile:${profileId}`;
    log(`Mailbox URN: ${mailboxUrn}`);

    // Paginate conversations using cursor across every inbox category. LinkedIn
    // splits Focused / InMail / Message Requests / Other into separate categories;
    // if we only hit PRIMARY_INBOX we silently lose older InMail threads.
    if (BACKFILL_MODE) {
      log(
        NAME_FILTERS.length
          ? `Backfill mode: scanning all inbox categories for name matches ${JSON.stringify(NAME_FILTERS)}`
          : `Backfill mode (--full): scanning all inbox categories with no date cutoff`,
      );
    } else {
      log(`Fetching conversations from the last ${LOOKBACK_DAYS} days (since ${CUTOFF.toISOString().split('T')[0]})...`);
    }

    const byUrn = new Map();
    let totalApiErrors = 0;

    for (const category of INBOX_CATEGORIES) {
      if (byUrn.size >= MAX_CONVERSATIONS) break;

      let nextCursor = null;
      let reachedCutoff = false;
      let page = 0;
      let categoryCount = 0;

      while (!reachedCutoff && byUrn.size < MAX_CONVERSATIONS) {
        page++;
        const rawJson = await evaluate(
          fetchConversations(csrfToken, mailboxUrn, nextCursor, category),
        );
        const raw = rawJson ? JSON.parse(rawJson) : null;

        if (raw?.error) {
          totalApiErrors++;
          log(`  [${category}] API error on page ${page}: ${JSON.stringify(raw.error)}`);
          break;
        }

        const batch = parseConversations(raw);
        if (batch.length === 0) break;

        for (const convo of batch) {
          if (!convo.conversationUrn) continue;
          // In backfill mode we never early-break on date; we want the whole tail.
          if (!BACKFILL_MODE && convo.lastActivityAt && new Date(convo.lastActivityAt) < CUTOFF) {
            reachedCutoff = true;
            break;
          }
          // If name filters are active, skip anything that doesn't match.
          if (NAME_FILTERS.length) {
            const match = convo.participants.some(p =>
              NAME_FILTERS.some(n => (p.name || '').toLowerCase().includes(n)),
            );
            if (!match) continue;
          }
          if (!byUrn.has(convo.conversationUrn)) {
            byUrn.set(convo.conversationUrn, { ...convo, _category: category });
            categoryCount++;
          }
          if (byUrn.size >= MAX_CONVERSATIONS) break;
        }

        const collection = getConversationCollection(raw);
        nextCursor = collection?.metadata?.nextCursor || null;
        if (!nextCursor) break;

        await new Promise(r => setTimeout(r, 500));
      }

      log(`  [${category}] pulled ${categoryCount} conversation(s); running total ${byUrn.size}`);
    }

    const filtered = Array.from(byUrn.values());
    if (totalApiErrors > 0) {
      log(`  (${totalApiErrors} API error(s) encountered across categories)`);
    }
    log(
      BACKFILL_MODE
        ? `${filtered.length} conversations in backfill result set`
        : `${filtered.length} conversations within the last ${LOOKBACK_DAYS} days`,
    );

    // If --thread flag is set, filter to only matching conversations
    let toScrape = filtered;
    if (THREAD_FILTER) {
      toScrape = filtered.filter(c =>
        c.participants.some(p => p.name.toLowerCase().includes(THREAD_FILTER))
      );
      log(`Filtered to ${toScrape.length} conversation(s) matching "${THREAD_FILTER}"`);
      if (toScrape.length === 0) {
        log('No matching conversations found.');
      }
    }

    if (API_ONLY) {
      // API-only mode: fetch messages via GraphQL (capped at ~20 per conversation)
      log('Fetching messages via API (--api-only mode)...');
      for (let i = 0; i < toScrape.length; i++) {
        const convo = toScrape[i];
        if (!convo.conversationUrn) continue;
        const names = convo.participants.map(p => p.name).join(', ');
        try {
          const rawJson = await evaluate(fetchMessages(csrfToken, convo.conversationUrn));
          const raw = rawJson ? JSON.parse(rawJson) : null;
          if (raw && !raw.error) {
            const apiMsgs = parseMessages(raw);
            if (apiMsgs.length > 0) convo.messages = apiMsgs;
          }
          log(`  [${i + 1}/${toScrape.length}] ${names}: ${convo.messages.length} messages`);
        } catch (err) {
          log(`  [${i + 1}/${toScrape.length}] ${names}: API error (${err.message})`);
        }
        await new Promise(r => setTimeout(r, 300));
      }
    } else {
      // Full DOM scraping: navigate to each thread URL, scroll up, extract
      log('Scraping full message history via DOM...');
      await scrapeAllConversations(cdp, evaluate, toScrape, log);

      // API fallback for conversations where DOM scraping failed
      for (const convo of toScrape) {
        if (convo.messages.length <= 1 && convo.conversationUrn) {
          try {
            const rawJson = await evaluate(fetchMessages(csrfToken, convo.conversationUrn), 15000);
            const raw = rawJson ? JSON.parse(rawJson) : null;
            if (raw && !raw.error) {
              const apiMsgs = parseMessages(raw);
              if (apiMsgs.length > convo.messages.length) {
                const names = convo.participants.map(p => p.name).join(', ');
                log(`  API fallback for ${names}: ${apiMsgs.length} messages`);
                convo.messages = apiMsgs;
              }
            }
          } catch {}
        }
      }
    }

    // Merge with existing data when running a targeted refresh (thread/name backfill).
    fs.mkdirSync(DATA_DIR, { recursive: true });
    let finalConversations = filtered;

    const shouldMerge = (THREAD_FILTER || NAME_FILTERS.length) && fs.existsSync(INBOX_FILE);
    if (shouldMerge) {
      log('Merging updated conversations into existing data...');
      const existing = JSON.parse(fs.readFileSync(INBOX_FILE, 'utf8'));
      const scrapedUrns = new Set(toScrape.map(c => c.conversationUrn));

      finalConversations = existing.conversations.map(existingConvo => {
        if (scrapedUrns.has(existingConvo.conversationUrn)) {
          const fresh = toScrape.find(c => c.conversationUrn === existingConvo.conversationUrn);
          return { ...existingConvo, ...fresh };
        }
        return existingConvo;
      });

      for (const convo of filtered) {
        if (!existing.conversations.some(c => c.conversationUrn === convo.conversationUrn)) {
          finalConversations.push(convo);
        }
      }
    }

    // Write output
    const output = {
      scrapedAt: new Date().toISOString(),
      lookbackDays: LOOKBACK_DAYS,
      cutoffDate: CUTOFF.toISOString(),
      conversationCount: finalConversations.length,
      conversations: finalConversations,
    };
    fs.writeFileSync(INBOX_FILE, JSON.stringify(output, null, 2));
    log(`Saved ${finalConversations.length} conversations to ${INBOX_FILE}`);

  } finally {
    try { cdp.close(); } catch {}
    log('Done.');
  }
}

main().catch(e => {
  console.error(`FATAL: ${e.message}`);
  process.exit(1);
});
