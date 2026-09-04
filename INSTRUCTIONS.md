# Personal Voice & Chat Assistant — Development Instructions

**Revision 3 — 2026-09-04.** Supersedes r1 and r2.
Changes marked **[r2]** (quota/security pass) and **[r3]** (personal-assistant reframe).
Gap analysis behind r2: `PLAN-REVIEW.md`.

Read this whole file before writing code. Build in the phase order given. Do not skip
ahead, and do not build phases that are marked "not yet."

---

## 1. What we're building

**[r3] This is a personal assistant, not a commercial product.** r1 and r2 were written
as a customer-facing business tool. It is not one. That reframe relaxes the licensing
constraints throughout §4 and softens §13, but it does **not** relax the security rules —
visitor mode is still a public endpoint on the open internet.

A web-based assistant that talks over text or voice, and turns each conversation into an
actionable item in a dashboard the owner reads.

### **[r3] Two modes, one pipeline

| Mode | Who | Auth | System prompt |
|---|---|---|---|
| `owner` | You | Supabase JWT required | Jarvis — your assistant |
| `visitor` | Anyone reaching you | None (public) | Secretary — screens on your behalf |

Both run through the same STT → LLM → TTS pipeline, the same persistence, and the same
summarizer. **They differ only in the system prompt, the auth requirement, and the rate
limits.** Do not fork the pipeline; branch on mode at the edges only.

Three surfaces, one static bundle:

- **Visitor widget** — public, unauthenticated. Takes messages for you.
- **Owner widget** — behind auth. You talk to it; it takes notes, tasks and reminders.
- **Dashboard** — behind auth. Everything both modes produced, in realtime.

The transcript is persisted turn by turn. At session end a single LLM call turns it into a
structured item.

### **[r3] What the assistant does — locked

**Owner mode (Jarvis).** Take notes, capture tasks and reminders, record things you want
to remember, answer from what's in the conversation. It is a capture surface with a
personality, not a general chatbot and not a search engine.

**Visitor mode (secretary).** Find out who is calling and what they want. Capture contact
details. Take a preferred date/time as **free text** — there is no calendar integration
and it must never say a booking is confirmed. Then take a message and route it to you.

**Explicitly out of scope in both modes:** answering questions from a knowledge base. That
implies RAG, content we do not have, and a retrieval budget this stack cannot afford. If
asked something it cannot answer, it takes a message. Do not add retrieval without a
decision to reopen this.

---

## 2. Hardware, hosting, and the real constraint

| Piece | Where | Notes |
|---|---|---|
| Frontend | GitHub Pages | Static only. No server-side rendering, no secrets. |
| Backend | Contabo VPS | 4 vCPU, 8 GB RAM, 100 GB disk. **No GPU.** |
| Database | Supabase free tier | Postgres + Auth + Realtime |
| **[r3]** Backend DNS | DuckDNS | `assistant-ai.duckdns.org` → `109.199.116.38` |
| **[r4]** Reverse proxy | **nginx** (not Caddy) | Already on the box. See below. |

### **[r4] The VPS is not empty — nginx replaces Caddy

r1–r3 all specified Caddy. **That is wrong for this box and has been changed.** The VPS
already runs:

- **nginx** on `:80` and `:443`, serving `gardening-ai.duckdns.org`
- **`garden-ai`**, a Node app under pm2 on `:3000`, with its own certbot cert
- fail2ban

Installing Caddy would contend for 80/443, and stopping nginx would take down a live app.
So the assistant follows the pattern already working here: **a second nginx server block,
proxying to FastAPI on `127.0.0.1:8000`, with certbot for TLS.**

**Coexistence rules — this box hosts something you care about:**

- **Never stop or replace nginx.** Reload, never restart, and only after `nginx -t` passes.
- **Back up `/etc/nginx` before touching it.** Phase 0 left a tarball in `/root/`.
- After any nginx change, verify `gardening-ai.duckdns.org` still serves. Note its `/`
  returns 404 by design — the Node app only answers specific routes, `/health` returns 200.
  Test `/health`, not `/`.
- Port 8000 is ours. 3000 is garden-ai's. Do not collide.

**[r4] nginx does not proxy WebSockets by default.** This is the trap the Caddy assumption
hid, and it would have surfaced as an unexplained 400 in Phase 1. Required, and now in
`infra/nginx/`:

- `proxy_set_header Upgrade $http_upgrade;` and `Connection $connection_upgrade;`
- a `map $http_upgrade $connection_upgrade` block in the `http{}` context — hence a
  separate `conf.d/websocket_upgrade.conf`, since a `map` cannot live in a server block
- `proxy_read_timeout 3600s` — the default 60s silently kills an idle voice session during
  any natural pause
- `proxy_buffering off` — otherwise nginx accumulates the response and destroys the
  first-token latency that all of §7 exists to protect

### DNS and TLS

The VPS has a **static IP**, so dynamic DNS is not needed — only a name. `duckdns.org` is
on the Public Suffix List, so the subdomain gets its own LE rate-limit bucket rather than
sharing one with every other DuckDNS user.

- **[r4] DuckDNS's API can only update domains, never create them.** A new subdomain must
  be added in the web UI first; the API returns `KO` for one that does not exist.
- **[r4] A cron refreshes the record every 6 hours** (`infra/duckdns-refresh.sh`).
  DuckDNS reclaims records that go unupdated.
