// Shared conversation widget: WebSocket, streaming text, audio playback,
// microphone capture and barge-in.
//
// Used by index.html (visitor) and assistant.html (owner). The two differ only
// in whether they send a session token on connect -- everything below is
// identical, and duplicating it across two pages would be a maintenance trap.
//
// Endpointing is energy-based rather than a neural VAD: @ricky0123/vad-web
// measured 13.3 MB raw / 4.75 MB gzipped, four times the budget §3.2 allows for
// a widget that must load on mobile data, and onnxruntime-web no longer ships a
// smaller build. An AnalyserNode costs nothing and gives both automatic
// end-of-utterance and barge-in.

const SILENCE_HANGOVER_MS = 900;   // quiet this long ends the utterance
const MIN_UTTERANCE_MS = 400;      // shorter than this is a cough, not speech
const SPEECH_MARGIN = 2.5;         // times the measured noise floor
// Onset must sustain across a few polls. Without this a single loud frame
// starts a turn -- and on speakers, the assistant's own voice leaking past echo
// cancellation would make it interrupt itself.
const SPEECH_ONSET_FRAMES = 3;     // x 50 ms poll = ~150 ms sustained
const POLL_MS = 50;

export function startWidget({ wsUrl, sessionToken = "", tokenKey, el,
                             consentNotice = "", strings = {} }) {
  let socket = null;
  let streaming = null;      // the agent bubble currently being written into
  let retryDelay = 1000;

  // ---- audio playback ---------------------------------------------------
  let audioCtx = null;
  let decodeChain = Promise.resolve();   // keeps decodes in arrival order
  const audioQueue = [];
  let currentSource = null;  // the node actually sounding right now
  let audioEpoch = 0;        // bumped on every interruption
  let playing = false;
  let muted = false;

  // ---- microphone -------------------------------------------------------
  let listening = false;
  let micStream = null, recorder = null, analyser = null, pollTimer = null;
  let chunks = [];
  let speaking = false, silenceSince = 0, speechStartedAt = 0, loudFrames = 0;
  let noiseFloor = 0.01;

  const storedToken = () => {
    try { return sessionStorage.getItem(tokenKey) || ""; } catch { return ""; }
  };
  const storeToken = (t) => {
    try { sessionStorage.setItem(tokenKey, t); } catch { /* private mode */ }
  };

  // Arabic changes the reading direction and the placeholder, but nothing
  // else -- the server decides the language, and it tells us what it chose
  // whether that came from the toggle or from what it heard.
  let lang = "en";
  function setLanguage(next) {
    if (next === lang) return;
    lang = next;
    const rtl = lang === "ar";
    el.log.dir = rtl ? "rtl" : "ltr";
    el.input.dir = rtl ? "rtl" : "ltr";
    el.input.placeholder = rtl
      ? (strings.arPlaceholder || "اكتب رسالتك…")
      : (strings.enPlaceholder || el.input.placeholder);
    if (el.lang) {
      el.lang.textContent = rtl ? "EN" : "ع";
      el.lang.title = rtl ? "Switch to English" : "التحويل إلى العربية";
    }
  }

  function setState(text, cls) {
    el.state.textContent = text;
    el.dot.className = "dot" + (cls ? " " + cls : "");
  }

  function bubble(cls, text = "") {
    const node = document.createElement("div");
    node.className = "msg " + cls;
    node.textContent = text;
    el.log.appendChild(node);
    el.log.scrollTop = el.log.scrollHeight;
    return node;
  }

  function setComposerEnabled(on) {
    el.input.disabled = !on;
    el.send.disabled = !on;
    if (on) el.input.focus();
  }

  function ensureAudio() {
    // Browsers block AudioContext until a user gesture; sending is one.
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
  }

  function playNext() {
    const buffer = audioQueue.shift();
    if (!buffer) { playing = false; currentSource = null; return; }
    playing = true;
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(audioCtx.destination);
    // Guarded: a source we stopped deliberately also fires onended, and without
    // this it would start the sentence we just cancelled.
    source.onended = () => { if (currentSource === source) playNext(); };
    currentSource = source;
    source.start();
  }

  function stopAudio() {
    audioEpoch++;              // voids decodes still in flight
    audioQueue.length = 0;
    if (currentSource) {
      // Clearing the queue is not enough -- the sentence already sounding must
      // be stopped, or the assistant talks over whoever is interrupting it.
      currentSource.onended = null;
      try { currentSource.stop(); } catch { /* already ended */ }
      currentSource = null;
    }
    playing = false;
  }

  function enqueueAudio(msg) {
    if (muted || !audioCtx) return;
    const epoch = audioEpoch;
    // Chained rather than parallel: decodeAudioData resolves out of order for
    // differently-sized clips, which would shuffle the reply's sentences.
    decodeChain = decodeChain.then(async () => {
      if (epoch !== audioEpoch) return;
      try {
        const bytes = Uint8Array.from(atob(msg.data), (c) => c.charCodeAt(0));
        const buffer = await audioCtx.decodeAudioData(bytes.buffer);
        if (epoch !== audioEpoch) return;   // interrupted during the decode
        audioQueue.push(buffer);
        if (!playing) playNext();
      } catch (e) {
        console.warn("could not decode audio", e);
      }
    });
  }

  // ---- microphone -------------------------------------------------------

  function rms() {
    const buf = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    return Math.sqrt(sum / buf.length);
  }

  async function startListening() {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    ensureAudio();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    audioCtx.createMediaStreamSource(micStream).connect(analyser);

    // Calibrate against the actual room rather than a guessed constant.
    const samples = [];
    for (let i = 0; i < 12; i++) {
      samples.push(rms());
      await new Promise((r) => setTimeout(r, 40));
    }
    noiseFloor = Math.max(0.005, samples.sort()[Math.floor(samples.length / 2)]);

    listening = true;
    el.mic.classList.add("live");
    el.mic.setAttribute("aria-pressed", "true");
    setState("listening", "ok");
    pollTimer = setInterval(poll, POLL_MS);
  }

  function stopListening() {
    listening = false;
    clearInterval(pollTimer);
    if (recorder && recorder.state === "recording") recorder.stop();
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    micStream = recorder = analyser = null;
    speaking = false; loudFrames = 0;
    el.mic.classList.remove("live", "hearing");
    el.mic.setAttribute("aria-pressed", "false");
    setState("connected", "ok");
  }

  function poll() {
    if (!analyser) return;
    const loud = rms() > noiseFloor * SPEECH_MARGIN;
    loudFrames = loud ? loudFrames + 1 : 0;

    if (loudFrames >= SPEECH_ONSET_FRAMES && !speaking) {
      speaking = true;
      speechStartedAt = Date.now();
      el.mic.classList.add("hearing");
      // Barge-in: kill playback and tell the server *before* the upload, so it
      // can cancel the LLM and TTS work rather than paying for it first.
      if (playing || audioQueue.length) {
        stopAudio();
        send({ type: "barge_in" });
      }
      chunks = [];
      recorder = new MediaRecorder(micStream, { mimeType: "audio/webm;codecs=opus" });
      recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = sendUtterance;
      recorder.start();
      silenceSince = 0;
    } else if (!loud && speaking) {
      if (!silenceSince) silenceSince = Date.now();
      if (Date.now() - silenceSince > SILENCE_HANGOVER_MS) {
        speaking = false;
        el.mic.classList.remove("hearing");
        const spoken = Date.now() - speechStartedAt - SILENCE_HANGOVER_MS;
        if (recorder && recorder.state === "recording") {
          recorder.stop();
          if (spoken < MIN_UTTERANCE_MS) chunks = [];
        }
      }
    } else if (loud && speaking) {
      silenceSince = 0;
    }
  }

  async function sendUtterance() {
    if (!chunks.length) return;
    const blob = new Blob(chunks, { type: "audio/webm" });
    chunks = [];
    // Groq bills a 10-second minimum per request, so a fragment costs the same
    // as a sentence. Drop the ones that are obviously not speech.
    if (blob.size < 2000) return;
    const b64 = await new Promise((resolve) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result.split(",")[1]);
      fr.readAsDataURL(blob);
    });
    setState("transcribing…", "ok");
    send({ type: "audio", data: b64, mime: "audio/webm" });
  }

  // ---- socket -----------------------------------------------------------

  function send(obj) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(obj));
    }
  }

  function connect() {
    setState("connecting…");
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      retryDelay = 1000;
      setState("connected", "ok");
      setComposerEnabled(true);
    };

    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      switch (msg.type) {
        case "ready":
          // Offer whatever we hold; the server ignores anything that does not
          // verify, and mode is derived from the session, never asserted.
          send({ type: "hello", token: storedToken(), session: sessionToken });
          return;
        case "resume_token":
          storeToken(msg.token);
          return;
        case "mode":
          if (el.mode) el.mode.textContent = msg.mode;
          return;
        case "lang":
          setLanguage(msg.lang);
          return;
        case "resumed":
          el.log.innerHTML = "";
          for (const t of msg.turns) {
            bubble(t.role === "customer" ? "user" : "agent", t.text);
          }
          return;
        case "interrupted":
          stopAudio();
          if (streaming) streaming.classList.remove("cursor");
          streaming = null;
          setComposerEnabled(true);
          return;
        case "transcript":
          // Echoed back so a misrecognition is visible before it reaches an item.
          bubble("user", msg.text);
          return;
        case "not_heard":
          setState(listening ? "listening" : "connected", "ok");
          setComposerEnabled(true);
          return;
        case "audio":
          enqueueAudio(msg);
          return;
        case "token":
          if (!streaming) {
            streaming = bubble("agent");
            streaming.classList.add("cursor");
          }
          streaming.textContent += msg.text;
          el.log.scrollTop = el.log.scrollHeight;
          return;
        case "done": {
          if (streaming) streaming.classList.remove("cursor");
          streaming = null;
          // §7: we cannot tune what we cannot see, and this is the cheapest
          // possible view of it.
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent =
            `${msg.model} · first token ${msg.first_token_ms}ms · total ${msg.total_ms}ms`;
          el.log.appendChild(meta);
          el.log.scrollTop = el.log.scrollHeight;
          setComposerEnabled(true);
          return;
        }
        case "error":
          if (streaming) streaming.classList.remove("cursor");
          streaming = null;
          bubble("error", msg.message);
          setComposerEnabled(true);
          return;
      }
    };

    socket.onclose = (event) => {
      setComposerEnabled(false);
      streaming = null;
      // 1008 is the server refusing on policy -- a disallowed Origin, too many
      // connections, or at capacity. Retrying would just be refused again.
      if (event.code === 1008) {
        setState("refused", "bad");
        bubble("error", `Connection refused: ${event.reason || "policy"}`);
        return;
      }
      setState("reconnecting…", "bad");
      setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 15000);
    };

    socket.onerror = () => setState("connection error", "bad");
  }

  // ---- wiring -----------------------------------------------------------

  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = el.input.value.trim();
    if (!text || !socket || socket.readyState !== WebSocket.OPEN) return;
    ensureAudio();      // this submit is the user gesture audio needs
    stopAudio();        // a new question cancels whatever is still speaking
    bubble("user", text);
    send({ type: "user_message", text });
    el.input.value = "";
    setComposerEnabled(false);
  });

  el.mute.addEventListener("click", () => {
    muted = !muted;
    el.mute.textContent = muted ? "🔇" : "🔊";
    el.mute.setAttribute("aria-pressed", String(muted));
    el.mute.title = muted ? "Unmute the voice" : "Mute the voice";
    if (muted) stopAudio();
  });

  // §13: visitor mode collects other people's data, so the microphone is
  // gated behind an explicit acknowledgement the first time. Remembered per
  // tab, not forever -- consent that silently persists across sessions is not
  // much of a consent.
  const CONSENT_KEY = "assistant.consent";
  function hasConsented() {
    if (!consentNotice) return true;      // owner mode: no notice, no gate
    try { return sessionStorage.getItem(CONSENT_KEY) === "1"; } catch { return false; }
  }
  function recordConsent() {
    try { sessionStorage.setItem(CONSENT_KEY, "1"); } catch { /* private mode */ }
  }

  if (el.lang) {
    el.lang.addEventListener("click", () => {
      // Asks the server; it answers with a "lang" message either way, so the
      // UI never disagrees with the prompt and voice actually in use.
      send({ type: "set_lang", lang: lang === "ar" ? "en" : "ar" });
    });
  }

  el.mic.addEventListener("click", async () => {
    try {
      if (listening) { stopListening(); return; }
      if (!hasConsented()) {
        if (!window.confirm(consentNotice)) return;
        recordConsent();
      }
      await startListening();
    } catch (e) {
      bubble("error", "Could not access the microphone: " + e.message);
      stopListening();
    }
  });

  connect();
}
