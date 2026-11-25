#!/usr/bin/env node
/**
 * dsh2trace — convert a DeepSeek Harness (DSH) native session log into a
 * Playground ARM trace (trace/trace.jsonl).
 *
 * WHY THIS EXISTS
 * ---------------
 * `playground trace validate` recognises Claude Code / OpenCode / OpenClaw JSONL
 * shapes. DSH writes `session.v4.jsonl.zstd`, whose rows look like
 *
 *   {"type":"tool/call","seq":21,"time":<epoch_ms>,"data":{"callId":"call_...","name":"pwsh",
 *    "arguments":"{...}"}}
 *   {"type":"assistant/message","seq":20,"time":<epoch_ms>,"data":{"message":{...},"usage":{...}}}
 *
 * None of the CLI's six detection paths match (types use "/" not ".", there is no
 * top-level role/source/content, and the clock field is `time`, not `timestamp`).
 * Result: usable_events = 0 and the CLI refuses the trace.
 *
 * This is a faithful, mechanical conversion of the real log — the same thing the
 * official trace-record "post-hoc convert" does for other harnesses. It never
 * invents steps: every emitted step corresponds to a real logged event.
 *
 * MAPPING
 * -------
 *   assistant/message .reasoning block  -> thought
 *   assistant/message .text block       -> thought   (assistant narration/plan)
 *   assistant/message .tool-call block  -> (not emitted; the tool/call row carries it)
 *   tool/call                           -> tool_call   (tool_call_id = data.callId)
 *   tool/result                         -> tool_result (tool_call_id = data.toolCallId)
 *   tool/result with isError            -> tool_result + error
 *   user/message                        -> observation
 *   system/message, session/*, request/*, step/*, job/*, turn/*  -> skipped
 *                                        (prompt bodies, bookkeeping, no agent action)
 *
 * A tool_call with no matching tool_result (log truncated mid-call) is DROPPED,
 * because the CLI rejects unpaired tool_calls. The count is reported on stderr.
 *
 * COST: token usage comes from assistant/message.data.usage. cost_usd is computed
 * with the same price table and fallback the CLI itself uses (tracePrice), so the
 * number we report matches the CLI's own yardstick.
 *
 * USAGE
 *   node tools/dsh2trace.mjs --in <session.v4.jsonl.zstd|session.jsonl> --out <trace.jsonl>
 *   node tools/dsh2trace.mjs --session session-ae66dc95-... --out trace.jsonl
 *   node tools/dsh2trace.mjs --latest --cwd "D:\\AI-for-science" --out trace.jsonl
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import zlib from "node:zlib";

const ZSTD_MAGIC = 0xfd2fb528;

// Same table + fallback as playground-cli dist/index.js tracePrice().
const PRICE_IN = {
  "deepseek-v4-pro": 0.27, "deepseek-v3": 0.14,
  "claude-opus-4-7": 15.0, "claude-sonnet-4-6": 3.0, "claude-opus-4-5": 15.0,
  "claude-sonnet-4-5": 3.0, "gpt-4o": 2.5, "gpt-4o-mini": 0.15, "gpt-5": 5.0,
};
const PRICE_OUT = {
  "deepseek-v4-pro": 1.10, "deepseek-v3": 0.28,
  "claude-opus-4-7": 75.0, "claude-sonnet-4-6": 15.0, "claude-opus-4-5": 75.0,
  "claude-sonnet-4-5": 15.0, "gpt-4o": 10.0, "gpt-4o-mini": 0.60, "gpt-5": 15.0,
};
function tracePrice(model, kind) {
  const lower = String(model || "").toLowerCase();
  const table = kind === "in" ? PRICE_IN : PRICE_OUT;
  for (const [name, price] of Object.entries(table)) if (lower.includes(name)) return price;
  return kind === "in" ? 0.5 : 1.5;
}
function estimateCostUsd(model, tokensIn, tokensOut) {
  return Number(((tokensIn * tracePrice(model, "in") + tokensOut * tracePrice(model, "out")) / 1e6).toFixed(6));
}

const BODY_LIMIT = 8000;

function arg(argv, name, fallback) {
  const i = argv.indexOf(`--${name}`);
  if (i === -1) return fallback;
  const v = argv[i + 1];
  return v === undefined || v.startsWith("--") ? true : v;
}
function flag(argv, name) {
  return argv.includes(`--${name}`);
}

function readRows(file) {
  const raw = fs.readFileSync(file);
  const isZstd = raw.length >= 4 && raw.readUInt32LE(0) === ZSTD_MAGIC;
  let text;
  if (isZstd) {
    if (typeof zlib.zstdDecompressSync !== "function") {
      throw new Error("this Node build has no zlib.zstdDecompressSync; need Node >= 22.15");
    }
    // zstdDecompressSync wants the whole frame including the magic number.
    text = zlib.zstdDecompressSync(raw).toString("utf8");
  } else {
    text = raw.toString("utf8");
  }
  const rows = [];
  let skipped = 0;
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    try {
      rows.push(JSON.parse(t));
    } catch {
      skipped += 1;
    }
  }
  return { rows, isZstd, skipped };
}

/** Find the session log for a session id, or the newest one, under ~/.dsh/sessions. */
function resolveSessionLog({ sessionId, cwd, latest }) {
  const roots = [];
  const base = path.join(os.homedir(), ".dsh", "sessions");
  roots.push(base);
  const found = [];
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    for (const wsDir of fs.readdirSync(root, { withFileTypes: true })) {
      if (!wsDir.isDirectory()) continue;
      const wsPath = path.join(root, wsDir.name);
      for (const sDir of fs.readdirSync(wsPath, { withFileTypes: true })) {
        if (!sDir.isDirectory()) continue;
        if (sessionId && !sDir.name.includes(sessionId)) continue;
        const dir = path.join(wsPath, sDir.name);
        for (const f of fs.readdirSync(dir)) {
          if (!/^session.*\.jsonl(\.zstd)?$/.test(f)) continue;
          const full = path.join(dir, f);
          found.push({ file: full, dir, sessionId: sDir.name, ws: wsDir.name, mtime: fs.statSync(full).mtimeMs });
        }
      }
    }
  }
  if (!found.length) return null;
  if (!latest && sessionId) {
    const exact = found.filter((x) => x.sessionId.includes(sessionId)).sort((a, b) => b.mtime - a.mtime);
    if (exact.length) return exact[0];
  }
  if (cwd) {
    // DSH encodes the workspace path in the directory name, e.g. --D-AI-for-science--
    const enc = cwd.replace(/[\\/]/g, "-").replace(/^([A-Za-z]):/, "$1").replace(/-/g, "-");
    const want = `--${cwd.replace(/\\/g, "-").replace(/:/g, "")}--`.replace(/--+/g, "--");
    const hits = found.filter((x) => x.ws.toLowerCase().includes("ai-for-science") || x.ws.includes(enc));
    const pool = hits.length ? hits : found;
    return pool.sort((a, b) => b.mtime - a.mtime)[0];
  }
  return found.sort((a, b) => b.mtime - a.mtime)[0];
}