- **Run `certbot --dry-run` before requesting a real cert.** Production rate limits will
  lock out HTTPS for a week if thrashed.
- DuckDNS is free with no SLA. If it is down, the backend is unreachable. Acceptable here;
  know that it is the weakest link in the hosting chain.
- Frontend (`<user>.github.io`) and backend (`assistant-ai.duckdns.org`) are
  **cross-origin**. Both exact origins go in the allowlist config. No wildcards.

### **[r2] Quota is the primary constraint. CPU is secondary.

r1 said the weak VPS drove every design decision. Correct for a paid API budget. Under
**free tier only, no subscriptions**, the VPS will serve more conversations than the free
quotas allow. Quota runs out first.

Both are real. Order them correctly: **API quota** sets volume and concurrency (§6);
**CPU** sets TTS strategy and forbids local STT.

Contabo vCPUs are shared and oversubscribed — 4 of them run at roughly 1.5–2 real cores,
degrading under sustained load. So:

- STT and the LLM are **offloaded to APIs**. Do not run Whisper locally.
- TTS must be an API call or a genuinely lightweight local model.
- Do not run Kokoro TTS. It sounds best but is far too slow on these cores.
- **[r3]** Voice cloning is off the table for the same reason — see §4 on the Jarvis voice.
- Do not add CPU-heavy work to the request path without benchmarking first.

---

## 3. Non-negotiables

1. **No provider API keys in the frontend, ever.** All provider keys live in the VPS
   environment. The browser talks only to our backend.
   **[r2] One carve-out:** the frontend may embed the Supabase **URL and anon key**. That
   key is public by design and protected by RLS. Valid *only* while RLS is enabled on
   every table (§8). Never the service role key. Do not widen this.
2. **No large model downloads in the browser.** **[r2]** Measure *total* transfer, not the
   model file — see §5 on VAD.
3. **The database is the source of truth, not the client.** Write turns as they happen.
   Never build an item from a transcript the client hands you at the end.
4. **Secrets never appear in code, logs, error responses, or commits.** `.env` only, commit
   `.env.example`.
5. **Origin is locked to the configured origins — on HTTP *and* on the WebSocket.**
   **[r2] CORS does not apply to WebSockets.** Browsers send cross-origin WS handshakes
   with no preflight and no enforcement, so `CORSMiddleware` protects the WS endpoint not
   at all. Read and validate `Origin` manually inside the handshake against an allowlist,
   rejecting on mismatch.
6. **HTTPS/WSS is mandatory.** Browsers block microphone access outside a secure context
   and block plain WebSockets from an HTTPS page.
7. **[r3] Mode is derived from authentication, never from the client.** The client does not
   send a `mode` field that the server trusts. The server determines mode by whether a
   **valid, verified Supabase JWT** was presented on the handshake: valid → `owner`,
   absent → `visitor`. A forged flag must not be able to reach owner mode, owner rate
   limits, or the Jarvis prompt. Verify the JWT against Supabase's JWKS server-side; never
   decode-without-verify.
8. **[r3] Visitor mode is still a public endpoint on the open internet.** Personal use
   does not relax rule 5, per-IP rate limiting, or the concurrency cap. It is
   unauthenticated and it spends finite quota.
9. **Never promise what the assistant cannot do.** No confirmed bookings, no commitments
   on your behalf, no claims about when you will respond. It captures and routes.

---

## 4. Stack

### Backend — Python 3.11+, FastAPI

Single service on the VPS. Holds all keys. Owns the WebSocket. Writes to Supabase with the
service role key.

### Speech-to-text — Groq

Model: `whisper-large-v3-turbo`. Free tier: 20 RPM, 2,000 RPD, 7,200 audio-sec/hour,
28,800 audio-sec/day. **Verified accurate 2026-09-04.**

**[r2] Groq counts a 10-second minimum per transcription request**, free tier included.
Every "yes" costs 10 audio-seconds. This drives the STT budget in §6 and means **do not
send sub-second VAD fragments** — merge very short segments before upload.

Groq's transcription endpoint accepts standard containers, so the browser sends
`webm/opus` straight from `MediaRecorder`. No PCM conversion. Free tier caps uploads at
25 MB; utterance-sized clips never approach it.

### **[r2] LLM — Groq only, laddered across models

**This replaces r1's Gemini → Groq → OpenRouter router.** Rationale, since it is a
deliberate trade:

- **Gemini's free tier trains on your data.** Google's terms for unpaid services: they use
  submitted content to "improve, and develop Google products," and "human reviewers may
  read, annotate, and process your API input and output." Paid tier explicitly does not.
- **OpenRouter's free endpoints require opting into training** — without the "free
  endpoints that may train on inputs" setting they refuse to route, and a separate setting
  governs endpoints that may *publish* prompts.
- **Groq does not train on customer data on any tier**, retains nothing by default, and
  offers Zero Data Retention in Data Controls. **Enable ZDR.**

The ladder runs *within* Groq. Limits are per-organization **and per model**, so **each
model is its own quota bucket and they stack.** Order by quality, fall through on
exhaustion:

| Order | Model | Free limits (verify in console) | Notes |
|---|---|---|---|
| 1 | `openai/gpt-oss-120b` | 30 RPM · 1K RPD · 8K TPM · 200K TPD | Best quality of the free set. |
| 2 | `openai/gpt-oss-20b` | 30 RPM · 1K RPD · 8K TPM · 200K TPD | Separate bucket from 120b. |
| 3 | `qwen/qwen3.8-27b` | 30 RPM · 8K TPM | Separate bucket again. |
| 4 | `groq/compound-mini` | 30 RPM · **70K TPM** | High TPM — for when concurrency, not daily volume, is binding. |

