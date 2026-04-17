#!/usr/bin/env node
/**
 * Approved-draft dispatcher for LinkedIn messages.
 *
 * Reads approved reply drafts from data/inbox_classified.json and approved
 * follow-up drafts from data/entities/followups.json, then dispatches each
 * via the DOM-primary sender (Voyager fallback if enabled).
 *
 * All sends are gated behind --live; without --live the dispatcher prints
 * what it would do but never calls LinkedIn.
 *
 * Usage:
 *   node src/send-approved.mjs --dry-run
 *   node src/send-approved.mjs --live
 *   node src/send-approved.mjs --live --only replies
 *   node src/send-approved.mjs --live --only followups --max 3
 *   node src/send-approved.mjs --live --thread 2-ZTk4YTE1ZTIt
 *   node src/send-approved.mjs --live --only replies --max 1 --reply-urn 'urn:li:...'
 *   node src/send-approved.mjs --live --only followups --max 1 --followup-task-id '<uuid>'
 */

import fs from 'fs';
import path from 'path';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import { connectToLinkedIn, getCSRFToken } from './cdp-client.mjs';
import { fetchCurrentProfile } from './linkedin-api.mjs';
import { sendMessage, threadIdFromUrn } from './linkedin-send.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'data');
const ENTITY_DIR = path.join(DATA_DIR, 'entities');

const CLASSIFIED_FILE = path.join(DATA_DIR, 'inbox_classified.json');
const FOLLOWUPS_FILE = path.join(ENTITY_DIR, 'followups.json');
const LEAD_STATE_FILE = path.join(DATA_DIR, 'lead_states.json');
const HISTORY_FILE = path.join(DATA_DIR, 'send_history.jsonl');

const args = process.argv.slice(2);
const DRY_RUN = !args.includes('--live');
const ONLY_IDX = args.indexOf('--only');
const ONLY = ONLY_IDX >= 0 ? args[ONLY_IDX + 1] : 'all';
const MAX_IDX = args.indexOf('--max');
const MAX = MAX_IDX >= 0 ? Math.max(1, parseInt(args[MAX_IDX + 1] || '8', 10)) : 8;
const THREAD_IDX = args.indexOf('--thread');
const THREAD_FILTER = THREAD_IDX >= 0 ? String(args[THREAD_IDX + 1] || '').toLowerCase() : null;
const REPLY_URN_IDX = args.indexOf('--reply-urn');
const REPLY_URN_EXACT = REPLY_URN_IDX >= 0 ? String(args[REPLY_URN_IDX + 1] || '').trim() : null;
const FOLLOWUP_TASK_IDX = args.indexOf('--followup-task-id');
const FOLLOWUP_TASK_EXACT = FOLLOWUP_TASK_IDX >= 0
  ? String(args[FOLLOWUP_TASK_IDX + 1] || '').trim()
  : null;
const INCLUDE_AUTO = args.includes('--include-auto-send');

const DELAY_MIN_MS = parseInt(process.env.LINKEDIN_SEND_DELAY_MIN || '45000', 10);
const DELAY_MAX_MS = parseInt(process.env.LINKEDIN_SEND_DELAY_MAX || '90000', 10);
const HARD_CAP = parseInt(process.env.LINKEDIN_MAX_SENDS_PER_RUN || '12', 10);

const USER_NAME = process.env.LINKEDIN_USER_NAME || 'Nicholas J. Fleischhauer';

function log(msg) {
  const ts = new Date().toISOString().substring(11, 19);
  console.log(`[${ts}] ${msg}`);
}

