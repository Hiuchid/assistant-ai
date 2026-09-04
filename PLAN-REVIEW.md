# Plan Review — Customer Voice & Chat Agent

Review date: 2026-09-04. Reviewer: Claude (Opus 5).
Status: **pre-build**. No code written. Blocking questions at the end.

---

## 0. Headline

The document is well-written and the instincts are right, but it was written against a **paid-tier** cost model ("$0.10/1M in", "$0.0003 per conversation is rounding error"). The operator has since constrained this to **free tier only, no subscriptions**.

That single constraint invalidates the document's central organising principle.

> The plan is organised around **CPU scarcity** (§2: "every design decision below follows from that"). Under free-tier-only, the binding constraint is **quota scarcity**, and it binds roughly an order of magnitude sooner than CPU does.

The VPS can almost certainly serve more conversations than the free LLM tiers will allow. Every "make it fast on weak cores" decision in the doc stays correct, but they are no longer the decisions that determine whether the product works.

**Estimated ceiling of the free-tier stack: ~90–130 voice conversations/day**, and the LLM — not the CPU, not TTS — is what runs out first. Section 3 shows the arithmetic.

---

## 1. Factual drift since the document was written

The doc's §10 asked for exactly this. Verified 2026-09-04.

| Claim in doc | Reality | Impact |
|---|---|---|
| Primary LLM `gemini-2.5-flash-lite` | **Dead or dying.** Google's changelog says the 2.5-flash-lite preview was shut down and directs users to `gemini-3.1-flash-lite`; secondary sources give a full shutdown of Oct 2026. Either way it will not outlive this build. | **Blocking.** Primary model must change. |
| "Step up to `gemini-3.1-flash-lite` if quality is poor" | This is the *current live* model. `gemini-3.5-flash-lite` also GA. | Promote 3.1-flash-lite to primary. The "step up" framing is now backwards. |
| Groq fallback `llama-3.1-8b-instant` | **Moved to Enterprise-only ("contact sales") on 2026-08-26**, dropped from the free and developer rate-limit tables. | **Blocking.** Fallback model ID is dead. `openai/gpt-oss-20b` survives — verify in console. |
| Groq Whisper free limits (20 RPM / 2000 RPD / 7200 sec-hr / 28800 sec-day) | **Confirmed accurate.** | None. Good call. |
| — (not in doc) | Groq bills/counts a **10-second minimum per transcription request**, free tier included. | Significant. VAD produces many short utterances; each "yes" costs 10 audio-seconds. Changes the STT budget materially. |
| — (not in doc) | **Gemini free tier: Google trains on your data.** Terms: on unpaid services "Google uses the content you submit... to provide, improve, and develop Google products", and "human reviewers may read, annotate, and process your API input and output." Paid tier explicitly does not. | **Blocking, and the most important finding in this review.** See §2. |
| — (not in doc) | **Groq does not train on customer data on any tier**, free included; no retention by default; Zero Data Retention available in Data Controls. | Inverts the provider preference order for a customer-data product. |
| Supabase: 500MB, pause after 7 days inactivity, no backups | **Confirmed accurate**, including that "activity" means real DB queries. | None. The Phase 6 cron is correctly specified. |
| OpenRouter free tier as third fallback | **20 RPM but only 50 requests/day** unless $10 of credit has ever been purchased, which raises it to 1,000/day permanently. Free model lineup rotates and endpoints vanish without notice. | At 50/day this tier is decorative — ~3 conversations. See question 1. |
| Piper is MIT | **No longer.** `rhasspy/piper` was archived Oct 2025; active development is `OHF-Voice/piper1-gpl` under **GPL-3.0** (v1.6.0, Jul 2026). | Real. For a commercial product, **invoke the Piper binary as a subprocess — never import it as a library** — to avoid a combined-work argument. Must be written into the code and the README. |
| Piper `en_GB-alan-medium` license "See URL" | **The URL is dead (404).** The mimic3-voices repo is CC BY-SA 4.0 at repo level, which does permit commercial use with attribution + sharealike, but the per-voice pointer the model card gives no longer resolves. | The doc's instruction to resolve this cannot be satisfied from the stated source. Recommend either accepting repo-level CC BY-SA 4.0 with attribution recorded, or picking a voice with an unambiguous license. |
| `@ricky0123/vad-web` "~1–2 MB, verify" | **The ONNX model is ~1–2 MB, but that is not the download.** vad-web also requires `onnxruntime-web` WASM binaries plus `.mjs` bindings and an audio worklet, served from your own origin. The runtime is the dominant cost, not the model. | The "no large downloads" non-negotiable (§3.2) needs a real measured number before Phase 3, not an assumption. |
| `edge-tts` "can break without notice" | Correct, and it has: recurring 403 / `WSServerHandshakeError` issues tied to Microsoft's `Sec-MS-GEC` token, reported as recently as Jan 2026. Server-side use still works; version-sensitive. | The doc's mitigation is a **manual config flag**. That is not sufficient — see §4.8. |

