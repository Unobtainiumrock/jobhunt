#!/usr/bin/env python3
"""
Unified Review UI for reply drafts and canonical workflow operations.

Usage:
  python -m pipeline.review_server
  python -m pipeline.review_server --port 8080
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from argparse import Namespace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from pipeline.config import (
    APPLICATIONS_DIR,
    CLASSIFIED_FILE,
    CONVERSATIONS_DIR,
    DATA_DIR,
    FOLLOWUP_QUEUE_FILE,
    INTERVIEW_LOOPS_DIR,
    LEAD_STATE_FILE,
    OPPORTUNITIES_DIR,
    PREP_ARTIFACTS_DIR,
    PROJECT_ROOT,
    TASKS_DIR,
    USER_NAME,
)
from pipeline.entity_workflow import (
    _add_interview_stage,
    _update_application_state,
    _update_interview_loop_state,
    _update_interview_stage_state,
    _update_task_state,
    load_workflow_state,
)
from pipeline.prep_packets import build_stage_prep_packet
from pipeline.research_enrichment import (
    _apply_job,
    _auto_queue_and_start,
    _load_queue,
    _poll_jobs,
    _start_jobs,
)
from pipeline.sync_entities import sync_entities
from pipeline.send_approved_exec import (
    SEND_SCRIPT,
    build_send_argv,
    env_truthy,
    run_send_approved_with_lock,
)
from infra.telegram_auth import verify_init_data

DEFAULT_PORT = 3457
SEND_HISTORY_FILE = DATA_DIR / "send_history.jsonl"
MOBILE_TEMPLATE_FILE = Path(__file__).resolve().parent / "mobile_template.html"

# JH-PH8: BAP pipeline data source. docker-compose mounts /opt/jobhunt/data
# read-only at /jobhunt. ``/jobhunt/jobhunt.db`` is the authoritative SQLite
# in Mode B; in Mode A it's the latest rsync push from the laptop. The
# review UI only reads it — no writes — so we open with ``mode=ro`` to
# prevent any code path from corrupting BAP state.
JOBHUNT_DIR = Path("/jobhunt")
JOBHUNT_DB = JOBHUNT_DIR / "jobhunt.db"
JOBHUNT_RESUMES_DIR = JOBHUNT_DIR / "tailored_resumes"
JOBHUNT_COVERS_DIR = JOBHUNT_DIR / "cover_letters"


def _load_mobile_html() -> str:
    if not MOBILE_TEMPLATE_FILE.exists():
        return "<!-- mobile_template.html missing -->"
    return MOBILE_TEMPLATE_FILE.read_text()


MOBILE_HTML = _load_mobile_html()
MOBILE_DRAFT_STATUSES = {"draft", "auto_send"}

_send_state_lock = threading.Lock()
_send_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_mode": None,
    "last_kind": None,
    "last_exit_code": None,
    "last_error": None,
    "stdout_tail": "",
}

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unified Review</title>
<style>
  :root {
    --bg: #0b1220;
    --surface: #121a2b;
    --surface-2: #172238;
    --border: #24324e;
    --text: #e8eefc;
    --muted: #9aa8c7;
    --accent: #5eead4;
    --green: #4ade80;
    --red: #fb7185;
    --yellow: #fbbf24;
    --blue: #60a5fa;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background:
      radial-gradient(circle at top left, rgba(96,165,250,0.12), transparent 30%),
      radial-gradient(circle at top right, rgba(94,234,212,0.10), transparent 28%),
      var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 2rem;
    max-width: 1200px;
    margin: 0 auto;
  }
  h1 { font-size: 1.7rem; margin-bottom: 0.25rem; }
  .subtitle { color: var(--muted); font-size: 0.95rem; margin-bottom: 1.5rem; }
  .tabs { display: flex; gap: 0.75rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  .tab-btn {
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    padding: 0.7rem 1rem;
    border-radius: 999px;
    cursor: pointer;
    font-size: 0.9rem;
    font-weight: 600;
  }
  .tab-btn.active {
    background: rgba(94,234,212,0.12);
    border-color: var(--accent);
    color: var(--accent);
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.9rem;
    margin-bottom: 1.5rem;
  }
  .stat {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 0.95rem 1rem;
  }
  .stat strong { display: block; font-size: 1.4rem; }
  .section-title {
    margin: 1.25rem 0 0.8rem;
    color: var(--muted);
    font-size: 0.88rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .card {
    background: linear-gradient(180deg, rgba(255,255,255,0.02), transparent), var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 1.1rem;
    margin-bottom: 1rem;
  }
  .card-header {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: flex-start;
    margin-bottom: 0.8rem;
  }
  .title { font-size: 1.05rem; font-weight: 700; }
  .meta { color: var(--muted); font-size: 0.84rem; margin-top: 0.15rem; }
  .badge {
    display: inline-block;
    padding: 0.22rem 0.6rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border: 1px solid var(--border);
    color: var(--text);
    background: var(--surface-2);
  }
  .badge-green { color: var(--green); border-color: rgba(74,222,128,0.35); background: rgba(74,222,128,0.10); }
  .badge-yellow { color: var(--yellow); border-color: rgba(251,191,36,0.35); background: rgba(251,191,36,0.10); }
  .badge-red { color: var(--red); border-color: rgba(251,113,133,0.35); background: rgba(251,113,133,0.10); }
  .badge-blue { color: var(--blue); border-color: rgba(96,165,250,0.35); background: rgba(96,165,250,0.10); }
  .muted-box {
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.8rem;
    margin: 0.75rem 0;
    color: var(--muted);
    font-size: 0.88rem;
    white-space: pre-wrap;
  }
  .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.75rem;
    margin: 0.85rem 0;
  }
  label {
    display: block;
    color: var(--muted);
    font-size: 0.78rem;
    margin-bottom: 0.32rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  input, textarea, select {
    width: 100%;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 0.7rem 0.8rem;
    font-family: inherit;
    font-size: 0.9rem;
  }
  textarea { min-height: 110px; resize: vertical; }
  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-top: 0.8rem;
  }
  .btn {
    padding: 0.55rem 0.9rem;
    border-radius: 9px;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text);
    cursor: pointer;
    font-size: 0.84rem;
    font-weight: 700;
  }
  .btn-primary { color: var(--accent); border-color: rgba(94,234,212,0.35); background: rgba(94,234,212,0.10); }
  .btn-green { color: var(--green); border-color: rgba(74,222,128,0.35); background: rgba(74,222,128,0.10); }
  .btn-yellow { color: var(--yellow); border-color: rgba(251,191,36,0.35); background: rgba(251,191,36,0.10); }
  .btn-red { color: var(--red); border-color: rgba(251,113,133,0.35); background: rgba(251,113,133,0.10); }
  .btn-gray { color: var(--muted); border-color: var(--border); background: var(--surface-2); }
  .btn[disabled], .btn[aria-disabled="true"] {
    opacity: 0.45;
    cursor: not-allowed;
    filter: grayscale(0.4);
    pointer-events: none;
  }
  .btn.pending {
    opacity: 0.7;
    cursor: wait;
    pointer-events: none;
  }
  .card.settled { opacity: 0.55; border-color: var(--border); }
  .card.settled .btn { pointer-events: none; }
  .resolved-note {
    margin-top: 0.6rem;
    font-size: 0.82rem;
    font-weight: 600;
  }
  .resolved-note.approved { color: var(--green); }
  .resolved-note.rejected { color: var(--red); }
  .resolved-note.abstained { color: var(--muted); }
  .hidden { display: none; }
  .empty {
    text-align: center;
    padding: 2.2rem;
    color: var(--muted);
    border: 1px dashed var(--border);
    border-radius: 14px;
  }
  @media (max-width: 640px) {
    body { padding: 1rem; }
    h1 { font-size: 1.35rem; }
    .tabs { gap: 0.4rem; }
    .tab-btn { padding: 0.6rem 0.8rem; font-size: 0.85rem; flex: 1 1 auto; text-align: center; }
    .stats { grid-template-columns: repeat(2, 1fr); gap: 0.6rem; }
    .stat { padding: 0.7rem; }
    .stat strong { font-size: 1.15rem; }
    .card { padding: 0.9rem; }
    .actions { gap: 0.4rem; }
    .btn { padding: 0.7rem 0.9rem; font-size: 0.9rem; flex: 1 1 auto; min-height: 44px; }
    textarea { font-size: 16px !important; }
    input, select { font-size: 16px !important; }
  }
</style>
</head>
<body>
<h1>Unified Review Dashboard</h1>
<p class="subtitle">Review reply drafts and operate the canonical application/interview workflow from one surface.</p>

<div class="tabs">
  <button class="tab-btn active" onclick="setTab('replies')">Replies</button>
  <button class="tab-btn" onclick="setTab('followups')">Follow-ups</button>
  <button class="tab-btn" onclick="setTab('workflow')">Workflow</button>
  <button class="tab-btn" onclick="setTab('applications')">Applications</button>
  <button class="tab-btn" onclick="setTab('pipeline')">Pipeline</button>
</div>

<div class="stats" id="stats"></div>
<div id="send-bar" class="muted-box" style="display:none;"></div>

<section id="replies-panel"></section>
<section id="followups-panel" class="hidden"></section>
<section id="workflow-panel" class="hidden"></section>
<section id="applications-panel" class="hidden"></section>
<section id="pipeline-panel" class="hidden"></section>

<script>
let state = {
  replies: [],
  followups: [],
  workflow: { applications: [], interview_loops: [], research_jobs: [], summary: {} },
  sendStatus: { running: false, history: [] },
  applications: [],
  pipelineStats: { stages: {}, last_run: {}, score_distribution: [], recent_errors: [] },
  appFilter: 'all',
  activeTab: 'replies',
};

function badgeClass(status) {
  if (['approved', 'submitted', 'offer', 'completed', 'screening', 'interviewing', 'active', 'sent'].includes(status)) return 'badge badge-green';
  if (['draft', 'drafting', 'planned', 'scheduled', 'auto_send'].includes(status)) return 'badge badge-yellow';
  if (['rejected', 'withdrawn', 'cancelled', 'error'].includes(status)) return 'badge badge-red';
  return 'badge badge-blue';
}

function setTab(tab) {
  state.activeTab = tab;
  const tabLabel = {
    replies: 'replies', followups: 'follow-ups', workflow: 'workflow',
    applications: 'applications', pipeline: 'pipeline',
  }[tab];
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.toLowerCase() === tabLabel);
  });
  ['replies','followups','workflow','applications','pipeline'].forEach(t => {
    document.getElementById(t + '-panel').classList.toggle('hidden', tab !== t);
  });
  render();
}

async function load() {
  const [repliesResp, workflowResp, followupsResp, sendResp, appsResp, statsResp] = await Promise.all([
    fetch('/api/drafts'),
    fetch('/api/workflow'),
    fetch('/api/followups'),
    fetch('/api/send/status'),
    fetch('/api/applications'),
    fetch('/api/pipeline-stats'),
  ]);
  state.replies = await repliesResp.json();
  state.workflow = await workflowResp.json();
  state.followups = await followupsResp.json();
  state.sendStatus = await sendResp.json();
  state.applications = await appsResp.json();
  state.pipelineStats = await statsResp.json();
  render();
}

function approvedReplies() {
  return state.replies.filter(d => d.reply?.status === 'approved');
}

function approvedFollowups() {
  return state.followups.filter(f => f.status === 'approved');
}

function renderStats() {
  const stats = document.getElementById('stats');
  if (state.activeTab === 'replies') {
    const total = state.replies.length;
    const drafts = state.replies.filter(d => d.reply?.status === 'draft').length;
    const autoSend = state.replies.filter(d => d.reply?.status === 'auto_send').length;
    const approved = state.replies.filter(d => d.reply?.status === 'approved').length;
    const sent = state.replies.filter(d => d.reply?.status === 'sent').length;
    stats.innerHTML = `
      <div class="stat"><strong>${total}</strong>Total Replies</div>
      <div class="stat"><strong>${drafts}</strong>Pending Review</div>
      <div class="stat"><strong>${autoSend}</strong>Auto-send Ready</div>
      <div class="stat"><strong>${approved}</strong>Approved</div>
      <div class="stat"><strong>${sent}</strong>Sent</div>
    `;
    return;
  }

  if (state.activeTab === 'followups') {
    const total = state.followups.length;
    const drafts = state.followups.filter(f => f.status === 'draft').length;
    const approved = state.followups.filter(f => f.status === 'approved').length;
    const sent = state.followups.filter(f => f.status === 'sent').length;
    const rejected = state.followups.filter(f => f.status === 'rejected').length;
    stats.innerHTML = `
      <div class="stat"><strong>${total}</strong>Total Follow-ups</div>
      <div class="stat"><strong>${drafts}</strong>Draft</div>
      <div class="stat"><strong>${approved}</strong>Approved</div>
      <div class="stat"><strong>${sent}</strong>Sent</div>
      <div class="stat"><strong>${rejected}</strong>Rejected</div>
    `;
    return;
  }

  const summary = state.workflow.summary || {};
  stats.innerHTML = `
    <div class="stat"><strong>${summary.applications || 0}</strong>Applications</div>
    <div class="stat"><strong>${summary.interviewLoops || 0}</strong>Interview Loops</div>
    <div class="stat"><strong>${summary.scheduledInterviews || 0}</strong>Scheduled</div>
    <div class="stat"><strong>${summary.debriefTasks || 0}</strong>Debriefs</div>
    <div class="stat"><strong>${summary.openTasks || 0}</strong>Open Tasks</div>
    <div class="stat"><strong>${summary.researchJobs || 0}</strong>Research Jobs</div>
  `;
}

function renderRetrievalSection(reply) {
  const profileHits = reply.retrieved_profile_chunks || [];
  const similarHits = reply.retrieved_similar_messages || [];
  const queries = reply.retrieval_queries || {};
  const debug = reply.retrieval_debug || {};
  if (!reply.retrieval_query && !profileHits.length && !similarHits.length) return '';

  return `
    <details>
      <summary class="meta" style="cursor:pointer;">Retrieved context</summary>
      <div class="muted-box">
        <strong>Profile query:</strong> ${queries.profile || reply.retrieval_query || '(none)'}
        <br><strong>Similar-message query:</strong> ${queries.similar_messages || reply.retrieval_query || '(none)'}
        <br><strong>Profile debug:</strong> kept ${debug.profile?.kept || 0}/${debug.profile?.candidates_considered || 0}, floor ${debug.profile?.score_floor || 'n/a'}
        <br><strong>Similar debug:</strong> kept ${debug.similar_messages?.kept || 0}/${debug.similar_messages?.candidates_considered || 0}, floor ${debug.similar_messages?.score_floor || 'n/a'}, overlap ${debug.similar_messages?.overlap_floor || 'n/a'}
      </div>
      ${profileHits.length ? `<div class="section-title">Profile Hits</div>${profileHits.map(hit => `<div class="muted-box">[${hit.chunk_type || '?'}] score=${(hit.score || 0).toFixed(4)}\n${hit.text || ''}</div>`).join('')}` : ''}
      ${similarHits.length ? `<div class="section-title">Similar Recruiter Messages</div>${similarHits.map(hit => `<div class="muted-box">[${hit.other_participant || '?'}] sender=${hit.sender || '?'} score=${(hit.score || 0).toFixed(4)} overlap=${((hit.overlap || 0)).toFixed(2)}\n${hit.text || ''}</div>`).join('')}` : ''}
    </details>
  `;
}

function sendToolbar(kind) {
  const approvedCount = kind === 'replies' ? approvedReplies().length : approvedFollowups().length;
  const running = state.sendStatus?.running;
  const disabled = running ? 'disabled' : '';
  const label = kind === 'replies' ? 'replies' : 'follow-ups';
  return `
    <div class="actions" style="margin-bottom: 1rem;">
      <span class="meta"><strong>${approvedCount}</strong> approved ${label} queued.</span>
      <button class="btn btn-yellow" ${disabled} onclick="triggerSend('${kind}', true)">Dry-run send</button>
      <button class="btn btn-green" ${disabled} onclick="triggerSend('${kind}', false)">Send Approved (live)</button>
      ${running ? '<span class="meta">Send already in progress...</span>' : ''}
    </div>
  `;
}

function renderSendBar() {
  const bar = document.getElementById('send-bar');
  const status = state.sendStatus || {};
  const history = status.history || [];
  if (!status.running && !history.length && !status.finished_at) {
    bar.style.display = 'none';
    bar.innerHTML = '';
    return;
  }
  bar.style.display = 'block';
  const headline = status.running
    ? `Sending in progress (${status.last_kind || '?'}, ${status.last_mode || '?'}) since ${status.started_at || '?'}`
    : (status.finished_at
        ? `Last send: ${status.last_kind || '?'} / ${status.last_mode || '?'} exit=${status.last_exit_code} at ${status.finished_at}`
        : 'Sender idle');
  const errorLine = status.last_error ? `<div class="meta" style="color: var(--red);">Error: ${status.last_error}</div>` : '';
  const tail = status.stdout_tail ? `<pre style="white-space: pre-wrap; margin: 0;">${status.stdout_tail}</pre>` : '';
  const historyLines = history.slice(-5).map(h => {
    const marker = h.ok ? 'OK' : 'FAIL';
    const verify = h.verified ? 'verified' : 'unverified';
    return `[${h.timestamp?.substring(11,19) || '?'}] ${marker} ${h.kind} ${verify} ${h.mode_used || ''} → ${h.recipient || '?'}: ${h.preview || ''}`;
  }).join('\\n');

  bar.innerHTML = `
    <strong>Send status:</strong> ${headline}
    ${errorLine}
    ${tail}
    ${historyLines ? `<details><summary class="meta" style="cursor:pointer;">Recent history (last 5)</summary><pre style="white-space: pre-wrap; margin: 0.4rem 0 0;">${historyLines}</pre></details>` : ''}
  `;
}

function renderReplies() {
  const container = document.getElementById('replies-panel');
  const toolbar = sendToolbar('replies');
  if (!state.replies.length) {
    container.innerHTML = toolbar + '<div class="empty">No reply drafts yet. Run the pipeline first.</div>';
    return;
  }

  container.innerHTML = toolbar + state.replies.map((convo, i) => {
    const reply = convo.reply || {};
    const score = convo.score || {};
    const meta = convo.metadata || {};
    const other = (convo.participants || []).find(p => p.name !== 'Nicholas J. Fleischhauer') || {name: 'Unknown', headline: ''};
    const msgs = (convo.messages || []).map(m =>
      '[' + (m.timestamp || '').substring(0,16) + '] ' + m.sender + ': ' + (m.text || '')
    ).join('\\n');

    const status = reply.status || 'draft';
    const isSettled = ['approved', 'sent', 'rejected', 'abstained', 'manually_handled'].includes(status);
    const resolvedClass = ['approved', 'sent'].includes(status) ? 'approved'
      : status === 'rejected' ? 'rejected'
      : ['abstained', 'manually_handled'].includes(status) ? 'abstained'
      : '';
    const resolvedLabel = {
      approved: 'Approved' + (reply.approved_at ? ' · ' + reply.approved_at.substring(0, 16) : ''),
      sent: 'Sent' + (reply.sent_at ? ' · ' + reply.sent_at.substring(0, 16) : ''),
      rejected: 'Rejected' + (reply.rejected_at ? ' · ' + reply.rejected_at.substring(0, 16) : ''),
      abstained: 'Abstained' + (reply.abstain_reason ? ' · ' + reply.abstain_reason : ''),
      manually_handled: 'Manually handled',
    }[status] || '';
    const dis = isSettled ? 'disabled' : '';

    return `
      <div class="card ${isSettled ? 'settled' : ''}" data-urn="${convo.conversationUrn || ''}">
        <div class="card-header">
          <div>
            <div class="title">${other.name}</div>
            <div class="meta">${other.headline || ''}</div>
            <div class="meta">${meta.role_title ? meta.role_title + ' at ' + (meta.company || '?') : ''}</div>
          </div>
          <span class="${badgeClass(status)}">${status}</span>
        </div>
        ${score.total != null ? `<div class="meta">Match score: ${score.total}/100</div>` : ''}
        ${reply.sent_at ? `<div class="meta">Sent ${reply.sent_at} via ${reply.send_mode || '?'} (verified=${reply.send_verified ? 'yes' : 'no'})</div>` : ''}
        ${reply.last_send_error ? `<div class="meta" style="color: var(--red);">Last send error: ${reply.last_send_error}</div>` : ''}
        ${reply.manually_edited ? '<div class="meta">(manually edited)</div>' : ''}
        <div class="muted-box">${reply.text || '(no reply generated)'}</div>
        ${renderRetrievalSection(reply)}
        <details>
          <summary class="meta" style="cursor:pointer;">Conversation context</summary>
          <div class="muted-box">${msgs}</div>
        </details>
        <div class="row">
          <div style="grid-column: 1 / -1;">
            <label>Edit Reply</label>
            <textarea id="reply-edit-${i}" ${dis}>${reply.text || ''}</textarea>
          </div>
        </div>
        <div class="actions">
          <button class="btn btn-green" ${dis} onclick="replyAction(this, ${i}, 'approve')">Approve</button>
          <button class="btn btn-red" ${dis} onclick="replyAction(this, ${i}, 'reject')">Reject</button>
          <button class="btn btn-gray" ${dis} onclick="replyAction(this, ${i}, 'mark_dead')" title="Lead is dead (out-of-band). Abstain and stop future follow-ups.">Mark Dead</button>
        </div>
        ${resolvedLabel ? `<div class="resolved-note ${resolvedClass}">${resolvedLabel}</div>` : ''}
      </div>
    `;
  }).join('');
}

function renderFollowups() {
  const container = document.getElementById('followups-panel');
  const toolbar = sendToolbar('followups');
  if (!state.followups.length) {
    container.innerHTML = toolbar + '<div class="empty">No follow-up drafts. Run <code>npm run followups</code> to generate them.</div>';
    return;
  }

  container.innerHTML = toolbar + state.followups.map((item, i) => {
    const status = item.status || 'draft';
    const isSettled = ['approved', 'sent', 'rejected'].includes(status);
    const resolvedClass = ['approved', 'sent'].includes(status) ? 'approved'
      : status === 'rejected' ? 'rejected'
      : '';
    const resolvedLabel = {
      approved: 'Approved' + (item.approved_at ? ' · ' + item.approved_at.substring(0, 16) : ''),
      sent: 'Sent' + (item.sent_at ? ' · ' + item.sent_at.substring(0, 16) : ''),
      rejected: 'Rejected' + (item.rejected_at ? ' · ' + item.rejected_at.substring(0, 16) : ''),
    }[status] || '';
    const dis = isSettled ? 'disabled' : '';
    return `
      <div class="card ${isSettled ? 'settled' : ''}">
        <div class="card-header">
          <div>
            <div class="title">${item.recipient || 'Unknown'} — follow-up #${item.followup_number || '?'}</div>
            <div class="meta">${item.company || ''} ${item.role_title ? '· ' + item.role_title : ''}</div>
            <div class="meta">task ${item.task_id || ''}</div>
          </div>
          <span class="${badgeClass(status)}">${status}</span>
        </div>
        ${item.score != null ? `<div class="meta">Opportunity score: ${item.score}/100</div>` : ''}
        ${item.sent_at ? `<div class="meta">Sent ${item.sent_at} via ${item.send_mode || '?'} (verified=${item.send_verified ? 'yes' : 'no'})</div>` : ''}
        ${item.last_send_error ? `<div class="meta" style="color: var(--red);">Last send error: ${item.last_send_error}</div>` : ''}
        ${item.referenced_quote ? `<details><summary class="meta" style="cursor:pointer;">Referenced quote</summary><div class="muted-box">${item.referenced_quote}</div></details>` : ''}
        <div class="row">
          <div style="grid-column: 1 / -1;">
            <label>Edit Follow-up</label>
            <textarea id="followup-edit-${i}" ${dis}>${item.message || ''}</textarea>
          </div>
        </div>
        <div class="actions">
          <button class="btn btn-green" ${dis} onclick="followupAction(this, ${i}, 'approve')">Approve</button>
          <button class="btn btn-red" ${dis} onclick="followupAction(this, ${i}, 'reject')">Reject</button>
          <button class="btn btn-yellow" ${dis} onclick="followupAction(this, ${i}, 'edit')">Save Edit Only</button>
        </div>
        ${resolvedLabel ? `<div class="resolved-note ${resolvedClass}">${resolvedLabel}</div>` : ''}
      </div>
    `;
  }).join('');
}

function renderWorkflow() {
  const container = document.getElementById('workflow-panel');
  const applications = state.workflow.applications || [];
  const interviews = state.workflow.interview_loops || [];
  const tasks = state.workflow.tasks || [];
  const researchJobs = state.workflow.research_jobs || [];

  const appCards = applications.length ? applications.map((item, i) => `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="title">${item.company || '?'} / ${item.role_title || '?'}</div>
          <div class="meta">${item.application_id}</div>
        </div>
        <span class="${badgeClass(item.status || 'planned')}">${item.status || 'planned'}</span>
      </div>
      <div class="meta">Submitted: ${item.submitted_at || 'not yet'}</div>
      <div class="meta">URL: ${item.application_url || 'none'}</div>
      <div class="row">
        <div>
          <label>Application URL</label>
          <input id="app-url-${i}" value="${item.application_url || ''}" placeholder="https://...">
        </div>
        <div>
          <label>Deadline</label>
          <input id="app-deadline-${i}" value="${item.deadline_at || ''}" placeholder="2026-04-20T17:00:00+00:00">
        </div>
      </div>
      <div class="actions">
        <button class="btn btn-yellow" onclick="workflowAction({kind:'application_status', application_id:'${item.application_id}', status:'drafting', application_url:document.getElementById('app-url-${i}').value, deadline_at:document.getElementById('app-deadline-${i}').value})">Drafting</button>
        <button class="btn btn-green" onclick="workflowAction({kind:'application_status', application_id:'${item.application_id}', status:'submitted', application_url:document.getElementById('app-url-${i}').value, deadline_at:document.getElementById('app-deadline-${i}').value})">Submitted</button>
        <button class="btn btn-primary" onclick="workflowAction({kind:'application_status', application_id:'${item.application_id}', status:'screening', application_url:document.getElementById('app-url-${i}').value})">Screening</button>
        <button class="btn btn-primary" onclick="workflowAction({kind:'application_status', application_id:'${item.application_id}', status:'interviewing', application_url:document.getElementById('app-url-${i}').value})">Interviewing</button>
        <button class="btn btn-green" onclick="workflowAction({kind:'application_status', application_id:'${item.application_id}', status:'offer', application_url:document.getElementById('app-url-${i}').value})">Offer</button>
        <button class="btn btn-red" onclick="workflowAction({kind:'application_status', application_id:'${item.application_id}', status:'rejected'})">Rejected</button>
      </div>
    </div>
  `).join('') : '<div class="empty">No canonical applications yet.</div>';

  const interviewCards = interviews.length ? interviews.map((item, i) => `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="title">${item.company || '?'} / ${item.role_title || '?'}</div>
          <div class="meta">${item.loop_id}</div>
          <div class="meta">Stages: ${(item.stages || []).length}</div>
        </div>
        <span class="${badgeClass(item.loop_status || 'active')}">${item.loop_status || 'active'}</span>
      </div>
      <div class="meta">Next step: ${item.next_step || 'none'}</div>
      <div class="meta">Debrief summary: ${item.debrief_summary || 'none'}</div>
      <div class="row">
        <div style="grid-column: 1 / -1;">
          <label>Next Step</label>
          <input id="loop-next-step-${i}" value="${item.next_step || ''}" placeholder="What should happen next">
        </div>
        <div style="grid-column: 1 / -1;">
          <label>Loop Debrief Summary</label>
          <textarea id="loop-debrief-summary-${i}" placeholder="Capture the loop debrief here">${item.debrief_summary || ''}</textarea>
        </div>
      </div>
      <div class="section-title">Stages</div>
      ${(item.stages || []).map((stage, stageIndex) => `
        <div class="card" style="margin-top: 0.8rem; background: var(--surface-2);">
          <div class="card-header">
            <div>
              <div class="title">${stage.kind || 'unknown'} / ${stage.stage_id || 'n/a'}</div>
              <div class="meta">Status: ${stage.status || 'planned'}</div>
            </div>
            <span class="${badgeClass(stage.status || 'planned')}">${stage.status || 'planned'}</span>
          </div>
          <div class="row">
            <div>
              <label>Scheduled At</label>
              <input id="stage-scheduled-${i}-${stageIndex}" value="${stage.scheduled_at || ''}" placeholder="2026-04-15T17:00:00+00:00">
            </div>
            <div>
              <label>Interviewers</label>
              <input id="stage-interviewers-${i}-${stageIndex}" value="${(stage.interviewer_names || []).join(', ')}" placeholder="Name 1, Name 2">
            </div>
            <div>
              <label>Duration Minutes</label>
              <input id="stage-duration-${i}-${stageIndex}" value="${stage.duration_minutes || ''}" placeholder="45">
            </div>
            <div style="grid-column: 1 / -1;">
              <label>Debrief</label>
              <textarea id="stage-debrief-${i}-${stageIndex}" placeholder="Capture stage-specific debrief here">${stage.debrief || ''}</textarea>
            </div>
          </div>
          <div class="meta">Goal: ${stage.prep_packet?.goal || 'No stage goal yet'}</div>
          <div class="meta">Prep artifacts: ${(stage.prep_packet?.artifact_titles || []).join(', ') || 'none linked yet'}</div>
          <div class="meta">Focus topics: ${(stage.prep_packet?.focus_topics || []).join(' | ') || 'none yet'}</div>
          <div class="meta">Talking points: ${(stage.prep_packet?.talking_points || []).join(' | ') || 'none yet'}</div>
          <div class="meta">Profile highlights: ${(stage.prep_packet?.profile_highlights || []).join(' | ') || 'none yet'}</div>
          <div class="meta">Suggested actions: ${(stage.prep_packet?.suggested_actions || []).join(' | ') || 'none yet'}</div>
          <div class="meta">Stage tasks: ${(stage.tasks || []).length ? (stage.tasks || []).map(task => `${task.title} [${task.status}]`).join(' | ') : 'none linked yet'}</div>
          <div class="actions">
            <button class="btn btn-yellow" onclick="workflowAction({kind:'interview_stage_status', loop_id:'${item.loop_id}', stage_id:'${stage.stage_id}', status:'planned', next_step:document.getElementById('loop-next-step-${i}').value})">Planned</button>
            <button class="btn btn-primary" onclick="workflowAction({kind:'interview_stage_status', loop_id:'${item.loop_id}', stage_id:'${stage.stage_id}', status:'scheduled', scheduled_at:document.getElementById('stage-scheduled-${i}-${stageIndex}').value, interviewer_names:document.getElementById('stage-interviewers-${i}-${stageIndex}').value, duration_minutes:parseInt(document.getElementById('stage-duration-${i}-${stageIndex}').value || '0', 10) || null, next_step:document.getElementById('loop-next-step-${i}').value})">Scheduled</button>
            <button class="btn btn-green" onclick="workflowAction({kind:'interview_stage_status', loop_id:'${item.loop_id}', stage_id:'${stage.stage_id}', status:'completed', debrief:document.getElementById('stage-debrief-${i}-${stageIndex}').value, next_step:document.getElementById('loop-next-step-${i}').value})">Completed</button>
            <button class="btn btn-red" onclick="workflowAction({kind:'interview_stage_status', loop_id:'${item.loop_id}', stage_id:'${stage.stage_id}', status:'cancelled', next_step:document.getElementById('loop-next-step-${i}').value})">Cancelled</button>
          </div>
        </div>
      `).join('')}
      <div class="section-title">Add Stage</div>
      <div class="row">
        <div>
          <label>Kind</label>
          <input id="new-stage-kind-${i}" value="technical" placeholder="technical">
        </div>
        <div>
          <label>After Stage ID</label>
          <input id="new-stage-after-${i}" value="${item.primary_stage.stage_id || ''}" placeholder="optional">
        </div>
        <div>
          <label>Scheduled At</label>
          <input id="new-stage-scheduled-${i}" value="" placeholder="2026-04-15T17:00:00+00:00">
        </div>
        <div>
          <label>Interviewers</label>
          <input id="new-stage-interviewers-${i}" value="" placeholder="Name 1, Name 2">
        </div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="workflowAction({kind:'add_interview_stage', loop_id:'${item.loop_id}', stage_kind:document.getElementById('new-stage-kind-${i}').value, after_stage_id:document.getElementById('new-stage-after-${i}').value || null, scheduled_at:document.getElementById('new-stage-scheduled-${i}').value || null, interviewer_names:document.getElementById('new-stage-interviewers-${i}').value || null, next_step:document.getElementById('loop-next-step-${i}').value})">Add Stage</button>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="workflowAction({kind:'interview_loop_status', loop_id:'${item.loop_id}', status:'active', next_step:document.getElementById('loop-next-step-${i}').value})">Loop Active</button>
        <button class="btn btn-green" onclick="workflowAction({kind:'interview_loop_status', loop_id:'${item.loop_id}', status:'completed', next_step:document.getElementById('loop-next-step-${i}').value, debrief_summary:document.getElementById('loop-debrief-summary-${i}').value})">Completed</button>
        <button class="btn btn-green" onclick="workflowAction({kind:'interview_loop_status', loop_id:'${item.loop_id}', status:'offer', next_step:document.getElementById('loop-next-step-${i}').value, debrief_summary:document.getElementById('loop-debrief-summary-${i}').value})">Offer</button>
        <button class="btn btn-red" onclick="workflowAction({kind:'interview_loop_status', loop_id:'${item.loop_id}', status:'rejected', next_step:document.getElementById('loop-next-step-${i}').value, debrief_summary:document.getElementById('loop-debrief-summary-${i}').value})">Rejected</button>
        <button class="btn btn-red" onclick="workflowAction({kind:'interview_loop_status', loop_id:'${item.loop_id}', status:'withdrawn', next_step:document.getElementById('loop-next-step-${i}').value, debrief_summary:document.getElementById('loop-debrief-summary-${i}').value})">Withdrawn</button>
      </div>
    </div>
  `).join('') : '<div class="empty">No canonical interview loops yet.</div>';

  const taskCards = tasks.length ? tasks.map((item, i) => `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="title">${item.title || item.task_id}</div>
          <div class="meta">${item.company || '?'} / ${item.role_title || '?'}</div>
          <div class="meta">${item.task_id}</div>
        </div>
        <span class="${badgeClass(item.status || 'not_started')}">${item.status || 'not_started'}</span>
      </div>
      <div class="meta">Kind: ${item.kind || 'other'} | Priority: ${item.priority || 'P2'}${item.interview_stage_id ? ' | Stage: ' + item.interview_stage_id : ''}</div>
      <div class="row">
        <div>
          <label>Due At</label>
          <input id="task-due-${i}" value="${item.due_at || ''}" placeholder="2026-04-15T17:00:00+00:00">
        </div>
        <div style="grid-column: 1 / -1;">
          <label>Notes</label>
          <textarea id="task-notes-${i}" placeholder="Task notes">${item.notes || ''}</textarea>
        </div>
      </div>
      <div class="actions">
        <button class="btn btn-yellow" onclick="workflowAction({kind:'task_status', task_id:'${item.task_id}', status:'not_started', due_at:document.getElementById('task-due-${i}').value, notes:document.getElementById('task-notes-${i}').value})">Not Started</button>
        <button class="btn btn-primary" onclick="workflowAction({kind:'task_status', task_id:'${item.task_id}', status:'in_progress', due_at:document.getElementById('task-due-${i}').value, notes:document.getElementById('task-notes-${i}').value})">In Progress</button>
        <button class="btn btn-primary" onclick="workflowAction({kind:'task_status', task_id:'${item.task_id}', status:'waiting', due_at:document.getElementById('task-due-${i}').value, notes:document.getElementById('task-notes-${i}').value})">Waiting</button>
        <button class="btn btn-red" onclick="workflowAction({kind:'task_status', task_id:'${item.task_id}', status:'blocked', due_at:document.getElementById('task-due-${i}').value, notes:document.getElementById('task-notes-${i}').value})">Blocked</button>
        <button class="btn btn-green" onclick="workflowAction({kind:'task_status', task_id:'${item.task_id}', status:'complete', due_at:document.getElementById('task-due-${i}').value, notes:document.getElementById('task-notes-${i}').value})">Complete</button>
        <button class="btn btn-red" onclick="workflowAction({kind:'task_status', task_id:'${item.task_id}', status:'cancelled', due_at:document.getElementById('task-due-${i}').value, notes:document.getElementById('task-notes-${i}').value})">Cancelled</button>
      </div>
    </div>
  `).join('') : '<div class="empty">No canonical tasks yet.</div>';

  const researchCards = researchJobs.length ? researchJobs.map((item, i) => `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="title">${item.company || '?'} / ${item.role_title || '?'}</div>
          <div class="meta">${item.job_id}</div>
          <div class="meta">${item.opportunity_id || 'no linked opportunity'}</div>
        </div>
        <span class="${badgeClass(item.status || 'queued')}">${item.status || 'queued'}</span>
      </div>
      <div class="meta">Report: ${item.report_path || 'not generated yet'}</div>
      <div class="meta">Artifact: ${item.artifact_path || 'not generated yet'}</div>
      <div class="meta">Summary: ${item.artifact_summary || 'none yet'}</div>
      <div class="meta">Sources: ${(item.source_citations || []).join(' | ') || 'none yet'}</div>
      <div class="muted-box">${(item.artifact_context || []).join('\\n') || 'No parsed company context yet.'}</div>
      <div class="actions">
        <button class="btn btn-primary" onclick="workflowAction({kind:'research_start', job_id:'${item.job_id}'})">Start</button>
        <button class="btn btn-yellow" onclick="workflowAction({kind:'research_poll', job_id:'${item.job_id}'})">Poll</button>
        <button class="btn btn-green" onclick="workflowAction({kind:'research_apply', job_id:'${item.job_id}'})">Apply</button>
      </div>
    </div>
  `).join('') : '<div class="empty">No external research jobs yet.</div>';

  container.innerHTML = `
    <div class="section-title">Applications</div>
    ${appCards}
    <div class="section-title">Interviews</div>
    ${interviewCards}
    <div class="section-title">Tasks</div>
    ${taskCards}
    <div class="section-title">Research</div>
    <div class="actions" style="margin-bottom: 1rem;">
      <button class="btn btn-primary" onclick="workflowAction({kind:'research_auto'})">Auto Queue/Start</button>
    </div>
    ${researchCards}
  `;
}

function render() {
  renderStats();
  renderSendBar();
  renderReplies();
  renderFollowups();
  renderWorkflow();
  renderApplications();
  renderPipeline();
}

// ── JH-PH8: Applications + Pipeline tabs ──────────────────────────────────

function _appStatusClass(status) {
  if (status === 'applied') return 'badge badge-green';
  if (status === 'ready' || status === 'tailored') return 'badge badge-yellow';
  if (status === 'in_progress') return 'badge badge-blue';
  if (['failed','captcha','login_issue','expired','manual'].includes(status)) return 'badge badge-red';
  return 'badge badge-blue';
}

function _filteredApplications() {
  const f = state.appFilter;
  if (f === 'all') return state.applications;
  if (f === 'tailored+') {
    return state.applications.filter(a => ['tailored','ready','in_progress','applied'].includes(a.status));
  }
  return state.applications.filter(a => a.status === f);
}

function setAppFilter(f) { state.appFilter = f; renderApplications(); }

function _escape(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function _fmt_when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const secs = (Date.now() - d.getTime()) / 1000;
  if (secs < 60) return 'just now';
  if (secs < 3600) return Math.floor(secs/60) + 'm ago';
  if (secs < 86400) return Math.floor(secs/3600) + 'h ago';
  return Math.floor(secs/86400) + 'd ago';
}

function renderApplications() {
  const container = document.getElementById('applications-panel');
  if (!container || state.activeTab !== 'applications') return;
  const apps = _filteredApplications();
  const filters = ['all','tailored+','applied','ready','tailored','scored_eligible','scored_below','failed','in_progress'];
  const toolbar = `
    <div class="toolbar" style="margin-bottom:1rem;display:flex;gap:0.4rem;flex-wrap:wrap;">
      ${filters.map(f => `
        <button class="tab-btn ${state.appFilter === f ? 'active' : ''}"
                style="padding:0.35rem 0.75rem;font-size:0.8rem"
                onclick="setAppFilter('${f}')">${f}</button>
      `).join('')}
      <span class="muted" style="margin-left:auto;align-self:center;">${apps.length} of ${state.applications.length}</span>
    </div>
  `;
  if (apps.length === 0) {
    container.innerHTML = toolbar + '<div class="empty">No applications match the current filter.</div>';
    return;
  }
  container.innerHTML = toolbar + `
    <table class="apps-table" style="width:100%;border-collapse:collapse;font-size:0.9rem;">
      <thead>
        <tr style="text-align:left;border-bottom:1px solid var(--border);">
          <th style="padding:0.5rem;">Score</th>
          <th style="padding:0.5rem;">Company</th>
          <th style="padding:0.5rem;">Role</th>
          <th style="padding:0.5rem;">Location</th>
          <th style="padding:0.5rem;">Status</th>
          <th style="padding:0.5rem;">Resume</th>
          <th style="padding:0.5rem;">Cover</th>
          <th style="padding:0.5rem;">Link</th>
          <th style="padding:0.5rem;">When</th>
        </tr>
      </thead>
      <tbody>
        ${apps.map(a => `
          <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:0.5rem;font-weight:700;">${a.fit_score ?? '—'}</td>
            <td style="padding:0.5rem;">${_escape(a.company)}</td>
            <td style="padding:0.5rem;">${_escape(a.title)}</td>
            <td style="padding:0.5rem;color:var(--muted);">${_escape(a.location)}</td>
            <td style="padding:0.5rem;"><span class="${_appStatusClass(a.status)}">${a.status}</span></td>
            <td style="padding:0.5rem;">${a.tailored_resume_url ? `<a href="${a.tailored_resume_url}" target="_blank" style="color:var(--accent);">PDF</a>` : '—'}</td>
            <td style="padding:0.5rem;">${a.cover_letter_url ? `<a href="${a.cover_letter_url}" target="_blank" style="color:var(--accent);">PDF</a>` : '—'}</td>
            <td style="padding:0.5rem;">${a.application_url ? `<a href="${_escape(a.application_url)}" target="_blank" style="color:var(--muted);">↗</a>` : ''}</td>
            <td style="padding:0.5rem;color:var(--muted);" title="${_escape(a.applied_at || a.tailored_at || a.discovered_at || '')}">${_fmt_when(a.applied_at || a.tailored_at || a.discovered_at)}</td>
          </tr>
          ${a.apply_error ? `<tr><td colspan="9" style="padding:0 0.5rem 0.75rem 4rem;color:var(--red);font-size:0.82rem;">error: ${_escape(a.apply_error)}</td></tr>` : ''}
          ${a.score_reasoning ? `<tr><td colspan="9" style="padding:0 0.5rem 0.75rem 4rem;color:var(--muted);font-size:0.82rem;">${_escape(a.score_reasoning).slice(0,280)}</td></tr>` : ''}
        `).join('')}
      </tbody>
    </table>
  `;
}

function renderPipeline() {
  const container = document.getElementById('pipeline-panel');
  if (!container || state.activeTab !== 'pipeline') return;
  const s = state.pipelineStats;
  const stage = (k) => s.stages?.[k] ?? 0;
  const run = (k) => s.last_run?.[k] ? _fmt_when(s.last_run[k]) : '—';
  const distBars = (s.score_distribution || []).map(d => {
    const pct = Math.min(100, d.count * 3);
    return `
      <div style="display:flex;align-items:center;gap:0.5rem;margin:0.15rem 0;">
        <div style="width:2rem;text-align:right;font-weight:600;">${d.score}</div>
        <div style="flex:1;background:var(--surface-2);border-radius:4px;height:1rem;position:relative;">
          <div style="width:${pct}%;height:100%;background:var(--accent);border-radius:4px;"></div>
        </div>
        <div style="width:2rem;color:var(--muted);">${d.count}</div>
      </div>
    `;
  }).join('');
  container.innerHTML = `
    <div class="stats">
      <div class="stat"><strong>${stage('total')}</strong>Discovered</div>
      <div class="stat"><strong>${stage('enriched')}</strong>Enriched</div>
      <div class="stat"><strong>${stage('scored')}</strong>Scored</div>
      <div class="stat"><strong>${stage('eligible')}</strong>Eligible (≥7)</div>
      <div class="stat"><strong>${stage('tailored')}</strong>Tailored</div>
      <div class="stat"><strong>${stage('with_cover')}</strong>With cover</div>
      <div class="stat"><strong>${stage('ready')}</strong>Ready to apply</div>
      <div class="stat"><strong>${stage('in_progress')}</strong>In progress</div>
      <div class="stat"><strong>${stage('applied')}</strong>Applied</div>
      <div class="stat"><strong>${stage('failed')}</strong>Failed</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1.25rem;">
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem;">
        <h3 style="font-size:1rem;margin-bottom:0.5rem;">Last run</h3>
        <div>discovered: <span style="color:var(--muted);">${run('discovered')}</span></div>
        <div>scored: <span style="color:var(--muted);">${run('scored')}</span></div>
        <div>tailored: <span style="color:var(--muted);">${run('tailored')}</span></div>
        <div>applied: <span style="color:var(--muted);">${run('applied')}</span></div>
        <div>last attempt: <span style="color:var(--muted);">${run('attempted')}</span></div>
      </div>
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem;">
        <h3 style="font-size:1rem;margin-bottom:0.5rem;">Score distribution</h3>
        ${distBars || '<div class="empty">No scored rows yet.</div>'}
      </div>
    </div>
    ${(s.recent_errors && s.recent_errors.length) ? `
      <div style="margin-top:1.25rem;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem;">
        <h3 style="font-size:1rem;margin-bottom:0.5rem;">Recent apply errors</h3>
        ${s.recent_errors.map(e => `
          <div style="padding:0.35rem 0;border-bottom:1px solid var(--border);font-size:0.85rem;">
            <strong>${_escape(e.title)}</strong>
            <span class="muted" style="margin-left:0.5rem;">${_fmt_when(e.at)}</span>
            <div style="color:var(--red);">${_escape(e.error)}</div>
          </div>
        `).join('')}
      </div>
    ` : ''}
  `;
}

function _setActionPending(buttonEl, label) {
  if (!buttonEl) return null;
  const card = buttonEl.closest('.card');
  if (!card) return null;
  card.classList.add('settled');
  const buttons = card.querySelectorAll('.btn');
  buttons.forEach(b => {
    b.setAttribute('aria-disabled', 'true');
    b.disabled = true;
  });
  buttonEl.classList.add('pending');
  const orig = buttonEl.textContent;
  buttonEl.textContent = label;
  return () => {
    buttonEl.classList.remove('pending');
    buttonEl.textContent = orig;
  };
}

async function replyAction(btn, index, action) {
  const convo = state.replies[index];
  const text = document.getElementById('reply-edit-' + index).value;
  const clickedBtn = btn;
  const label = { approve: 'Approving…', reject: 'Rejecting…', mark_dead: 'Marking dead…' }[action] || 'Working…';
  const restore = _setActionPending(clickedBtn, label);
  try {
    const resp = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ urn: convo.conversationUrn, action, text }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      alert((data && data.error) || `Action failed (${resp.status})`);
      if (restore) restore();
      return;
    }
    if (action === 'approve' && data.autosend && !data.autosend.skipped && data.autosend.ok === false) {
      alert('Approved, but LinkedIn send failed: ' + (data.autosend.error || 'see server logs'));
    }
  } catch (err) {
    alert('Network error: ' + err);
    if (restore) restore();
    return;
  }
  await load();
}

async function followupAction(btn, index, action) {
  const item = state.followups[index];
  const text = document.getElementById('followup-edit-' + index).value;
  const clickedBtn = btn;
  const label = { approve: 'Approving…', reject: 'Rejecting…', edit: 'Saving…' }[action] || 'Working…';
  const restore = _setActionPending(clickedBtn, label);
  try {
    const resp = await fetch('/api/followups/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ task_id: item.task_id, action, text }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(err.error || 'Follow-up action failed');
      if (restore) restore();
      return;
    }
    const data = await resp.json().catch(() => ({}));
    if (action === 'approve' && data.autosend && !data.autosend.skipped) {
      if (data.autosend.ok === false) {
        alert('Approved, but LinkedIn send failed: ' + (data.autosend.error || 'see server logs'));
      }
    }
  } catch (err) {
    alert('Network error: ' + err);
    if (restore) restore();
    return;
  }
  await load();
}

async function triggerSend(kind, dryRun) {
  if (!dryRun && !confirm(`Really send approved ${kind} LIVE to LinkedIn?`)) return;
  const resp = await fetch('/api/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ kind, dry_run: dryRun, max: 8 }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(err.error || 'Send request rejected');
    return;
  }
  pollSendStatus();
}

let _sendPollTimer = null;
async function pollSendStatus() {
  if (_sendPollTimer) clearTimeout(_sendPollTimer);
  try {
    const resp = await fetch('/api/send/status');
    state.sendStatus = await resp.json();
    renderSendBar();
    if (state.sendStatus.running) {
      _sendPollTimer = setTimeout(pollSendStatus, 3000);
    } else {
      await load();
    }
  } catch {
    _sendPollTimer = setTimeout(pollSendStatus, 5000);
  }
}

async function workflowAction(payload) {
  const resp = await fetch('/api/workflow/action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = await resp.json();
    alert(err.error || 'Workflow action failed');
    return;
  }
  await load();
}

load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# JH-PH8: BAP applications + pipeline-stats payloads
# ---------------------------------------------------------------------------

def _jobhunt_conn():
    """Open the BAP SQLite read-only. Returns None if the mount is absent
    (e.g., running outside the compose stack) so handlers can degrade to
    an empty-state response rather than 500."""
    import sqlite3
    if not JOBHUNT_DB.exists():
        return None
    return sqlite3.connect(
        f"file:{JOBHUNT_DB}?mode=ro&immutable=1", uri=True,
        check_same_thread=False,
    )


def _derive_status(row: dict[str, Any]) -> str:
    """Collapse the multi-column BAP state into a single status token for UI."""
    if row.get("applied_at"):
        return "applied"
    s = (row.get("apply_status") or "").lower()
    if s == "in_progress":
        return "in_progress"
    if s in {"failed", "captcha", "login_issue", "expired", "manual"}:
        return s
    if row.get("tailored_resume_path") and row.get("application_url"):
        return "ready"
    if row.get("tailored_resume_path"):
        return "tailored"
    if row.get("fit_score") is not None:
        return "scored_eligible" if row["fit_score"] >= 7 else "scored_below"
    if row.get("full_description"):
        return "enriched"
    return "discovered"


def _pdf_link(field_value: str | None, kind: str) -> str | None:
    """Map a container-side ``/data/tailored_resumes/foo.pdf`` (or host
    equivalent) to the ``/jobhunt/<kind>/foo.pdf`` URL this server serves.

    Returns None if the referenced file isn't actually on disk — the UI
    skips the link rather than producing a broken 404.
    """
    if not field_value:
        return None
    name = Path(field_value).name
    base = JOBHUNT_RESUMES_DIR if kind == "resume" else JOBHUNT_COVERS_DIR
    if not (base / name).exists():
        return None
    return f"/jobhunt/{kind}/{name}"


def _build_applications_payload() -> list[dict[str, Any]]:
    conn = _jobhunt_conn()
    if conn is None:
        return []
    try:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(
            "SELECT url, title, site, location, salary, fit_score, score_reasoning, "
            "       tailored_resume_path, cover_letter_path, application_url, "
            "       discovered_at, tailored_at, applied_at, apply_status, apply_error, "
            "       apply_duration_ms, full_description "
            "FROM jobs ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        out.append({
            "url": d["url"],
            "title": d["title"] or "",
            "company": d["site"] or "",
            "location": d["location"] or "",
            "salary_hint": d["salary"] or "",
            "fit_score": d["fit_score"],
            "score_reasoning": d["score_reasoning"] or "",
            "status": _derive_status(d),
            "tailored_resume_url": _pdf_link(d["tailored_resume_path"], "resume"),
            "cover_letter_url": _pdf_link(d["cover_letter_path"], "cover"),
            "application_url": d["application_url"] or d["url"],
            "discovered_at": d["discovered_at"],
            "tailored_at": d["tailored_at"],
            "applied_at": d["applied_at"],
            "apply_error": d["apply_error"] or "",
            "apply_duration_ms": d["apply_duration_ms"],
        })
    return out


def _build_pipeline_stats_payload() -> dict[str, Any]:
    conn = _jobhunt_conn()
    if conn is None:
        return {"stages": {}, "last_run": {}, "score_distribution": [], "recent_errors": []}
    try:
        stages = dict(conn.execute(
            "SELECT 'total',                 COUNT(*) FROM jobs "
            "UNION ALL SELECT 'enriched',    COUNT(*) FROM jobs WHERE full_description IS NOT NULL "
            "UNION ALL SELECT 'scored',      COUNT(*) FROM jobs WHERE fit_score IS NOT NULL "
            "UNION ALL SELECT 'eligible',    COUNT(*) FROM jobs WHERE fit_score >= 7 "
            "UNION ALL SELECT 'tailored',    COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
            "UNION ALL SELECT 'with_cover',  COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL "
            "UNION ALL SELECT 'ready',       COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
            "                                                        AND application_url IS NOT NULL "
            "                                                        AND apply_status IS NULL "
            "                                                        AND applied_at IS NULL "
            "UNION ALL SELECT 'in_progress', COUNT(*) FROM jobs WHERE apply_status = 'in_progress' "
            "UNION ALL SELECT 'applied',     COUNT(*) FROM jobs WHERE applied_at IS NOT NULL "
            "UNION ALL SELECT 'failed',      COUNT(*) FROM jobs WHERE apply_status IN "
            "                                                        ('failed','captcha','login_issue','expired','manual')"
        ).fetchall())
        last_run = dict(conn.execute(
            "SELECT 'discovered', MAX(discovered_at) FROM jobs "
            "UNION ALL SELECT 'scored',    MAX(scored_at)         FROM jobs "
            "UNION ALL SELECT 'tailored',  MAX(tailored_at)       FROM jobs "
            "UNION ALL SELECT 'applied',   MAX(applied_at)        FROM jobs "
            "UNION ALL SELECT 'attempted', MAX(last_attempted_at) FROM jobs"
        ).fetchall())
        score_dist = [
            {"score": s, "count": c}
            for s, c in conn.execute(
                "SELECT fit_score, COUNT(*) FROM jobs "
                "WHERE fit_score IS NOT NULL GROUP BY fit_score ORDER BY fit_score DESC"
            ).fetchall()
        ]
        errors = [
            {"url": u, "title": t, "stage": "apply", "error": e, "at": ts}
            for u, t, e, ts in conn.execute(
                "SELECT url, title, apply_error, last_attempted_at FROM jobs "
                "WHERE apply_error IS NOT NULL "
                "ORDER BY last_attempted_at DESC LIMIT 10"
            ).fetchall()
        ]
    finally:
        conn.close()

    return {
        "stages": stages,
        "last_run": {k: v for k, v in last_run.items() if v},
        "score_distribution": score_dist,
        "recent_errors": errors,
    }


def _load_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _mark_lead_declined(urn: str, now_iso: str) -> None:
    """Flip lead_states[urn].status to 'declined' so the follow-up scheduler
    skips this thread. No-ops silently if the state file or entry is missing.
    """
    if not urn:
        return
    states: dict[str, Any] = {}
    if LEAD_STATE_FILE.exists():
        try:
            states = _load_json(LEAD_STATE_FILE)
        except json.JSONDecodeError:
            states = {}
    entry = states.get(urn) or {}
    entry.update({
        "status": "declined",
        "updated_at": now_iso,
        "marked_dead_at": now_iso,
    })
    states[urn] = entry
    LEAD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LEAD_STATE_FILE, "w") as handle:
        json.dump(states, handle, indent=2)
        handle.write("\n")


def _unmark_lead_declined(urn: str) -> bool:
    """Clear a 'declined' latch so the thread can be re-drafted on next
    pipeline run. Returns True if a latch was cleared.
    """
    if not urn or not LEAD_STATE_FILE.exists():
        return False
    try:
        states = _load_json(LEAD_STATE_FILE)
    except json.JSONDecodeError:
        return False
    entry = states.get(urn) or {}
    if entry.get("status") != "declined":
        return False
    del states[urn]
    with open(LEAD_STATE_FILE, "w") as handle:
        json.dump(states, handle, indent=2)
        handle.write("\n")
    return True


def _load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [_load_json(path) for path in sorted(directory.glob("*.json"))]


def _build_workflow_payload() -> dict[str, Any]:
    opportunities = {record["id"]: record for record in _load_records(OPPORTUNITIES_DIR)}
    tasks = _load_records(TASKS_DIR)
    task_map = {task["id"]: task for task in tasks}
    prep_artifacts = {record["id"]: record for record in _load_records(PREP_ARTIFACTS_DIR)}
    queue = _load_queue()
    applications = []
    for application in _load_records(APPLICATIONS_DIR):
        opportunity = opportunities.get(application.get("opportunity_id"), {})
        applications.append({
            "application_id": application["id"],
            "opportunity_id": application.get("opportunity_id"),
            "company": opportunity.get("company"),
            "role_title": opportunity.get("role_title"),
            "status": application.get("status"),
            "submitted_at": application.get("submitted_at"),
            "application_url": application.get("application_url"),
            "deadline_at": application.get("deadline_at"),
            "notes": application.get("notes"),
        })

    interview_loops = []
    for loop in _load_records(INTERVIEW_LOOPS_DIR):
        opportunity = opportunities.get(loop.get("opportunity_id"), {})
        stages = loop.get("stages") or []
        primary_stage = stages[0] if stages else {}
        interview_loops.append({
            "loop_id": loop["id"],
            "opportunity_id": loop.get("opportunity_id"),
            "company": opportunity.get("company"),
            "role_title": opportunity.get("role_title"),
            "loop_status": loop.get("status"),
            "next_step": loop.get("next_step"),
            "debrief_summary": loop.get("debrief_summary"),
            "primary_stage": {
                "stage_id": primary_stage.get("id"),
                "kind": primary_stage.get("kind"),
                "status": primary_stage.get("status"),
                "scheduled_at": primary_stage.get("scheduled_at"),
                "interviewer_names": primary_stage.get("interviewer_names", []),
                "debrief": primary_stage.get("debrief"),
            },
            "stages": [
                {
                    "stage_id": stage.get("id"),
                    "kind": stage.get("kind"),
                    "status": stage.get("status"),
                    "scheduled_at": stage.get("scheduled_at"),
                    "duration_minutes": stage.get("duration_minutes"),
                    "interviewer_names": stage.get("interviewer_names", []),
                    "debrief": stage.get("debrief"),
                    "task_ids": stage.get("task_ids", []),
                    "tasks": [
                        {
                            "task_id": task_map[task_id]["id"],
                            "title": task_map[task_id].get("title"),
                            "kind": task_map[task_id].get("kind"),
                            "status": task_map[task_id].get("status"),
                            "priority": task_map[task_id].get("priority"),
                        }
                        for task_id in stage.get("task_ids", [])
                        if task_id in task_map
                    ],
                    "prep_packet": build_stage_prep_packet(
                        stage,
                        opportunity,
                        [
                            prep_artifacts[artifact_id]
                            for artifact_id in stage.get("prep_artifact_ids", opportunity.get("prep_artifact_ids", []))
                            if artifact_id in prep_artifacts
                        ],
                    ),
                }
                for stage in stages
            ],
            "task_ids": loop.get("task_ids", []),
        })

    scheduled_interviews = sum(
        1
        for loop in interview_loops
        if any(stage.get("status") == "scheduled" for stage in loop.get("stages", []))
    )
    workflow_tasks = []
    for task in tasks:
        opportunity = opportunities.get(task.get("opportunity_id"), {})
        workflow_tasks.append({
            "task_id": task["id"],
            "title": task.get("title"),
            "kind": task.get("kind"),
            "status": task.get("status"),
            "priority": task.get("priority"),
            "due_at": task.get("due_at"),
            "notes": task.get("notes"),
            "company": opportunity.get("company"),
            "role_title": opportunity.get("role_title"),
            "interview_loop_id": task.get("interview_loop_id"),
            "interview_stage_id": task.get("interview_stage_id"),
            "opportunity_id": task.get("opportunity_id"),
        })
    debrief_tasks = [
        task for task in workflow_tasks
        if task.get("kind") == "admin" and task.get("interview_loop_id") and task.get("status") != "complete"
    ]
    open_tasks = [task for task in workflow_tasks if task.get("status") not in {"complete", "cancelled"}]
    research_jobs = []
    for job in queue.get("jobs", []):
        artifact = None
        artifact_path = job.get("artifact_path")
        if artifact_path:
            path = Path(artifact_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent / artifact_path
            if path.exists():
                artifact = _load_json(path)
        research_jobs.append({
            "job_id": job.get("id"),
            "company": job.get("company"),
            "role_title": job.get("role_title"),
            "opportunity_id": job.get("opportunity_id"),
            "status": job.get("status"),
            "submitted_at": job.get("submitted_at"),
            "completed_at": job.get("completed_at"),
            "report_path": job.get("report_path"),
            "artifact_path": job.get("artifact_path"),
            "artifact_summary": (artifact or {}).get("summary"),
            "artifact_context": ((artifact or {}).get("structured_data") or {}).get("company_context", []),
            "source_citations": ((artifact or {}).get("structured_data") or {}).get("source_citations", []),
        })

    return {
        "applications": applications,
        "interview_loops": interview_loops,
        "tasks": workflow_tasks,
        "research_jobs": research_jobs,
        "workflow_state": load_workflow_state(),
        "summary": {
            "applications": len(applications),
            "interviewLoops": len(interview_loops),
            "scheduledInterviews": scheduled_interviews,
            "debriefTasks": len(debrief_tasks),
            "openTasks": len(open_tasks),
            "researchJobs": len(research_jobs),
        },
    }


def _build_followups_payload() -> list[dict[str, Any]]:
    """Roll up follow-up drafts with recipient + opportunity context."""
    queue = _load_json(FOLLOWUP_QUEUE_FILE) if FOLLOWUP_QUEUE_FILE.exists() else {"followups": []}
    followups = queue.get("followups", [])
    if not followups:
        return []

    conversations = {record["id"]: record for record in _load_records(CONVERSATIONS_DIR)}
    opportunities = {record["id"]: record for record in _load_records(OPPORTUNITIES_DIR)}

    out: list[dict[str, Any]] = []
    for entry in followups:
        conversation = conversations.get(entry.get("conversation_id", ""), {}) or {}
        opportunity = opportunities.get(entry.get("opportunity_id", ""), {}) or {}
        participants = conversation.get("participants", []) or []
        recipient = next(
            (p.get("name", "") for p in participants if p.get("name") != USER_NAME),
            "Unknown",
        )

        out.append({
            "task_id": entry.get("task_id"),
            "conversation_id": entry.get("conversation_id"),
            "opportunity_id": entry.get("opportunity_id"),
            "thread_id": entry.get("thread_id"),
            "followup_number": entry.get("followup_number"),
            "status": entry.get("status"),
            "message": entry.get("message"),
            "referenced_quote": entry.get("referenced_quote"),
            "generated_at": entry.get("generated_at"),
            "sent_at": entry.get("sent_at"),
            "send_mode": entry.get("send_mode"),
            "send_verified": entry.get("send_verified"),
            "recommended_next_state": entry.get("recommended_next_state"),
            "last_send_error": entry.get("last_send_error"),
            "recipient": recipient,
            "company": opportunity.get("company"),
            "role_title": opportunity.get("role_title"),
            "score": (opportunity.get("score") or {}).get("total"),
        })
    out.sort(key=_followup_sort_key, reverse=True)
    return out


def _reply_sort_key(convo: dict[str, Any]) -> tuple[int, str]:
    """Highest score first; ties broken by latest activity."""
    score_total = ((convo.get("score") or {}).get("total") or 0)
    try:
        score_value = int(score_total)
    except (TypeError, ValueError):
        score_value = 0
    messages = convo.get("messages") or []
    last_ts = ""
    if messages:
        last_ts = str(messages[-1].get("timestamp") or "")
    return (score_value, last_ts)


def _followup_sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    """Highest opportunity score first; ties broken by most recently generated."""
    score_total = entry.get("score") or 0
    try:
        score_value = int(score_total)
    except (TypeError, ValueError):
        score_value = 0
    generated = str(entry.get("generated_at") or "")
    return (score_value, generated)


def _apply_followup_action(task_id: str, action: str, edited_text: str | None) -> dict[str, Any]:
    if not FOLLOWUP_QUEUE_FILE.exists():
        raise ValueError("No follow-ups queue exists yet")
    queue = _load_json(FOLLOWUP_QUEUE_FILE)
    drafts = [item for item in queue.get("followups", []) if item.get("task_id") == task_id]
    if not drafts:
        raise ValueError(f"No follow-up found for task_id={task_id}")
    target = max(drafts, key=lambda item: item.get("generated_at") or "")

    now = datetime.now(timezone.utc).isoformat()
    if action == "approve":
        target["status"] = "approved"
        target["approved_at"] = now
        if edited_text and edited_text != target.get("message"):
            target["message"] = edited_text
            target["manually_edited"] = True
        target["approved_message"] = target.get("message", "")
    elif action == "reject":
        target["status"] = "rejected"
        target["rejected_at"] = now
    elif action == "edit":
        if edited_text:
            target["message"] = edited_text
            target["manually_edited"] = True
    else:
        raise ValueError(f"Unsupported follow-up action: {action}")

    FOLLOWUP_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FOLLOWUP_QUEUE_FILE, "w") as handle:
        json.dump(queue, handle, indent=2)
        handle.write("\n")
    return {"ok": True, "task_id": task_id, "status": target.get("status")}


def _read_send_history(limit: int = 20) -> list[dict[str, Any]]:
    if not SEND_HISTORY_FILE.exists():
        return []
    lines = SEND_HISTORY_FILE.read_text().splitlines()
    tail = [line for line in lines if line.strip()][-limit:]
    entries: list[dict[str, Any]] = []
    for line in tail:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _send_state_tail_from_proc(proc: subprocess.CompletedProcess[str]) -> None:
    tail_lines = (proc.stdout or "").splitlines()[-40:]
    with _send_state_lock:
        _send_state["running"] = False
        _send_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _send_state["last_exit_code"] = proc.returncode
        _send_state["stdout_tail"] = "\n".join(tail_lines)
        if proc.returncode != 0:
            _send_state["last_error"] = (proc.stderr or "").strip()[-400:]
        else:
            _send_state["last_error"] = None


def _autosend_reply_after_approve(urn: str) -> dict[str, Any]:
    """Immediately dispatch one approved reply when live sends are enabled."""
    if not env_truthy("LINKEDIN_SEND_ENABLED", default=False):
        return {"skipped": True, "reason": "LINKEDIN_SEND_ENABLED=0"}
    if not SEND_SCRIPT.exists():
        return {"ok": False, "error": f"sender script missing at {SEND_SCRIPT}"}
    with _send_state_lock:
        if _send_state.get("running"):
            return {"ok": False, "error": "send_in_progress"}
        _send_state.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "last_mode": "live",
            "last_kind": "replies",
            "last_exit_code": None,
            "last_error": None,
            "stdout_tail": "",
        })
    argv = build_send_argv(only="replies", max_items=1, live=True, reply_urn=urn)
    try:
        proc = run_send_approved_with_lock(argv)
    except Exception as exc:  # pragma: no cover — defensive
        with _send_state_lock:
            _send_state["running"] = False
            _send_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            _send_state["last_exit_code"] = -1
            _send_state["last_error"] = str(exc)
        return {"ok": False, "error": str(exc)}
    _send_state_tail_from_proc(proc)
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode}


def _autosend_followup_after_approve(task_id: str) -> dict[str, Any]:
    """Immediately dispatch one approved follow-up when live sends are enabled."""
    if not env_truthy("LINKEDIN_SEND_ENABLED", default=False):
        return {"skipped": True, "reason": "LINKEDIN_SEND_ENABLED=0"}
    if not SEND_SCRIPT.exists():
        return {"ok": False, "error": f"sender script missing at {SEND_SCRIPT}"}
    with _send_state_lock:
        if _send_state.get("running"):
            return {"ok": False, "error": "send_in_progress"}
        _send_state.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "last_mode": "live",
            "last_kind": "followups",
            "last_exit_code": None,
            "last_error": None,
            "stdout_tail": "",
        })
    argv = build_send_argv(
        only="followups", max_items=1, live=True, followup_task_id=task_id
    )
    try:
        proc = run_send_approved_with_lock(argv)
    except Exception as exc:  # pragma: no cover — defensive
        with _send_state_lock:
            _send_state["running"] = False
            _send_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            _send_state["last_exit_code"] = -1
            _send_state["last_error"] = str(exc)
        return {"ok": False, "error": str(exc)}
    _send_state_tail_from_proc(proc)
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode}


def _spawn_send_approved(
    kind: str,
    max_items: int,
    dry_run: bool,
    *,
    reply_urn: str | None = None,
    followup_task_id: str | None = None,
) -> dict[str, Any]:
    if not SEND_SCRIPT.exists():
        raise RuntimeError(f"Sender script missing at {SEND_SCRIPT}")

    # Server-side safety rail: live sends require explicit opt-in via env.
    # Dry runs are always allowed so operators can rehearse without risk.
    if not dry_run and not env_truthy("LINKEDIN_SEND_ENABLED", default=False):
        raise RuntimeError(
            "Live sending disabled (LINKEDIN_SEND_ENABLED=0 in env; set to 1 or remove to allow)"
        )

    with _send_state_lock:
        if _send_state.get("running"):
            raise RuntimeError("A send is already in progress")

        _send_state.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "last_mode": "dry_run" if dry_run else "live",
            "last_kind": kind,
            "last_exit_code": None,
            "last_error": None,
            "stdout_tail": "",
        })

    only_arg = kind if kind in ("replies", "followups", "all") else "all"
    argv = build_send_argv(
        only=only_arg,
        max_items=max_items,
        live=not dry_run,
        reply_urn=reply_urn,
        followup_task_id=followup_task_id,
    )

    def _runner() -> None:
        try:
            proc = run_send_approved_with_lock(argv)
            _send_state_tail_from_proc(proc)
        except Exception as exc:  # pragma: no cover — defensive
            with _send_state_lock:
                _send_state["running"] = False
                _send_state["finished_at"] = datetime.now(timezone.utc).isoformat()
                _send_state["last_exit_code"] = -1
                _send_state["last_error"] = str(exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    with _send_state_lock:
        return dict(_send_state)


def _workflow_action_result(body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind")
    if kind == "application_status":
        return _update_application_state(Namespace(
            application_id=body.get("application_id"),
            query=body.get("query"),
            status=body.get("status"),
            submitted_at=body.get("submitted_at"),
            application_url=body.get("application_url") or None,
            resume_variant=body.get("resume_variant"),
            cover_letter_variant=body.get("cover_letter_variant"),
            notes=body.get("notes"),
            deadline_at=body.get("deadline_at") or None,
        ))
    if kind == "interview_loop_status":
        return _update_interview_loop_state(Namespace(
            loop_id=body.get("loop_id"),
            query=body.get("query"),
            status=body.get("status"),
            next_step=body.get("next_step") or None,
            debrief_summary=body.get("debrief_summary") or None,
        ))
    if kind == "interview_stage_status":
        return _update_interview_stage_state(Namespace(
            loop_id=body.get("loop_id"),
            stage_id=body.get("stage_id"),
            query=body.get("query"),
            status=body.get("status"),
            scheduled_at=body.get("scheduled_at") or None,
            duration_minutes=body.get("duration_minutes"),
            interviewer_names=body.get("interviewer_names") or None,
            debrief=body.get("debrief") or None,
            loop_status=body.get("loop_status"),
            next_step=body.get("next_step") or None,
        ))
    if kind == "add_interview_stage":
        return _add_interview_stage(Namespace(
            loop_id=body.get("loop_id"),
            query=body.get("query"),
            kind=body.get("stage_kind"),
            stage_id=body.get("stage_id"),
            after_stage_id=body.get("after_stage_id"),
            status=body.get("status"),
            scheduled_at=body.get("scheduled_at") or None,
            duration_minutes=body.get("duration_minutes"),
            interviewer_names=body.get("interviewer_names") or None,
            debrief=body.get("debrief") or None,
            next_step=body.get("next_step") or None,
        ))
    if kind == "task_status":
        return _update_task_state(Namespace(
            task_id=body.get("task_id"),
            query=body.get("query"),
            status=body.get("status"),
            notes=body.get("notes"),
            due_at=body.get("due_at") or None,
        ))
    if kind == "research_start":
        return {"job_id": body.get("job_id"), "result": _start_jobs(Namespace(job_id=body.get("job_id")))}
    if kind == "research_poll":
        return {"job_id": body.get("job_id"), "result": _poll_jobs(Namespace(job_id=body.get("job_id")))}
    if kind == "research_apply":
        return {"job_id": body.get("job_id"), "result": _apply_job(Namespace(job_id=body.get("job_id")))}
    if kind == "research_auto":
        return {"result": _auto_queue_and_start(Namespace(no_start=False, include_contacted=False, limit=3))}
    raise ValueError(f"Unsupported workflow action: {kind}")


class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/drafts":
            self._serve_drafts()
        elif self.path == "/api/workflow":
            self._serve_workflow()
        elif self.path == "/api/followups":
            self._serve_followups()
        elif self.path == "/api/send/status":
            self._serve_send_status()
        elif self.path == "/api/applications":
            self._serve_applications()
        elif self.path == "/api/pipeline-stats":
            self._serve_pipeline_stats()
        elif self.path.startswith("/jobhunt/resume/"):
            self._serve_jobhunt_pdf(JOBHUNT_RESUMES_DIR, self.path[len("/jobhunt/resume/"):])
        elif self.path.startswith("/jobhunt/cover/"):
            self._serve_jobhunt_pdf(JOBHUNT_COVERS_DIR, self.path[len("/jobhunt/cover/"):])
        elif self.path == "/m/" or self.path == "/m":
            self._serve_mobile_html()
        elif self.path.startswith("/m/api/drafts"):
            if self._verify_mobile():
                self._serve_mobile_drafts()
        else:
            self.send_error(404)

    def _serve_applications(self) -> None:
        self._write_json(200, _build_applications_payload())

    def _serve_pipeline_stats(self) -> None:
        self._write_json(200, _build_pipeline_stats_payload())

    def _serve_jobhunt_pdf(self, base_dir: Path, name: str) -> None:
        """File-serve a tailored resume / cover letter PDF from the read-only
        JH-PH8 mount. Rejects anything that escapes the base directory."""
        from urllib.parse import unquote
        name = unquote(name)
        # Disallow traversal components outright — basename resolution is
        # defense-in-depth on top of the ``:ro`` compose mount.
        if "/" in name or ".." in name or not name:
            self.send_error(404)
            return
        path = base_dir / name
        try:
            if not path.is_file() or not path.resolve().is_relative_to(base_dir.resolve()):
                self.send_error(404)
                return
        except (OSError, ValueError):
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        content_type = "application/pdf" if name.lower().endswith(".pdf") else "text/plain; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path == "/api/action":
            self._handle_reply_action()
        elif self.path == "/api/workflow/action":
            self._handle_workflow_action()
        elif self.path == "/api/followups/action":
            self._handle_followup_action()
        elif self.path == "/api/send":
            self._handle_send_action()
        elif self.path == "/m/api/action":
            if self._verify_mobile():
                self._handle_reply_action()
        else:
            self.send_error(404)

    def _verify_mobile(self) -> bool:
        """Gate `/m/api/*` behind a valid Telegram WebApp initData payload.

        Accepts the signed payload via either the ``X-Telegram-Init-Data``
        request header or an ``initData`` query-string parameter (useful for
        GETs from a plain browser session during local debugging). Writes a
        401 on failure and returns False so the caller can short-circuit.
        """
        init_data = self.headers.get("X-Telegram-Init-Data", "").strip()
        if not init_data and "?" in self.path:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            init_data = (qs.get("initData") or [""])[0]
        if verify_init_data(init_data) is not None:
            return True
        self._write_json(401, {"ok": False, "error": "unauthorized"})
        return False

    def _serve_mobile_html(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(MOBILE_HTML.encode())

    def _serve_mobile_drafts(self) -> None:
        data = self._load_classified_data()
        conversations = data.get("conversations", [])
        drafts = [
            c for c in conversations
            if c.get("classification", {}).get("category") == "recruiter"
            and (c.get("reply") or {}).get("status") in MOBILE_DRAFT_STATUSES
        ]
        drafts.sort(key=_reply_sort_key, reverse=True)
        self._write_json(200, drafts)

    def _serve_html(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_TEMPLATE.encode())

    def _serve_drafts(self) -> None:
        data = self._load_classified_data()
        conversations = data.get("conversations", [])
        with_replies = [
            convo for convo in conversations
            if "reply" in convo and convo.get("classification", {}).get("category") == "recruiter"
        ]
        with_replies.sort(key=_reply_sort_key, reverse=True)
        self._write_json(200, with_replies)

    def _serve_workflow(self) -> None:
        self._write_json(200, _build_workflow_payload())

    def _handle_reply_action(self) -> None:
        body = self._read_json_body()
        urn = body.get("urn", "")
        action = body.get("action", "")
        edited_text = body.get("text", "")

        data = self._load_classified_data()
        for convo in data.get("conversations", []):
            if convo.get("conversationUrn") != urn or "reply" not in convo:
                continue
            if action == "approve":
                convo["reply"]["status"] = "approved"
                convo["reply"]["approved_at"] = datetime.now(timezone.utc).isoformat()
                if edited_text and edited_text != convo["reply"].get("text"):
                    convo["reply"]["text"] = edited_text
                    convo["reply"]["manually_edited"] = True
                convo["reply"]["approved_text"] = convo["reply"].get("text", "")
            elif action == "reject":
                convo["reply"]["status"] = "rejected"
                convo["reply"]["rejected_at"] = datetime.now(timezone.utc).isoformat()
            elif action == "reopen":
                # Clear a declined latch so this thread is eligible for a
                # fresh draft on the next pipeline run. Drops the existing
                # abstain reply and flips intent.abstain off; the classifier
                # re-runs against current tail + email because we reset its
                # input_hash.
                cleared = _unmark_lead_declined(urn)
                intent = convo.get("intent") or {}
                intent.pop("abstain_reason", None)
                intent["abstain"] = False
                intent["input_hash"] = ""
                convo["intent"] = intent
                convo.pop("reply", None)
                if not cleared:
                    # Still reset convo-level state; lead_states simply had
                    # no entry (e.g. old mark_dead before latching shipped).
                    pass
            elif action == "mark_dead":
                # Manual dead-end escape hatch: the operator knows this thread
                # is dead from out-of-band context (email rejection, verbal
                # decline, etc.). We stamp three places so every downstream
                # consumer stops drafting/sending:
                #   - reply.status = abstained  → review UI hides it
                #   - intent.abstain = True     → generate_reply skips it
                #   - lead_states[urn].status = declined → follow-up
                #     scheduler bucket drops it
                now = datetime.now(timezone.utc).isoformat()
                convo["reply"]["status"] = "abstained"
                convo["reply"]["abstain_reason"] = "manual_dead_end"
                convo["reply"]["marked_dead_at"] = now
                intent = convo.get("intent") or {}
                intent["abstain"] = True
                intent["abstain_reason"] = "manual_dead_end"
                intent["tag"] = "dead_end"
                intent["marked_dead_at"] = now
                convo["intent"] = intent
                _mark_lead_declined(urn, now)
            break

        with open(CLASSIFIED_FILE, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

        autosend: dict[str, Any] | None = None
        if action == "approve" and urn:
            autosend = _autosend_reply_after_approve(urn)
        self._write_json(200, {"ok": True, "action": action, "autosend": autosend})

    def _serve_followups(self) -> None:
        self._write_json(200, _build_followups_payload())

    def _serve_send_status(self) -> None:
        with _send_state_lock:
            state = dict(_send_state)
        state["history"] = _read_send_history(limit=20)
        self._write_json(200, state)

    def _handle_followup_action(self) -> None:
        body = self._read_json_body()
        task_id = body.get("task_id", "")
        action = body.get("action", "")
        edited_text = body.get("text") or body.get("message")
        try:
            result = _apply_followup_action(task_id, action, edited_text)
        except ValueError as exc:
            self._write_json(400, {"ok": False, "error": str(exc)})
            return
        if action == "approve" and task_id:
            result["autosend"] = _autosend_followup_after_approve(task_id)
        self._write_json(200, result)

    def _handle_send_action(self) -> None:
        body = self._read_json_body()
        kind = body.get("kind", "replies")
        max_items = int(body.get("max", 8) or 8)
        dry_run = bool(body.get("dry_run", False))
        if kind not in ("replies", "followups", "all"):
            self._write_json(400, {"ok": False, "error": f"Unsupported kind: {kind}"})
            return
        try:
            state = _spawn_send_approved(kind=kind, max_items=max_items, dry_run=dry_run)
        except RuntimeError as exc:
            self._write_json(409, {"ok": False, "error": str(exc)})
            return
        self._write_json(202, {"ok": True, "state": state})

    def _handle_workflow_action(self) -> None:
        body = self._read_json_body()
        try:
            result = _workflow_action_result(body)
            sync_entities()
        except Exception as exc:
            self._write_json(400, {"ok": False, "error": str(exc)})
            return
        self._write_json(200, {"ok": True, "result": result})

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _load_classified_data(self) -> dict[str, Any]:
        if CLASSIFIED_FILE.exists():
            return _load_json(CLASSIFIED_FILE)
        return {"conversations": []}

    def _write_json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(payload, indent=2).encode())
        except (BrokenPipeError, ConnectionResetError):
            # Client (typically Telegram WebView or healthdog probe)
            # disconnected before we finished writing. Idempotent endpoints
            # — the next probe will retry. Log at INFO instead of letting
            # the BaseHTTPRequestHandler emit a 30-line traceback.
            self.log_message("client disconnected mid-write: %s %s",
                             self.command, self.path)


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _find_open_port(host: str, start: int, max_attempts: int = 20) -> int:
    for offset in range(max_attempts):
        candidate = start + offset
        if _port_available(host, candidate):
            return candidate
    raise RuntimeError(f"No open port found in range {start}-{start + max_attempts - 1}")


def main() -> None:
    host = os.getenv("REVIEW_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    port = _find_open_port(host, port)
    HTTPServer.allow_reuse_address = True
    server = HTTPServer((host, port), ReviewHandler)
    display_host = "localhost" if host == "127.0.0.1" else host
    print(f"Review UI running at http://{display_host}:{port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