function isoNoMillis(millis) {
  return new Date(millis).toISOString().replace(/\.\d{3}Z$/, "Z");
}
/**
 * Mask personal data before it leaves the machine. The trace is uploaded, so a
 * phone number or email captured in a tool result would travel with it. This only
 * masks identifiers; it never changes the technical content of a step.
 * Counted and reported so the redaction is auditable rather than silent.
 */
const PII_PATTERNS = [
  [/1[3-9]\d{9}/g, "<PHONE_REDACTED>"],
  [/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "<EMAIL_REDACTED>"],
];
const redactionCounts = { phone: 0, email: 0, secret: 0 };

/**
 * Known-secret vault. A credential pasted into the chat ends up in the session
 * log, and the session log is uploaded as the trace. Regexes cannot recognise an
 * arbitrary password, so every literal value in `.secrets/*.txt` is redacted by
 * exact match. Only values of length >= 6 are used, to avoid shredding ordinary
 * words.
 */
const VAULT_DIR = "D:/AI-for-science/.secrets";
function loadVault() {
  const values = [];
  try {
    for (const name of fs.readdirSync(VAULT_DIR)) {
      if (!/\.txt$/i.test(name) || name.startsWith("_")) continue;
      try {
        const v = fs.readFileSync(path.join(VAULT_DIR, name), "utf8").trim();
        if (v.length >= 6) values.push(v);
      } catch {
        /* unreadable single file must not stop the conversion */
      }
    }
  } catch {
    /* no vault: PII regexes still apply */
  }
  // longest first so a token containing another secret is not partially masked
  return values.sort((a, b) => b.length - a.length);
}
const VAULT = loadVault();