---

## 2. The privacy fork (most important open decision)

This product stores customer names, phone numbers and emails (`tickets.contact jsonb`) and voice transcripts. Under the free-tier-only constraint the two viable LLM providers have opposite data postures:

- **Gemini free tier** — high quota, good quality, **trains on your data and human reviewers may read it**.
- **Groq free tier** — no training on any tier, no retention by default, **but a hard 200,000 tokens/day ceiling** that caps you around 24 conversations/day.

You cannot have high volume, no cost, and data privacy simultaneously. Pick two. This is question 1 and it determines the entire router design, so nothing should be built until it is answered.

Note also: an `en-GB` voice implies UK customers, which implies UK GDPR. Sending customer PII to a tier whose terms permit human review is a decision to make deliberately, not by default. There is currently **no privacy notice, consent line, or retention/deletion policy anywhere in the plan** — see §4.12.

---

## 3. The missing quota budget

The document contains no token arithmetic anywhere. Under free-tier-only this is the central design artefact and its absence is the biggest structural gap.

**Assumptions** (stated so they can be argued with):
- 10-turn voice conversation, ~15 customer utterances
- system prompt ~300 tokens; each turn resends the growing transcript
- ~80 tokens per turn of history; ~50 output tokens per reply
- one ticket-summarisation call: ~1,000 in / 200 out

**Per conversation: ~8,300 LLM tokens, ~16 LLM requests, ~15 STT requests.**

| Provider | Binding limit | Conversations/day |
|---|---|---|
| Groq Whisper (STT) | 2,000 RPD | **~130** |
| Gemini 3.1-flash-lite free | ~1,500 RPD | **~90** |
| Groq `gpt-oss-20b` free | **200,000 TPD** (not RPD) | **~24** |
| OpenRouter free, no credit | 50 RPD | ~3 |
| OpenRouter free, after one-time $10 | 1,000 RPD | ~60 |

Three consequences the plan does not account for:

1. **The concurrency cap is set by STT request rate, not by CPU.** Groq allows 20 transcriptions/minute *across the whole account*. An engaged speaker generates ~8–10 utterances/minute. That is **2, maybe 3 concurrent voice sessions** before 429s start — far below anything CPU would impose. §2 of the doc tells you to derive the cap from benchmarking; derive it from this instead, then check CPU.

2. **Token-per-day binds before requests-per-day on Groq.** Any budget expressed in request counts (as the doc does throughout) will be wrong.

3. **The three-tier router is shallower than it looks.** Gemini → Groq → OpenRouter reads as depth, but tier 3 is 50 requests/day and tier 2 is ~24 conversations. Once Gemini's daily quota is gone, the fallbacks absorb roughly one extra hour of traffic.

**Required additions:**
- A **local quota ledger** — token bucket per provider tracking remaining RPM/RPD/TPM/TPD, consulted *before* dispatch. The doc's design is purely reactive (fall through on 429), which burns a request to discover you are out and adds a full round-trip of latency to the turn that discovers it.
- **Transcript windowing.** Context grows unbounded, so token cost per conversation grows quadratically. Needs a sliding window plus a running summary. Not mentioned anywhere.
- **A defined exhaustion mode.** When every provider is 429, what does the customer hear? Undefined. Suggest: pre-rendered "let me take your details and have someone call you back", then write a `type='lead'` ticket from the raw transcript with no LLM call.
- **Reconsider §8's "don't optimise the ticket prompt".** True at $0.0003/conversation. Under free tier that call is ~1,200 tokens — a real share of the budget.