function readJson(file, fallback) {
  if (!fs.existsSync(file)) return fallback;
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function writeJsonAtomic(file, data) {
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`);
  fs.renameSync(tmp, file);
}

function appendHistory(entry) {
  fs.mkdirSync(path.dirname(HISTORY_FILE), { recursive: true });
  fs.appendFileSync(HISTORY_FILE, `${JSON.stringify(entry)}\n`);
}

function jitteredDelay() {
  const range = Math.max(0, DELAY_MAX_MS - DELAY_MIN_MS);
  return DELAY_MIN_MS + Math.floor(Math.random() * (range + 1));
}

function nowIso() {
  return new Date().toISOString();
}

function shortUrn(urn) {
  const tid = threadIdFromUrn(urn || '');
  return tid ? tid.slice(0, 18) : urn.slice(-18);
}

function notifyTelegram(message) {
  return new Promise((resolve) => {
    try {
      const notifier = path.join(ROOT, 'infra', 'notify.py');
      if (!fs.existsSync(notifier)) return resolve(false);
      const child = spawn('python', [notifier, '--message', message], {
        cwd: ROOT,
        stdio: 'ignore',
      });
      child.on('error', () => resolve(false));
      child.on('exit', () => resolve(true));
    } catch {
      resolve(false);
    }
  });
}

function participantName(convo) {
  const parts = convo.participants || [];
  const other = parts.find((p) => p.name !== USER_NAME);
  return other ? other.name : 'Unknown';
}

function isTamperingDetected(originalText, currentText, manuallyEdited) {
  if (!originalText) return false;
  if (originalText === currentText) return false;
  return !manuallyEdited;
}

function filterReplies(classified) {
  const actionable = INCLUDE_AUTO ? new Set(['approved', 'auto_send']) : new Set(['approved']);
  const out = [];
  const skipped = [];
  for (const convo of classified.conversations || []) {
    const reply = convo.reply;
    if (!reply) continue;
    if (!actionable.has(reply.status)) continue;
    if (convo.classification?.category !== 'recruiter') continue;

    const urn = convo.conversationUrn || '';
    if (REPLY_URN_EXACT && urn !== REPLY_URN_EXACT) continue;
    if (THREAD_FILTER && !urn.toLowerCase().includes(THREAD_FILTER) &&
      !participantName(convo).toLowerCase().includes(THREAD_FILTER)) continue;

    if (reply.safety_passed !== true) {
      skipped.push({ urn, recipient: participantName(convo), reason: 'safety_passed_not_true' });
      continue;
    }
    if (isTamperingDetected(reply.approved_text, reply.text, reply.manually_edited)) {
      skipped.push({
        urn,
        recipient: participantName(convo),
        reason: 'text_changed_without_manually_edited_flag',
      });
      continue;
    }

    out.push({
      kind: 'reply',
      urn,
      text: reply.text || '',
      recipient: participantName(convo),
      company: convo.metadata?.company || 'Unknown',
      role: convo.metadata?.role_title || '',
      score: convo.score?.total || 0,
      status: reply.status,
    });
  }
  out.sort((a, b) => b.score - a.score);
  return { items: out, skipped };
}

function filterFollowups(queue) {
  const out = [];
  const skipped = [];
  for (const entry of queue.followups || []) {
    if (entry.status !== 'approved') continue;
    if (FOLLOWUP_TASK_EXACT && entry.task_id !== FOLLOWUP_TASK_EXACT) continue;
    const urn = entry.thread_id || '';
    if (THREAD_FILTER && !urn.toLowerCase().includes(THREAD_FILTER) &&
      !String(entry.task_id || '').toLowerCase().includes(THREAD_FILTER)) continue;

    if (isTamperingDetected(entry.approved_message, entry.message, entry.manually_edited)) {
      skipped.push({
        urn,
        task_id: entry.task_id,
        reason: 'text_changed_without_manually_edited_flag',
      });
      continue;
    }

    out.push({
      kind: 'followup',
      urn,
      text: entry.message || '',
      task_id: entry.task_id,
      conversation_id: entry.conversation_id,
      opportunity_id: entry.opportunity_id,
      followup_number: entry.followup_number,
      recommended_next_state: entry.recommended_next_state,
      score: 0,
      status: entry.status,
    });
  }
  return { items: out, skipped };
}

function updateReplyState(urn, patch) {
  const data = readJson(CLASSIFIED_FILE, { conversations: [] });
  for (const convo of data.conversations || []) {
    if (convo.conversationUrn !== urn) continue;
    if (!convo.reply) convo.reply = {};
    Object.assign(convo.reply, patch);
    break;
  }
  writeJsonAtomic(CLASSIFIED_FILE, data);
}

function updateFollowupState(taskId, patch, leadStatePatch) {
  const queue = readJson(FOLLOWUPS_FILE, { followups: [] });
  const drafts = (queue.followups || []).filter((f) => f.task_id === taskId);
  if (drafts.length === 0) return;
  const target = drafts.reduce((a, b) =>
    (a.generated_at || '') > (b.generated_at || '') ? a : b);
  Object.assign(target, patch);
  writeJsonAtomic(FOLLOWUPS_FILE, queue);

  if (leadStatePatch) {
    const states = readJson(LEAD_STATE_FILE, {});
    const threadId = target.thread_id;
    if (threadId) {
      const existing = states[threadId] || {};
      const history = existing.followup_history || [];
      if (leadStatePatch.append_history) history.push(leadStatePatch.append_history);
      states[threadId] = {
        ...existing,
        status: leadStatePatch.status || existing.status || 'awaiting_response',
        updated_at: leadStatePatch.updated_at,
        last_outbound_at: leadStatePatch.last_outbound_at,
        followup_history: history,
      };
      writeJsonAtomic(LEAD_STATE_FILE, states);
    }
  }
}

async function dispatchOne(ctx, item) {
  const { evaluate, cdp, csrfToken, mailboxUrn } = ctx;
  if (DRY_RUN) {
    log(`[dry-run] ${item.kind} → ${item.recipient || shortUrn(item.urn)} ` +
      `(${item.text.slice(0, 60).replace(/\n/g, ' ')}...)`);
    return { ok: true, dry_run: true };
  }

  log(`Sending ${item.kind} → ${item.recipient || shortUrn(item.urn)} ` +
    `(score=${item.score}, status=${item.status})`);

  const result = await sendMessage({
    cdp,
    evaluate,
    conversationUrn: item.urn,
    text: item.text,
    userName: USER_NAME,
    mode: 'auto',
    csrfToken,
    mailboxUrn,
  });

  const sentAt = nowIso();

  if (item.kind === 'reply') {
    if (result.ok) {
      updateReplyState(item.urn, {
        status: 'sent',
        sent_at: sentAt,
        send_mode: result.mode_used,
        send_verified: result.verified,
      });
    } else {
      updateReplyState(item.urn, {
        last_send_error: result.error || 'unknown',
        last_send_attempt_at: sentAt,
      });
    }
  } else if (item.kind === 'followup' && item.task_id) {
    if (result.ok) {
      updateFollowupState(item.task_id, {
        status: 'sent',
        sent_at: sentAt,
        send_mode: result.mode_used,
        send_verified: result.verified,
      }, {
        status: item.recommended_next_state || 'awaiting_response',
        updated_at: sentAt,
        last_outbound_at: sentAt,
        append_history: {
          task_id: item.task_id,
          conversation_id: item.conversation_id,
          followup_number: item.followup_number,
          sent_at: sentAt,
          message: item.text,
          send_mode: result.mode_used,
          verified: result.verified,
        },
      });
    } else {
      updateFollowupState(item.task_id, {
        last_send_error: result.error || 'unknown',
        last_send_attempt_at: sentAt,
      });
    }
  }

  appendHistory({
    timestamp: sentAt,
    kind: item.kind,
    urn: item.urn,
    recipient: item.recipient || null,
    ok: result.ok,
    mode_used: result.mode_used,
    verified: result.verified,
    authwall: result.authwall || false,
    error: result.error || null,
    preview: item.text.slice(0, 120).replace(/\n/g, ' '),
  });

  if (!result.ok || !result.verified || result.authwall) {
    const failureReason = result.authwall
      ? 'LinkedIn authwall / session expired'
      : result.error || 'unverified send';
    const alert = `LinkedIn send issue (${item.kind}):\n` +
      `recipient: ${item.recipient || shortUrn(item.urn)}\n` +
      `mode: ${result.mode_used || 'none'}\n` +
      `verified: ${result.verified}\n` +
      `error: ${failureReason}\n` +
      `preview: ${item.text.slice(0, 80).replace(/\n/g, ' ')}`;
    await notifyTelegram(alert);
  }

  return result;
}

async function main() {
  const classified = readJson(CLASSIFIED_FILE, { conversations: [] });
  const queue = readJson(FOLLOWUPS_FILE, { followups: [] });

  const effectiveMax = Math.min(MAX, HARD_CAP);
  if (MAX > HARD_CAP) {
    log(`--max ${MAX} exceeds hard cap ${HARD_CAP}; clamping.`);
  }
  const repliesResult = ONLY === 'followups' ? { items: [], skipped: [] } : filterReplies(classified);
  const followupsResult = ONLY === 'replies' ? { items: [], skipped: [] } : filterFollowups(queue);

  const replies = repliesResult.items;
  const followups = followupsResult.items;

  const skipped = [...repliesResult.skipped, ...followupsResult.skipped];
  if (skipped.length) {
    log(`Skipped ${skipped.length} item(s) due to safety rails:`);
    for (const s of skipped) {
      log(`  - ${s.recipient || s.task_id || shortUrn(s.urn)}: ${s.reason}`);
    }
    await notifyTelegram(
      `LinkedIn send: ${skipped.length} item(s) refused by safety rails ` +
      `(first: ${skipped[0].reason})`
    );
  }

  const items = [...replies, ...followups].slice(0, effectiveMax);

  log(`Approved queue: ${replies.length} reply / ${followups.length} follow-up ` +
    `(sending ${items.length}, cap=${effectiveMax})`);

  if (items.length === 0) {
    log('Nothing to send.');
    return;
  }

  if (DRY_RUN) {
    for (const item of items) {
      log(`[dry-run] ${item.kind.padEnd(9)} → ${(item.recipient || shortUrn(item.urn)).padEnd(28)}` +
        ` | score=${item.score} | ${item.text.slice(0, 60).replace(/\n/g, ' ')}...`);
    }
    log('Dry-run complete. Rerun with --live to actually send.');
    return;
  }

  let ctx;
  try {
    log('Connecting to Chrome via CDP...');
    ctx = await connectToLinkedIn();
    ctx.csrfToken = await getCSRFToken(ctx.evaluate);

    const meRaw = await ctx.evaluate(fetchCurrentProfile(ctx.csrfToken));
    const me = meRaw ? JSON.parse(meRaw) : null;
    const miniProfile = me?.included?.find((i) => i.firstName);
    const rawUrn = miniProfile?.entityUrn || me?.data?.entityUrn || me?.data?.['*miniProfile'];
    if (!rawUrn) throw new Error('Could not determine profile URN (login expired?)');
    const profileId = rawUrn.split(':').pop();
    ctx.mailboxUrn = `urn:li:fsd_profile:${profileId}`;
    log(`Connected as mailbox: ${ctx.mailboxUrn}`);
  } catch (err) {
    log(`CDP setup failed: ${err.message}`);
    await notifyTelegram(`LinkedIn send aborted: CDP setup failed (${err.message})`);
    process.exit(1);
  }

  let successes = 0;
  let failures = 0;
  try {
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      try {
        const result = await dispatchOne(ctx, item);
        if (result.ok) successes++; else failures++;
      } catch (err) {
        failures++;
        log(`  unhandled error on ${shortUrn(item.urn)}: ${err.message}`);
        await notifyTelegram(`LinkedIn send unhandled exception: ${err.message}`);
      }

      if (i < items.length - 1) {
        const delay = jitteredDelay();
        log(`Pacing delay: ${Math.round(delay / 1000)}s`);
        await new Promise((r) => setTimeout(r, delay));
      }
    }
  } finally {
    try { ctx.cdp.close(); } catch {}
  }

  log(`Done. ${successes} ok, ${failures} failed.`);
  if (failures > 0) process.exit(2);
}

main().catch(async (err) => {
  log(`Fatal: ${err.message}`);
  await notifyTelegram(`LinkedIn send fatal: ${err.message}`);
  process.exit(1);
});