**[r3] The ladder is config-driven, not hardcoded.** A list of model IDs with their limits
in `config.py`. Adding, removing or reordering a rung is a config edit. Since actual volume
is unknown (§6), this is how we adapt without a refactor.

**Verify these in the Groq console before building around them.** Limits are
per-organization and the lineup moves: `llama-3.1-8b-instant`, named as r1's fallback,
moved to Enterprise-only on 2026-08-26.

**`groq/compound` and `compound-mini` are agentic system models** with built-in tool use.
Confirm they behave as plain chat completions before relying on them, and disable built-in
browsing.

**Stream the response.** Non-negotiable for latency — see §7.

**[r2] Accept the single point of failure.** Groq serves both STT and the LLM. If Groq is
down, the assistant is down. That is the cost of the privacy posture, accepted
deliberately. The mitigation is not another provider — it is the no-LLM degraded mode in
§6, which must work.

Keep `llm.py` behind a provider interface even though only Groq is wired up. Adding a
provider must be a new file plus a config line. Do not ship a second provider.

### Text-to-speech

Interface:

```python
class TTSBackend(Protocol):
    async def synthesize(self, text: str, voice: str, prosody: Prosody) -> bytes: ...
```

**Primary: `edge-tts`.** Server-side it is an outbound HTTP call, so it costs the VPS
essentially no CPU.

Document in the code: `edge-tts` is an unofficial wrapper around Microsoft Edge's
read-aloud endpoint. Undocumented rate limits, and a real history of breaking — recurring
403 / `WSServerHandshakeError` failures tied to Microsoft's `Sec-MS-GEC` token, reported
as recently as Jan 2026. **Pin the version. Expect it to break.**

**[r2] Failure handling is a circuit breaker, not a config flag.** N consecutive failures
trips to Piper automatically, with periodic retry to trip back. A manual flag requires a
human to notice at 3am. The flag stays, for forcing a backend during benchmarking.

**Fallback: Piper**, `en_GB-alan-medium` (63 MB ONNX, 22.05 kHz). Built for realtime neural
TTS on weak CPUs. Ships with the app so it can never be switched off.

**[r3] Licensing is no longer blocking.** r2 hardened this for a commercial product. Personal
use involves no distribution, so:

- Piper's move to GPL-3.0 (`OHF-Voice/piper1-gpl`; `rhasspy/piper` archived Oct 2025) does
  not create an obligation here. **Still invoke the binary as a subprocess rather than
  importing it** — it is better process isolation and keeps a commercial pivot open — but
  this is now a preference, not a rule.
- The unresolvable `en_GB-alan-medium` license ("See URL", and the URL 404s) does not block
  anything. Record what was found and move on.
- Non-commercial voices are back on the table. Piper's `en_US-ryan` (CC BY-NC-SA 4.0) is
  usable — though it is US English, so not a fit for the Jarvis brief.

### **[r3] The Jarvis voice — what is and is not achievable

The actual Jarvis is Paul Bettany. Reproducing *that* means voice cloning (XTTS, RVC,
F5-TTS), all of which need a GPU. On 1.5–2 effective shared cores it is not close — the
same reason §2 rules out Kokoro and local Whisper. **Do not attempt cloning on this
hardware.**

What we do instead, in descending order of impact:

1. **The persona prompt does most of the work.** Jarvis is mostly a writing style: dry,
   economical, unflappable, faintly amused, never gushes, consistent address. Free, lives
   in the system prompt, survives any voice change. This is the main lever.
2. **SSML prosody.** edge-tts accepts rate and pitch. Slightly slower and slightly lower
   reads as composed rather than chirpy. Start around `rate=-8%`, `pitch=-4Hz`; both
   configurable, both worth tuning by ear in Phase 2.
3. **Voice selection.** edge-tts has exactly two en-GB male voices: `en-GB-RyanNeural` and
   `en-GB-ThomasNeural`. Ryan is generally judged the less robotic. **Default to Ryan**;
   the config makes swapping trivial.

Realistic expectation: most of the character, none of the actual timbre. Prosody settings
apply to **owner mode only** — visitor mode uses a neutral, professional delivery.

### **[r3] Audio wire format — decide once, before Phase 2

edge-tts returns MP3; Piper returns 22.05 kHz WAV. Two backends must not mean two client
playback paths, and server-side transcoding is exactly the CPU work §2 forbids. Pick one
wire format in Phase 2, make both backends conform, benchmark before deciding. The client
implements exactly one decode path.

### Audio cache — required, not optional

Cache synthesized audio on disk. The assistant repeats greetings, clarifying questions and
closings constantly, so hit rate should be high. This is the main lever that makes
concurrency workable. 100 GB is plenty; LRU eviction at a configurable cap.

**[r2] Key on `sha256(backend + voice + prosody + format + sample_rate + text)`.** Keying on
`voice + text` alone, as r1 specified, serves the previous backend's bytes under the new
backend's assumptions the moment the circuit breaker trips — corrupt audio, hard to debug.
**[r3]** Prosody is in the key because owner and visitor modes render the same text
differently.

**[r2] Pin the pre-rendered fixed phrases (§7.2) against eviction.** Otherwise the greeting
gets evicted under exactly the load that makes it matter.

