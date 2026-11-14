#!/usr/bin/env node
// xdkvm — dieu khien phien terminal root tren W105D KVM (ban sua tu CLI cua S-remote, cung API /api/sessions).
// Single file, no deps, Node 18+ (global fetch). Tai tu W105D:
//   curl -o xdkvm.js http://<w105d>:8090/cli/xdkvm.js
// Can token: $XDKVM_TOKEN hoac --token (token doi moi lan W105D cam nguon).
"use strict";

const HELP = `xdkvm — control W105D terminal sessions over REST

usage: node xdkvm.js [--host H[:port]] [--token T] <command> [args]
  host: --host or $XDKVM (default 127.0.0.1:8090) · token: --token or $XDKVM_TOKEN
  <s> = session id, exact name, or unique id prefix
  put "--" before text/commands that contain --flags of their own

  ls                                   list sessions
  new [--cwd DIR] [--name N] [--user root|sang] [--cols 200 --rows 50]
                                       create a bash session (root by default), prints its id
  send <s> <text...> [--no-enter]      type text (+ Enter); "-" reads stdin
  key <s> <key...>                     enter tab esc up down left right bs ctrl-c ctrl-d ...
  run <s> <command...> [--timeout 600] run a shell command, wait, print its output,
                                       exit with its exit code (124 on timeout)
  read <s> [--since N] [--tail N]      print buffered output; next offset → stderr
  wait <s> [--since N] [--idle 2] [--for REGEX] [--timeout 600]
                                       wait until output goes idle / matches, print it
  tail <s> [-f]                        print recent output; -f follows
  kill <s>                             kill the session`;