function redactPii(text) {
  let s = String(text ?? "");
  for (const secret of VAULT) {
    if (!s.includes(secret)) continue;
    const parts = s.split(secret);
    redactionCounts.secret += parts.length - 1;
    s = parts.join("<SECRET_REDACTED>");
  }
  s = s.replace(PII_PATTERNS[0][0], () => {
    redactionCounts.phone += 1;
    return PII_PATTERNS[0][1];
  });
  s = s.replace(PII_PATTERNS[1][0], () => {
    redactionCounts.email += 1;
    return PII_PATTERNS[1][1];
  });
  return s;
}
function clip(text) {
  const s = redactPii(text);
  return s.length > BODY_LIMIT ? `${s.slice(0, BODY_LIMIT)}\n…[truncated ${s.length - BODY_LIMIT} chars]` : s;
}
function blockText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((b) => b && (b.type === "text" || b.type === "reasoning"))
    .map((b) => String(b.text ?? ""))
    .filter(Boolean)
    .join("\n");
}

function convert(rows, { model }) {
  // Order by seq when present; the file is append-ordered, seq is the stable key.
  const ordered = [...rows].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));

  const steps = [];
  const calls = new Map(); // callId -> index in steps
  const resultSeen = new Set();
  const notes = { unpaired: 0, errorResults: 0 };

  const push = (stepType, body, millis, extra = {}) => {
    const step = {
      step_type: stepType,
      type: stepType,
      step_id: `dsh-${String(extra.seq ?? steps.length).padStart(5, "0")}-${String(steps.length).padStart(5, "0")}`,
      step_order: steps.length + 1,
      timestamp: isoNoMillis(millis),
      body: clip(body),
      ...extra,
    };
    delete step.seq;
    steps.push(step);
    return step;
  };

  for (const row of ordered) {
    const type = String(row.type || "");
    const millis = Number(row.time) || Date.parse(String(row.timestamp || "")) || Date.now();
    const d = row.data || {};

    if (type === "assistant/message") {
      const msg = d.message || {};
      const usage = d.usage || {};
      const tokensIn = Number(usage.inputTokens ?? usage.prompt_tokens ?? 0);
      const tokensOut = Number(usage.outputTokens ?? usage.completion_tokens ?? 0);
      const usedModel = String(d.model || msg.model || model || "deepseek-flash");
      const cost = tokensIn || tokensOut ? estimateCostUsd(usedModel, tokensIn, tokensOut) : 0;
      let firstOfMessage = true;
      for (const block of Array.isArray(msg.content) ? msg.content : []) {
        if (!block) continue;
        if (block.type === "reasoning" && block.text) {
          push("thought", block.text, millis, { model_id: usedModel });
        } else if (block.type === "text" && block.text) {
          push("thought", block.text, millis, { model_id: usedModel });
        } else {
          continue;
        }
        // Resource signals ride on the first step of the message so totals stay exact.
        if (firstOfMessage) {
          const last = steps[steps.length - 1];
          last.tokens_in = tokensIn;
          last.tokens_out = tokensOut;
          last.cost_usd = cost;
          last.model_id = usedModel;
          firstOfMessage = false;
        }
      }
      continue;
    }

    if (type === "tool/call") {
      const callId = String(d.callId || d.toolCallId || "");
      const name = String(d.name || "tool");
      const step = push("tool_call", `${name} ${d.arguments ?? ""}`, millis, {
        tool_call_id: callId,
        tool_name: name,
        seq: row.seq,
      });
      if (callId) calls.set(callId, step);
      continue;
    }

    if (type === "tool/result") {
      const callId = String(d.toolCallId || (d.message && d.message.toolCallId) || "");
      const msg = d.message || {};
      const isError = Boolean(msg.isError);
      const body = blockText(msg.content) || (msg.content ? JSON.stringify(msg.content) : "");
      const step = push("tool_result", body || "(empty result)", millis, {
        tool_call_id: callId,
        seq: row.seq,
      });
      if (callId) {
        resultSeen.add(callId);
        const call = calls.get(callId);
        if (isError) {
          notes.errorResults += 1;
          step.is_error = true;
          // Link the error back to the call that caused it, like trace-record does.
          const errStep = push("error", body || "(tool error)", millis, { seq: row.seq });
          errStep.responds_to = step.step_id;
        }
        if (call && !call.tool_result_id) call.tool_result_id = step.step_id;
      }
      continue;
    }

    if (type === "user/message") {
      const msg = d.message || {};
      const body = blockText(msg.content) || (typeof d.text === "string" ? d.text : "");
      if (body) push("observation", `user: ${body}`, millis, { seq: row.seq });
      continue;
    }

    // Everything else (session, request/*, step/*, turn/*, job/*, system/message,
    // permission/preset, sandbox/mode, approval/policy, model/selection, session/title*)
    // is bookkeeping or prompt payload, not an agent action.
  }

  // Drop tool_calls whose result never landed: the CLI rejects unpaired calls.
  const keep = steps.filter((s) => {
    if (s.step_type !== "tool_call") return true;
    const id = s.tool_call_id;
    if (!id) return false;
    if (resultSeen.has(id)) return true;
    notes.unpaired += 1;
    return false;
  });
  keep.forEach((s, i) => {
    s.step_order = i + 1;
  });
  return { steps: keep, notes };
}