---

## 4. Security, correctness and design gaps

Ordered roughly by severity.

### 4.1 WebSocket origin is unchecked — the only guard on a public endpoint
§3.5 says "CORS is locked to the GitHub Pages origin. No wildcards." **CORS does not apply to WebSockets.** Browsers send cross-origin WS handshakes without preflight and without enforcement. Configuring FastAPI's `CORSMiddleware` will not protect the WS endpoint at all. The `Origin` header must be validated manually inside the handshake and the connection rejected on mismatch. As written, the single most exposed surface in the system — unauthenticated, public, spends your quota — has no working control.

### 4.2 Per-IP rate limiting will collapse to a single bucket behind Caddy
§11 requires per-IP limiting on the public WS endpoint. Behind a reverse proxy, `request.client.host` is the proxy's address, so every visitor shares one bucket and the first user locks out everyone. Requires `--proxy-headers` with an explicit trusted-hosts list and `X-Forwarded-For` parsing. Standard bug, worth naming explicitly because the doc's own quota exposure makes it expensive.

### 4.3 Session resume has no authentication
Phase 4: "a dropped socket resumes the same conversation row rather than starting a new one." No mechanism is specified. If the client simply presents a `conversation_id`, then any party who guesses or obtains one can append turns to — or read — another customer's conversation. Needs a server-issued signed resume token (HMAC over conversation id + expiry), never a bare UUID.

### 4.4 The dashboard RLS policy grants every authenticated user access to every ticket
§7: "write a read policy scoped to authenticated users only." Supabase Auth allows public sign-up by default, so this reduces to "anyone who registers can read all customer tickets." Needs public sign-up disabled *and* an operator allowlist or a JWT role claim, with the policy scoped to that.

### 4.5 Ticket idempotency is asserted but not enforced
§8 says "make it idempotent; one ticket per conversation", but `tickets` has no unique constraint on `conversation_id`. WS-close and the 5-minute timeout can race and produce two tickets. Add `unique (conversation_id)` and let the DB be the arbiter.

### 4.6 The inactivity sweeper has no column to sweep on
The 5-minute timeout cannot rely on the WS close handler (a hung socket never closes). It needs a periodic job querying "active and idle" — but `conversations` has no `last_activity_at`. As designed you would need `max(turns.ts)` per conversation on every sweep. Add `last_activity_at timestamptz`, updated on each turn, and index it.

### 4.7 Audio cache key is under-specified and will serve corrupt audio
Key is `sha256(voice + text)`. But edge-tts returns MP3 and Piper returns 22.05 kHz WAV. Flip the backend config flag and the cache serves the previous backend's bytes under the new backend's assumptions. Key must include **backend + format + sample rate**. Separately: pre-rendered fixed phrases (§6.2) must be **pinned** against LRU eviction, or the greeting gets evicted under load — exactly when you need it.

### 4.8 edge-tts failure handling is manual, and it is the component most likely to fail
The doc provides a config flag to switch backends. Given the documented 403 history, this needs to be an automatic **circuit breaker**: N consecutive failures → trip to Piper → periodic retry. A flag requires a human to notice at 3am.

### 4.9 The client-side audio path is hand-waved, and it is the hardest client work
"Audio streams to the browser and plays" spans two quite different implementations: streaming MP3 chunks via MediaSource versus decoding WAV into AudioBuffers. Two backends with two formats means either two client paths or server-side transcoding — and transcoding is precisely the CPU work §2 forbids. **Recommend: normalise on one wire format and have the client handle only that**, decided before Phase 2 rather than discovered during it.