Log cache hit rate at INFO.

---

## 5. Repo layout

```
/backend
  main.py                 FastAPI app, WS endpoint, origin check, auth, rate limits
  config.py               env loading, model ladder, prosody. No defaults for secrets.
  quota.py                [r2] per-model quota ledger. See §6.
  auth.py                 [r3] Supabase JWT verification against JWKS
  /prompts
    owner.py              [r3] Jarvis persona
    visitor.py            [r3] secretary persona
    summarize.py          [r3] item-extraction prompt
  /providers
    stt.py                Groq Whisper client
    llm.py                Groq model ladder, streaming, behind a provider interface
    tts/
      base.py             TTSBackend protocol
      edge.py             edge-tts + SSML prosody
      piper.py            Piper (subprocess)
      cache.py            disk cache + LRU + pinned phrases
  session.py              per-conversation state machine, mode-aware
  persistence.py          Supabase writes (service role key)
  summarize.py            end-of-session item generation
  degraded.py             [r2] no-LLM capture path. See §6.
/tests
  test_quota.py           [r2] ladder + exhaustion, mocked 429s and clock
  test_auth.py            [r3] mode derivation: forged/expired/absent JWT never yields owner
  test_summarize.py       [r2] schema validation, malformed-response fallback
/frontend
  index.html              visitor widget
  assistant.html          [r3] owner widget (auth-gated)
  dashboard.html          operator view
  /js
    widget.js             shared: mic capture, VAD, WS client, audio playback
    auth.js               [r3] shared Supabase auth for assistant + dashboard
    dashboard.js          Supabase realtime subscription
/infra
  nginx/assistant-ai.conf        [r4] server block (certbot adds TLS)
  nginx/websocket_upgrade.conf   [r4] map for $connection_upgrade, http{} context
  systemd/assistant-ai.service
  duckdns-refresh.sh             [r4] 6-hourly cron, keeps the DNS record alive
  supabase/schema.sql
deploy.sh                        [r4] tar over SSH, pip install, restart, health check
.env.example
README.md
PLAN-REVIEW.md
```

Frontend stays buildless if possible — plain ES modules served by Pages. If a bundler
becomes necessary, use Vite and commit built output to `gh-pages`.

### **[r4] ⚠️ The local repo and the public repo are deliberately different

- **Local `main`** — everything: backend, infra, docs, `deploy.sh`.
- **Remote `Hiuchid/assistant-ai` `main`** — **`index.html` only.**

The repo is public because GitHub Pages requires a paid plan for private repos, and
`deploy.sh` and this document both contain the VPS address. Publishing them hands a map to
a box that is being actively swept for `/web/.env` and phpunit RCE paths, and which still
has root password auth enabled.

**Therefore: never run `git push origin main`.** The two histories are unrelated so git
would currently reject it, but that is an accident, not a safeguard. To update the
published page, upload `frontend/index.html` through the GitHub web UI, or push only that
file to a dedicated branch.

Revisit once the security items in §13 are done — at that point publishing the whole repo
is a reasonable call.

**[r2] VAD asset footprint.** `@ricky0123/vad-web`'s ONNX model is ~1–2 MB, but that is not
the download. It also requires `onnxruntime-web` WASM binaries, `.mjs` bindings and an
audio worklet, all served from our origin — and the runtime dominates. **Measure total
transfer before Phase 3 and record it in the README.** If it breaches §3.2, report the
number and stop rather than shipping a slow widget.

---

## 6. **[r2] Quota budget, concurrency, and degraded mode

### Per-conversation cost

Assumptions — argue with these, they drive everything:

- 10-turn voice conversation, ~15 utterances
- system prompt ~300 tokens; each turn resends the growing transcript
- ~80 tokens per turn of history; ~50 output tokens per reply
- one summarisation call: ~1,000 in / 200 out

**≈ 8,300 LLM tokens, ≈ 16 LLM requests, ≈ 15 STT requests per conversation.**

### **[r3] Volume is unknown — build for the ceiling, not a guess

Actual volume is undetermined. Do not tune for a number nobody has. Instead:

- **Keep the ledger, keep it simple.** It tracks and it refuses. No prediction, no
  smoothing, no adaptive backoff.
- **Ladder is config-driven** (§4). If volume turns out low, drop to two rungs. If high,
  add rungs without touching code.
- **Log actual consumption from day one** — conversations/day, tokens/day, peak concurrent.
  After two weeks of real use the numbers replace these assumptions, and §6 gets rewritten
  against data.

Reference ceiling at 50 conversations/day:

| Resource | Consumed at 50/day | Free limit | Headroom |
|---|---|---|---|
| STT requests | 750 | 2,000 RPD | ✅ |
| STT audio-seconds | ~7,500 (at 10s minimum) | 28,800/day | ✅ |
| LLM tokens | ~415,000 | ~600K+ stacked across the ladder | ✅ tight |
| LLM requests | 800 | 1,000 RPD *per model* | ✅ |

The stacked ladder is what makes 50/day work. A single model caps at ~24/day.

### Concurrency cap — derived, not guessed

Binding constraint is **STT request rate**, not CPU. Groq allows **20 transcriptions/minute
account-wide**; an engaged speaker generates ~9 utterances/minute → **2 concurrent voice
sessions.**

```
MAX_CONCURRENT_VOICE = 2      # derived from Groq 20 RPM STT, not from CPU
MAX_CONCURRENT_TEXT  = 4
```