function main() {
  const argv = process.argv.slice(2);
  if (flag(argv, "help") || flag(argv, "-h")) {
    console.log("dsh2trace --in <log> --out <trace.jsonl> | --session <id> | --latest [--cwd <dir>]");
    return;
  }
  let input = arg(argv, "in");
  const out = path.resolve(String(arg(argv, "out", "trace.jsonl")));
  if (!input) {
    const sessionId = arg(argv, "session");
    const found = resolveSessionLog({
      sessionId: sessionId === true ? undefined : sessionId,
      cwd: arg(argv, "cwd") === true ? undefined : arg(argv, "cwd"),
      latest: flag(argv, "latest"),
    });
    if (!found) {
      console.error("dsh2trace: could not locate a DSH session log; pass --in explicitly");
      process.exit(2);
    }
    input = found.file;
    console.error(`dsh2trace: using ${found.file}`);
  }
  const file = path.resolve(String(input));
  if (!fs.existsSync(file)) {
    console.error(`dsh2trace: no such file: ${file}`);
    process.exit(2);
  }

  const { rows, isZstd, skipped } = readRows(file);
  const model = String(arg(argv, "model", "deepseek-flash"));
  const { steps, notes } = convert(rows, { model });

  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, steps.map((s) => JSON.stringify(s)).join("\n") + "\n", "utf8");

  const byType = {};
  let totalCost = 0;
  let totalTok = 0;
  let longThoughts = 0;
  for (const s of steps) {
    byType[s.step_type] = (byType[s.step_type] || 0) + 1;
    totalCost += Number(s.cost_usd) || 0;
    totalTok += (Number(s.tokens_in) || 0) + (Number(s.tokens_out) || 0);
    if (s.step_type === "thought" && String(s.body || "").length >= 80) longThoughts += 1;
  }
  console.log(JSON.stringify({
    input: file,
    input_kind: isZstd ? "zstd-jsonl" : "jsonl",
    rows_read: rows.length,
    rows_unparsable: skipped,
    out,
    steps: steps.length,
    by_type: byType,
    thoughts_over_80_chars: longThoughts,
    total_cost_usd: Number(totalCost.toFixed(6)),
    total_tokens: totalTok,
    unpaired_tool_calls_dropped: notes.unpaired,
    error_results: notes.errorResults,
    pii_redactions: redactionCounts,
  }, null, 2));
}

main();