### 4.10 Barge-in and persistence interact, undefined
When a turn is cancelled mid-generation, is the partial agent turn written to `turns`? `turns.text` is `not null`. Whatever is chosen affects transcript fidelity and therefore ticket quality. Needs an explicit rule — suggest: persist partial agent text with a `cancelled boolean`, since what the customer actually heard is what matters for the ticket.

### 4.11 Turn ordering relies on `ts`
The index is `(conversation_id, ts)`. Same-millisecond writes, clock skew, or reconnect races can reorder the transcript and corrupt the ticket. `turns.id` is already `bigserial` and monotonic — order by it, and index `(conversation_id, id)`.

### 4.12 No data protection posture at all
Voice capture, transcripts and contact details, with no consent line on the widget, no retention policy, no deletion path, and (depending on question 1) a provider tier that permits human review. For a UK-facing commercial product this is a gap in the plan, not just in the code.

### 4.13 §3.1 as written forbids the dashboard from working
"No API keys in the frontend, ever." But `dashboard.js` needs the Supabase URL and anon key. That key is *designed* to be public and is protected by RLS, so this is fine — but the absolutism means a future reader will either "fix" a non-bug or lose trust in the rule. Needs an explicit carve-out naming the anon key as the sole permitted exception, contingent on RLS being enabled.

### 4.14 Sentence-ordering on the TTS queue
§6.1 streams the LLM, splits on sentence boundaries and synthesises concurrently. Nothing specifies that audio must be **played in generation order** — sentence 2 can finish synthesis before sentence 1 (short sentence, or a cache hit against a cache miss). Needs an explicit ordered queue.

### 4.15 No test strategy
§11 covers ruff and mypy but no tests. The provider router with quota tracking and fallback is the highest-risk component and the easiest to test with mocked 429s and clock control. Recommend it be the one thing with real unit tests from Phase 1.

### 4.16 Smaller items
- `customer_ref` is never defined — what populates it for an anonymous widget user?
- No language column on `conversations` despite §12 asking about languages.
- No local development story. The operator is on Windows 10; Piper, edge-tts and the `.env` flow all assume the VPS. Needs either a documented Windows path or a container.
- No phase provisions the VPS Python environment or a deploy step, yet Phase 2 requires benchmarking on the VPS. Phase 0 should include deployment, not just Caddy + health.
- `openai/gpt-oss-20b` free-tier availability should be confirmed in the Groq console before it is designed around, given `llama-3.1-8b-instant` vanished a week ago.

---

## 5. Recommended structural changes

1. **Reframe §2.** Keep every CPU-driven decision, but state that quota is the primary constraint and CPU the secondary one. Add the §3 budget table to the document.
2. **Rewrite §4's LLM section** once question 1 is answered. `gemini-3.1-flash-lite` becomes primary or is dropped entirely; `llama-3.1-8b-instant` is removed.
3. **Add a `quota.py`** to the repo layout — the provider ledger. It is a peer of `llm.py`, not a detail inside it.
4. **Add a Phase 1.5: quota and exhaustion behaviour**, verified before TTS work begins. Cheap to build, and it determines whether the product is viable at the operator's expected volume. Better to learn that before building the voice pipeline.
5. **Move the concurrency cap decision into Phase 0** as a documented number derived from STT RPM, rather than a Phase 3 afterthought derived from CPU.
6. **Add security items 4.1–4.4 to Phase 0/1** rather than leaving them implicit. Origin checking and proxy headers are ten lines each and are painful to retrofit.
7. Keep Phase 7 exactly as it is. The threat model in it is correct and well-stated.

---

## 6. What was right and should not be second-guessed

Recording this so the review is not read as a rewrite:

- Database as source of truth, turns written as they happen, ticket built server-side from the DB. Correct, and it is what makes 4.10 a small problem instead of a large one.
- Offloading STT and LLM; refusing Kokoro; the audio cache as the main concurrency lever.
- Streaming with sentence-boundary splitting, pre-rendered fixed phrases, client-side VAD, barge-in. This is the right latency design.
- Per-stage latency logging from day one.
- The fallback-ticket-on-validation-failure rule — never drop a lead.
- Phase 7's untrusted-input threat model, and deferring it.
- Not logging transcript content at INFO.