**[r3] Owner mode is exempt from the cap and reserves one slot.** You are one person and
you should never be turned away by visitor traffic. Visitors get the pre-rendered "please
hold" line when the cap is hit. Re-derive if Groq's limits change. **Then** benchmark CPU
to confirm it is not tighter — it should not be, but measure.

### The quota ledger (`quota.py`)

r1's design was purely reactive: send, get a 429, fall through. That burns a request to
discover exhaustion and adds a round-trip to the turn that finds out. Instead, track
remaining **RPM / RPD / TPM / TPD per model** locally and consult before dispatch:

- Estimate token cost before sending; check the ledger first.
- Reconcile against actual usage from response headers after each call.
- Honour `retry-after`. A 429 despite the ledger means the ledger is wrong — log at
  WARNING, mark the model cold for the window, fall to the next rung.
- Reset windows on wall-clock boundaries matching the provider's, not process start.
- Log remaining quota per model at INFO once a minute.

**[r3] Owner mode gets first claim on remaining quota.** When the ledger is nearly empty,
visitors degrade (below) before you do.

### Transcript windowing

Send the system prompt plus a **sliding window of the last 6 turns**, plus a short running
summary of anything older. Honestly: windowing saves little on a 10-turn conversation —
most cost is in the early turns — but it bounds the tail, and the tail breaks the budget.

### Degraded mode (`degraded.py`) — must work

When every rung is exhausted, the assistant must still capture. **It must not fail the
conversation and it must not drop the person.**

- Switch to a **scripted, deterministic capture flow** — no LLM. Fixed questions,
  pre-rendered audio (already cached, §4): who, contact, what about, preferred time.
- Write turns as normal.
- At session end write an item with `type='other'`, the raw transcript as `summary`, and a
  title marking it for triage. No summarisation call.
- Log at WARNING with the conversation id.

The product degrades to a transcribing voicemail rather than an outage. Given the §4 single
point of failure this path is load-bearing — **test it deliberately, do not let it rot.**

---

## 7. The latency budget

Naive sequential processing gives ~0.4 s (Groq) + ~1.0 s (LLM) + ~1.5 s (TTS) ≈ 3 seconds
before anything is heard. That is a bad conversation.

1. **Stream the LLM and split on sentence boundaries.** Hand the first complete sentence to
   TTS and start streaming audio while the LLM keeps generating.
   **[r2]** Audio must play **in generation order**. Sentence 2 can finish synthesis before
   sentence 1 — a short sentence, or a cache hit racing a cache miss. Use an explicit
   ordered queue; do not play chunks as they complete.
2. **Pre-render fixed phrases at startup.** Greeting, hold lines, fallbacks, closings,
   **[r2]** and the full degraded-mode script. Pin them in the cache.
   **[r3]** Render each in both prosody profiles — owner and visitor.
3. **Client-side VAD.** `@ricky0123/vad-web` (Silero). Endpoint in the browser, send only
   the speech segment. **[r2]** Merge very short segments before upload — Groq's 10-second
   minimum makes fragments disproportionately expensive.
4. **Barge-in.** On VAD speech detection, stop playback, clear the queue, cancel in-flight
   LLM/TTS work for that turn.
   **[r2] Persistence rule:** write the partial agent text with `cancelled = true`. What was
   actually heard is what the item must reflect. Do not discard it, and do not write the
   full untruncated reply.

Log per-stage latency from day one: `stt_ms`, `llm_first_token_ms`, `tts_first_chunk_ms`,
`total_to_first_audio_ms`.

---

## 8. Database schema

Supabase free tier: 500 MB, 2 projects, 50k MAU, 5 GB egress, 200 concurrent realtime
connections. Free projects **pause after 7 days of inactivity** — activity means real
database queries, not dashboard visits — and have **no backups**. Phase 6 handles both.
**Verified accurate 2026-09-04.**