// ---------- args ----------
const argv = process.argv.slice(2);
const flags = {}, pos = [];
const BOOL = new Set(["no-enter", "f", "help", "h"]);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === "--") { pos.push(...argv.slice(i + 1)); break; }   // rest is literal: run s -- git log --oneline
  if (pos.length >= 2 && ["send", "run"].includes(pos[0]) && !a.startsWith("--")) { pos.push(a); continue; }
  const m = /^--?([\w-]+)(?:=(.*))?$/.exec(a);
  if (m && a !== "-") {
    if (m[2] !== undefined) flags[m[1]] = m[2];
    else if (BOOL.has(m[1])) flags[m[1]] = true;
    else flags[m[1]] = argv[++i];
  } else pos.push(a);
}
let host = flags.host || process.env.XDKVM || "127.0.0.1:8090";
if (!/^https?:\/\//.test(host)) host = "http://" + host;
if (!/:\d+$/.test(host.replace(/^https?:\/\//, "")) && !/^https:/.test(host)) host += ":8090";
const token = flags.token || process.env.XDKVM_TOKEN || "";

// never process.exit(): right after fetch it trips a libuv assert on Windows
// (UV_HANDLE_CLOSING) and the exit code is lost — set exitCode and unwind instead
class Exit { constructor(code) { this.code = code; } }
const quit = (code) => { throw new Exit(code); };
const die = (msg, code = 1) => { process.stderr.write(msg + "\n"); quit(code); };

async function api(method, p, body) {
  let r;
  try {
    r = await fetch(host + p, {
      method, headers: Object.assign(body ? { "Content-Type": "application/json" } : {}, token ? { Authorization: "Bearer " + token } : {}),
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) { die(`cannot reach ${host}: ${e.cause ? e.cause.code || e.cause.message : e.message}`); }
  const t = await r.text();
  let j = null; try { j = t ? JSON.parse(t) : null; } catch {}
  if (r.status === 401) die(`${method} ${p}: HTTP 401 — cần token (export XDKVM_TOKEN=...)`);
  if (!r.ok) die(`${method} ${p}: HTTP ${r.status} ${(j && j.error) || t}`);
  return j;
}

async function resolve(ref) {
  if (!ref) die("missing session (id, name or id prefix)");
  const list = await api("GET", "/api/sessions");
  const hit = list.find((s) => s.id === ref) || list.find((s) => s.name === ref);
  if (hit) return hit;
  const pre = list.filter((s) => s.id.startsWith(ref));
  if (pre.length === 1) return pre[0];
  die(pre.length ? `ambiguous session "${ref}": ${pre.map((s) => s.id).join(", ")}` : `no session "${ref}"`);
}

const output = (s, q) => api("GET", `/api/sessions/${s.id}/output?${new URLSearchParams(q)}`);
const input = (s, body) => api("POST", `/api/sessions/${s.id}/input`, body);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const readStdin = async () => { let d = ""; for await (const c of process.stdin) d += c; return d; };

// long-poll from `since` until done(text) says stop, timeout, or the session exits
async function follow(s, since, { timeoutMs, done, idleMs }) {
  const t0 = Date.now();
  let text = "", cur = since;
  for (;;) {
    const left = timeoutMs - (Date.now() - t0);
    if (left <= 0) return { text, next: cur, timedOut: true };
    const r = await output(s, { since: cur, wait: Math.min(idleMs || 25000, left) });
    if (r.truncated) process.stderr.write(`[xdkvm] output before offset ${r.base} was dropped (buffer cap)\n`);
    text += r.data; cur = r.next;
    if (done && done(text)) return { text, next: cur };
    if (r.exited) return { text, next: cur, exited: true };
    if (idleMs && !r.data) return { text, next: cur, idle: true };
  }
}

const KEYS = {
  enter: "\r", tab: "\t", esc: "\x1b", bs: "\x7f", backspace: "\x7f", space: " ",
  up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D",
  home: "\x1b[H", end: "\x1b[F", pgup: "\x1b[5~", pgdn: "\x1b[6~", del: "\x1b[3~",
};
function keyData(k) {
  const n = k.toLowerCase();
  if (KEYS[n]) return KEYS[n];
  const c = /^ctrl-([a-z@\[\\\]^_])$/.exec(n);
  if (c) return String.fromCharCode(c[1].toUpperCase().charCodeAt(0) & 0x1f);
  die(`unknown key "${k}"`);
}

// shell-specific "print a done marker with the exit code". The typed form never
// contains the printed form (split string / %^var%), so the echo can't match.
function wrapCommand(shell, cmd, nonce) {
  const mk = `DONE_${nonce}:`;
  if (/^(powershell|pwsh)/.test(shell))
    return `$global:LASTEXITCODE=0; ${cmd}; $__sr=$?; Write-Host ('__SR'+'${mk}'+$(if($__sr){0}elseif($LASTEXITCODE){$LASTEXITCODE}else{1}))`;
  if (shell === "cmd") return `${cmd} & call echo __SR${mk}%^errorlevel%`;
  return `${cmd}; echo "__SR""${mk}$?"`;
}

async function main() {
  const [cmd, ref, ...rest] = pos;
  if (!cmd || flags.help || flags.h) return console.log(HELP);
  switch (cmd) {
    case "ls": {
      const list = await api("GET", "/api/sessions");
      if (!list.length) return console.log("(no sessions)");
      for (const s of list)
        console.log([s.id, s.exited ? `exited(${s.code})` : "live", s.user, s.by, JSON.stringify(s.name), s.cwd].join("\t"));
      return;
    }
    case "new": {
      const body = { cwd: flags.cwd, name: flags.name, user: flags.user, cols: +flags.cols || 200, rows: +flags.rows || 50 };
      const r = await api("POST", "/api/sessions", body);   // PAGER/GIT_PAGER=cat da dat san trong moi truong phien
      return console.log(r.id);
    }
    case "send": {
      const s = await resolve(ref);
      let text = rest.join(" ");
      if (text === "-") text = await readStdin();
      await input(s, flags["no-enter"] ? { data: text } : { line: text });
      return;
    }
    case "key": {
      const s = await resolve(ref);
      if (!rest.length) die("missing key");
      await input(s, { data: rest.map(keyData).join("") });
      return;
    }
    case "run": {
      const s = await resolve(ref);
      let command = rest.join(" ");
      if (command === "-") command = (await readStdin()).trim();
      if (!command) die("missing command");
      const nonce = Math.random().toString(36).slice(2, 8);
      const re = new RegExp(`__SRDONE_${nonce}:(\\d+)`);
      const { offset } = await input(s, { line: wrapCommand(s.shell, command, nonce) });
      const r = await follow(s, offset, { timeoutMs: (+flags.timeout || 600) * 1000, done: (t) => re.test(t) });
      const m = re.exec(r.text);
      let out = m ? r.text.slice(0, m.index) : r.text;
      // drop the echoed command line (it may wrap); first hit only — PowerShell
      // errors quote the command line again further down
      const echo = out.indexOf(nonce);
      if (echo >= 0) { const nl = out.indexOf("\n", echo); out = nl >= 0 ? out.slice(nl + 1) : ""; }
      out = out.replace(/\s+$/, "");
      if (out) process.stdout.write(out + "\n");
      if (m) quit(+m[1]);
      if (r.exited) die("[xdkvm] session exited", 1);
      die(`[xdkvm] timed out — command may still be running (xdkvm read ${s.id} --since ${r.next})`, 124);
    }
    case "read": {
      const s = await resolve(ref);
      const q = {};
      if (flags.since !== undefined) q.since = flags.since;
      if (flags.tail !== undefined) q.tail = flags.tail;
      const r = await output(s, q);
      process.stdout.write(r.data);
      process.stderr.write(`\n[xdkvm] next=${r.next}${r.exited ? " exited" : ""}\n`);
      return;
    }
    case "wait": {
      const s = await resolve(ref);
      const since = flags.since !== undefined ? +flags.since : (await output(s, { tail: 0 })).next;
      const re = flags.for ? new RegExp(flags.for, "m") : null;
      const r = await follow(s, since, {
        timeoutMs: (+flags.timeout || 600) * 1000,
        idleMs: re ? 0 : (+flags.idle || 2) * 1000,
        done: re ? (t) => re.test(t) : null,
      });
      process.stdout.write(r.text);
      process.stderr.write(`\n[xdkvm] next=${r.next}${r.timedOut ? " timeout" : ""}${r.exited ? " exited" : ""}\n`);
      return quit(r.timedOut ? 124 : 0);
    }
    case "tail": {
      const s = await resolve(ref);
      const r = await output(s, { tail: +flags.n || 4000 });
      process.stdout.write(r.data);
      if (!flags.f) return;
      let cur = r.next;
      for (;;) {
        const n = await output(s, { since: cur, wait: 25000 });
        process.stdout.write(n.data); cur = n.next;
        if (n.exited) return;
        if (!n.data) await sleep(50);
      }
    }
    case "kill": {
      const s = await resolve(ref);
      await api("DELETE", `/api/sessions/${s.id}`);
      return;
    }
    default: die(`unknown command "${cmd}"\n\n${HELP}`);
  }
}
main().catch((e) => {
  if (e instanceof Exit) process.exitCode = e.code;
  else { process.stderr.write(`[xdkvm] ${e.stack || e}\n`); process.exitCode = 1; }
});