```sql
create table conversations (
  id               uuid primary key default gen_random_uuid(),
  started_at       timestamptz not null default now(),
  ended_at         timestamptz,
  channel          text not null check (channel in ('text','voice')),
  -- [r3] set server-side from verified auth, never from the client. See §3.7.
  mode             text not null check (mode in ('owner','visitor')),
  -- [r3] visitor correlation only (campaign tag / referrer). Never PII,
  -- never person-entered. Null in owner mode.
  visitor_ref      text,
  status           text not null default 'active',
  -- [r2] the inactivity sweeper needs a column to sweep on; deriving
  -- max(turns.ts) per conversation on every sweep does not scale.
  last_activity_at timestamptz not null default now(),
  -- [r3] English at launch; Arabic (Lebanon) likely later. Recording it now
  -- costs nothing and backfilling later is painful.
  lang             text not null default 'en-GB',
  -- [r2] conversation ran without an LLM (§6 degraded mode).
  degraded         boolean not null default false
);
create index on conversations (status, last_activity_at)
  where status = 'active';

create table turns (
  id              bigserial primary key,
  conversation_id uuid not null references conversations(id) on delete cascade,
  role            text not null check (role in ('customer','agent')),
  text            text not null,
  ts              timestamptz not null default now(),
  latency_ms      int,
  -- [r2] barge-in: agent turn was cut off mid-delivery.
  cancelled       boolean not null default false
);
-- [r2] order by the monotonic id, not ts. Same-millisecond writes, clock skew
-- and reconnect races can reorder a transcript keyed on ts, silently
-- corrupting the item.
create index on turns (conversation_id, id);

-- Table name kept from r1 for continuity. It now holds owner-side notes and
-- tasks as well as visitor messages; read "ticket" as "actionable item".
create table tickets (
  id              uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  -- [r3] broadened: owner-side kinds alongside visitor-side ones.
  type            text not null check (type in
                    ('note','task','reminder','message','request','other')),
  title           text not null,
  summary         text not null,
  intent          text,
  action_items    jsonb default '[]'::jsonb,
  urgency         text check (urgency in ('low','medium','high')),
  contact         jsonb default '{}'::jsonb,
  -- [r2] appointment requests are free text. No calendar integration, and the
  -- assistant must never present these as confirmed.
  requested_slot  text,
  -- [r3] denormalised from conversations so the dashboard can filter
  -- "mine" vs "inbound" without a join.
  mode            text not null check (mode in ('owner','visitor')),
  status          text not null default 'new'
                  check (status in ('new','triaged','agent_queued','done')),
  created_at      timestamptz not null default now(),
  -- [r2] §9 asserts idempotency; nothing enforced it. WS-close and the 5-minute
  -- timeout can race. Let the database arbitrate.
  unique (conversation_id)
);
create index on tickets (mode, status, created_at desc);

-- Phase 7 only. Create the table now, leave it unused.
create table agent_runs (
  id          uuid primary key default gen_random_uuid(),
  ticket_id   uuid not null references tickets(id) on delete cascade,
  prompt      text not null,
  status      text not null default 'queued',
  started_at  timestamptz,
  finished_at timestamptz,
  output      text,
  approved_by text
);
```

### RLS

Enable on every table. The backend uses the service role key and bypasses RLS. The visitor
widget never touches Supabase directly.

**[r2] "Authenticated users only" is not sufficient.** Supabase Auth allows public sign-up
by default, so that policy means anyone who registers reads everything. Required:

- **Disable public sign-up** in Supabase Auth settings.
- Add an `operators` allowlist table (or a JWT role claim) and scope every read policy to
  it. **[r3]** The owner widget uses the same identity, so this table gates both surfaces.
- Verify by registering a second account and confirming it reads nothing.

**[r2] Realtime needs explicit setup** — add `tickets` to the realtime publication and set
`REPLICA IDENTITY`. Realtime respects RLS, so confirm the operator policy applies to the
subscription too, not just to direct reads.

Do not store audio in Supabase Storage — the free tier gives 1 GB. Keep audio on the VPS or
discard after transcription.

---

## 9. Item generation

One LLM call per conversation, at session end, reading turns **from the database**, ordered
by `turns.id`. Not per-turn, not from client-supplied text.

**[r3] The extraction prompt branches on mode.** Owner conversations yield
`note` / `task` / `reminder`; visitor conversations yield `message` / `request` / `other`.
Same call, same schema, different instruction and different allowed types.

Request structured JSON matching the columns: type, title, summary, intent, action_items,
urgency, contact, requested_slot. Validate against a Pydantic model before insert; on
validation failure retry once, then write `type='other'` with the raw transcript in the
summary rather than dropping it.

**[r2] Verify Groq's structured-output support** (`response_format`) for the chosen model
before relying on it; fall back to prompt-enforced JSON plus strict parsing if absent.

Trigger on explicit session end, WebSocket close, or a 5-minute inactivity timeout —
whichever fires first. **[r2]** The timeout cannot rely on the WS close handler, since a
hung socket never closes it. Run a periodic sweeper against
`status='active' and last_activity_at < now() - interval '5 minutes'`.

Idempotency is enforced by the unique constraint on `conversation_id` (§8). Handle the
conflict rather than pre-checking — the race is real.

---

## 10. Build phases

Each phase must be working and verified before starting the next.

### Phase 0 — Plumbing **[r2: expanded] — ✅ DONE**

DuckDNS pointed at the VPS. **[r4]** nginx server block + certbot TLS (**dry run first**,
§2). FastAPI with `GET /health`. Static page on GitHub Pages calling it over HTTPS.

**[r4] Completed 2026-09-04:**

- `assistant-ai.duckdns.org` → `109.199.116.38`, refreshed 6-hourly by cron.
- nginx block with WebSocket directives; certbot cert valid to 2026-12-03, auto-renewing.
  garden-ai verified unaffected.
- systemd unit `assistant-ai`, enabled at boot, uvicorn with `--proxy-headers`.
- JSON logging to journald.
- Verified externally: `client_ip` returns the real caller, **not** `127.0.0.1` — so
  per-IP rate limiting in Phase 1 will key correctly.
- Verified `/`-relative fetches of `.env` and source return 404 (the vhost is pure
  `proxy_pass` with no `root`, so nginx never touches the filesystem for it).

- **[r4]** Live at **https://hiuchid.github.io/assistant-ai/** — green, `env=prod`, and
  `your ip` shows the real external address. `ALLOWED_ORIGINS=https://hiuchid.github.io`.
- Verified the Pages origin receives `access-control-allow-origin` and a disallowed origin
  does not. Note a disallowed origin still gets a 200 with a body — CORS is enforced by the
  browser, not the server. **This is exactly why the Phase 1 WebSocket needs its own
  `Origin` check (§3.5); the CORS middleware does not cover it.**

**[r2] Added, because these are ten lines each and painful to retrofit:**

- **A deploy path.** r1 had no phase provisioning the VPS environment, yet Phase 2 requires
  benchmarking on it. Establish it here: systemd unit, venv, documented deploy command.
- **Trusted proxy headers.** Behind Caddy, `request.client.host` is the proxy, so per-IP
  rate limiting collapses into one shared bucket and the first visitor locks out everyone.
  Configure `--proxy-headers` with an explicit trusted-hosts list; verify two different
  client IPs are distinguished in the logs.
- **[r3] A local development story.** You are on Windows 10; Piper, edge-tts and the `.env`
  flow all assume Linux. Document a working local path or containerise.

**Done when:** the Pages site shows green health with no CORS or mixed-content errors, the
service survives a VPS reboot, the access log shows real client IPs, and Caddy has been
switched from staging to production TLS.

### Phase 1 — Visitor text chat, end to end **[r2/r3: revised]**

WebSocket endpoint. Browser sends text, backend calls the Groq ladder with streaming,
tokens stream back and render. No database, no TTS, no auth. **[r3]** Visitor mode only —
owner mode arrives in Phase 4.5, once Supabase Auth exists.

**[r2] Ship the WS origin check in this phase.** It is the only access control on a public,
unauthenticated endpoint that spends finite quota. Verify by connecting from a disallowed
origin and confirming rejection.

**Done when:** a multi-turn text conversation works with tokens arriving incrementally;
forcing a 429 on the top model falls through without dropping the turn; and a WS connection
from an unlisted origin is refused.

### **[r2] Phase 1.5 — Quota ledger and degraded mode

Build `quota.py` and `degraded.py`. Verify the ladder falls through on *predicted*
exhaustion, not just observed 429s. Verify the no-LLM capture flow completes a conversation
and produces a triage item.

Deliberately placed before voice work: cheap, and it determines viability at real volume.
**[r3]** Keep it simple — volume is unknown, so build the mechanism, not a tuned policy.

**Done when:** with all models forced to zero remaining quota, a conversation still
completes, still captures details, and still produces exactly one item.

### Phase 2 — TTS layer

`TTSBackend` with both implementations and the disk cache. Sentence-boundary splitting on
the LLM stream, with the ordered playback queue. The circuit breaker.

**Benchmark both backends on the actual VPS before wiring them in.** Record wall-clock time
and real-time factor for a typical one-sentence reply, in the README. If `edge-tts` is under
~600 ms and Piper under ~1× realtime, proceed. If not, report the numbers and stop — do not
build around a backend that cannot keep up.

**[r3] Tune the Jarvis prosody here, by ear.** Generate the same few lines through
`en-GB-RyanNeural` and `en-GB-ThomasNeural` at a couple of rate/pitch settings; pick and
record the defaults. Decide the single wire format (§4) and record why.

**Done when:** text in, spoken reply out, cache hits logged, sentences always play in order,
and killing edge-tts mid-conversation trips to Piper automatically without dropping the turn.

### Phase 3 — Voice input

`MediaRecorder` capture, `@ricky0123/vad-web` endpointing, webm/opus upload, Groq Whisper,
into the existing pipeline. Barge-in cancellation. Concurrency cap from §6 with the
pre-rendered hold line.

**[r2]** Record the measured total VAD asset transfer in the README before wiring it in.

**Done when:** a full spoken conversation works, interrupting mid-sentence stops playback
immediately, per-stage latency is logged, and a third concurrent voice session gets the hold
message rather than a 429.

### Phase 4 — Persistence and auth **[r2/r3: revised]**

Supabase schema applied. Conversations and turns written as they happen.
**[r3]** Supabase Auth set up here rather than in Phase 6, since both the owner widget and
the dashboard need it. Public sign-up disabled, `operators` allowlist in place, RLS policies
written and verified.

**[r2] Reconnect must be authenticated.** r1 said a dropped socket resumes the same
conversation row, without saying how. A bare `conversation_id` means anyone holding or
guessing one can append to — or read — another conversation. Issue a **signed resume token**
(HMAC over conversation id + expiry) at session start, require it on resume, reject anything
else.

**Done when:** a conversation survives a mid-session refresh with the transcript intact and
correctly ordered; a resume with a forged or expired token is refused; and a second
registered account reads nothing.

### **[r3] Phase 4.5 — Owner mode — NEW

`assistant.html` behind Supabase auth. JWT passed on the WS handshake and verified
server-side against JWKS (`auth.py`). Mode derived from verified auth only (§3.7). The
Jarvis system prompt, owner prosody profile, cap exemption and quota priority from §6.

**Done when:** you can hold a spoken conversation with Jarvis that produces a `note`, `task`
or `reminder`; a request with no JWT lands in visitor mode; and a request with a forged or
expired JWT is refused rather than silently downgraded.

### Phase 5 — Item generation

The summarizer from §9, wired to session end and the inactivity sweeper, branching on mode.

**Done when:** ending a conversation produces exactly one valid item; a deliberately
malformed LLM response still produces a fallback item; firing session-end and the timeout
simultaneously still produces exactly one; and owner and visitor conversations produce
appropriately-typed items.

### Phase 6 — Dashboard and ops

Operator page with the ticket list and a Realtime subscription. **[r3]** Auth already exists
from Phase 4; this phase adds the UI and the mode filter (mine vs inbound). Plus a cron on
the VPS pinging Supabase every 6 hours to prevent the 7-day pause, and a nightly `pg_dump`
to local disk with 7-day retention.

**Done when:** an item created in one browser appears in the dashboard in another without a
refresh, a non-operator account sees nothing through both direct queries and Realtime, and a
dump file exists on disk.

### Phase 7 — Claude CLI worker — **NOT YET**

Create `agent_runs` in Phase 4 and leave it alone. Do not build the worker. When we do, it
will read from `tickets`, never from live conversation data, and every run will require
approval in the dashboard first.

**[r3] The threat model is unchanged and still applies** — arguably more so now that owner
and visitor items share a table. Visitor-authored text is untrusted input heading toward an
agent with shell access. Sandboxed container, no host mounts, no cloud credentials in the
environment, visitor text passed inside a delimited data block and never as instruction,
allowlisted tools, every run logged. **A worker acting on a `mode='visitor'` item must never
inherit owner trust.** Built slowly, reviewed carefully.

---

## 11. Verify, don't assume

**Verified 2026-09-04 — do not re-verify unless building later than ~Dec 2026:**

- Groq Whisper free limits: 20 RPM / 2,000 RPD / 7,200 sec-hr / 28,800 sec-day.
- Groq 10-second minimum per transcription request.
- Groq does not train on customer data on any tier; ZDR available.
- Gemini free tier trains on submitted content, with human review. Confirmed in terms.
- OpenRouter free endpoints require opting into training.
- Supabase free tier: 500 MB, 7-day inactivity pause, no backups.
- Piper is GPL-3.0 under `OHF-Voice/piper1-gpl`; `rhasspy/piper` archived Oct 2025.
- `gemini-2.5-flash-lite` shut down or shutting down; `llama-3.1-8b-instant` moved to
  Enterprise-only 2026-08-26. Both named in r1; both unusable.
- edge-tts en-GB male voices are exactly `RyanNeural` and `ThomasNeural`.
- `duckdns.org` is on the Public Suffix List.
- **[r4]** DuckDNS's HTTP API updates existing domains only; it cannot create them.
- **[r4]** The VPS runs Ubuntu 24.04.4, Python 3.12.3, and already hosts nginx + pm2 +
  garden-ai. `pip`, `espeak-ng` and `ffmpeg` were absent and have been installed.
- **[r4]** pydantic-settings JSON-decodes `list[str]` env values before validators run;
  comma-separated config needs `Annotated[list[str], NoDecode]` or startup fails.

**Still open — check before depending on them, record findings in the README:**

- The four Groq ladder models and their per-model limits, **in our own console** — limits
  are per-organization.
- Whether `groq/compound-mini` behaves as a plain chat completion with tools disabled.
- Groq structured-output (`response_format`) support on the chosen model.
- `@ricky0123/vad-web` **total** asset transfer including onnxruntime-web.
- Whether `edge-tts` still works, at a pinned version, **from the VPS IP** — datacenter
  ranges may be treated differently from residential.
- **[r3]** Whisper's Arabic quality on Lebanese dialect, and whether edge-tts has an
  acceptable `ar-LB` voice, before committing to §13's Arabic plan.

---

## 12. Conventions

- Type hints everywhere. Pydantic for anything crossing a boundary.
- `ruff` and `mypy` clean. Do not add ignores to get green; if a check looks wrong, raise it.
- Errors fail loudly. No bare `except: pass`. Provider failures fall through the ladder;
  everything else propagates with context.
- No silent stubs. Anything unfinished gets a `TODO` and a line in the final summary.
- Structured logging (JSON) with a conversation id on every line. **Never log transcript
  content at INFO.** Debug only, off in production.
- Rate-limit the public WebSocket per IP. It is unauthenticated, on the open internet, and
  spends finite quota. See Phase 0 on proxy headers.
- Commit `.env.example` with every key named and no values.
- Small commits, one concern each. Do not commit or push unless asked.
- **[r2] Tests.** The quota ledger, the summarizer, and **[r3]** mode derivation get real
  unit tests — highest risk, easiest to test, with mocked 429s and a controllable clock.
  Everything else is covered by phase acceptance criteria.

---

## 13. **[r3] Data protection — reduced scope, not zero

r2 treated this as a commercial UK-GDPR problem. It is not — this is personal use, and the
Groq-only decision means no provider trains on any of it.

**Owner mode:** your own data on your own infrastructure. Nothing further required.

**Visitor mode still collects other people's data**, so a smaller version stands:

- **A consent line on the visitor widget** before the microphone is enabled. One sentence.
- **A retention window.** Nothing currently deletes conversations or turns, ever. 500 MB is
  generous at any plausible volume, which means it will be forgotten. Pick a window and add
  a nightly delete job.
- **A deletion path** if someone asks.

Not blocking any phase. Worth doing before the visitor widget is shared with anyone.

---

## 14. **[r3] Arabic (Lebanon) — deferred, not designed out

English at launch. Arabic likely later. The `lang` column exists (§8) so the data model is
ready. Before committing:

- Whisper handles Arabic, but **Lebanese dialect quality needs testing** — it is far from
  MSA and transcription may be poor. Test before promising it.
- edge-tts Arabic voices exist; whether any is acceptable for `ar-LB` needs an ear test.
- Code-switching between Arabic and English mid-sentence is common in Lebanon and is the
  hard case. Whisper's language parameter forces one language — auto-detect per utterance
  is likely needed, at some accuracy cost.
- The persona prompts (§5) would need translating, not just the voice swapping.

Do not build this speculatively. Revisit as a scoped addition once English works end to end.
