/**
 * lathe_guard_bridge.js — Physical-to-Virtual "Twin Mirror" Bridge (Phase 4: T4.4)
 * The Crafters Hub | LatheGuard Physical AI × CraftTwin 3D Simulator
 *
 * Connects to `ws://127.0.0.1:8766` (Nebius 3-Tier Hazard Advisor).
 * On physical Red-Zone breach:
 *   1. Triggers CraftTwin E-STOP state & visual overlay
 *   2. Places red breach ghost marker at normalized coordinates `norm_xy`
 *   3. Plays strictly synthetic Virtual Osta cue (`assets/audio/osta_safety_trip.mp3` — D-3 rule)
 *   4. Updates live bilingual coaching banner (Tier 0 -> Tier 1 -> Tier 2 report + Tavily sources)
 */
(function () {
  const WS_URL = "ws://127.0.0.1:8766";
  const state = { connected: false, estop: false, lastEvent: null, lastCoaching: null, lastReport: null };
  window.LatheGuardBridge = state;

  function ensureOverlay() {
    let el = document.getElementById("lg-twin-mirror-banner");
    if (el) return el;
    el = document.createElement("div");
    el.id = "lg-twin-mirror-banner";
    el.style.cssText =
      "position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:9999;" +
      "min-width:440px;max-width:780px;background:rgba(18,18,22,0.95);color:#f8fafc;" +
      "border:2px solid #ef4444;border-radius:12px;padding:12px 18px;font-family:system-ui,sans-serif;" +
      "box-shadow:0 16px 40px rgba(0,0,0,0.65);display:none;";
    document.body.appendChild(el);
    return el;
  }

  function ensureMarker() {
    let mk = document.getElementById("lg-twin-breach-marker");
    if (mk) return mk;
    mk = document.createElement("div");
    mk.id = "lg-twin-breach-marker";
    mk.style.cssText =
      "position:fixed;width:28px;height:28px;border:3px solid #ef4444;border-radius:50%;" +
      "background:rgba(239,68,68,0.35);pointer-events:none;z-index:9998;display:none;" +
      "transform:translate(-50%,-50%);box-shadow:0 0 18px #ef4444;";
    document.body.appendChild(mk);
    return mk;
  }

  function handleMessage(data) {
    if (!data || !data.type) return;
    const banner = ensureOverlay();
    if (data.type === "breach_event") {
      state.estop = true;
      state.lastEvent = data;
      const [nx, ny] = data.norm_xy || [0.5, 0.65];
      const mk = ensureMarker();
      mk.style.left = `${Math.round(nx * 100)}vw`;
      mk.style.top = `${Math.round(ny * 100)}vh`;
      mk.style.display = "block";
      banner.style.display = "block";
      banner.innerHTML =
        `<div style="color:#f87171;font-weight:700;font-size:12px;">[TWIN MIRROR E-STOP | TIER-0 LOCAL | ${data.zone} / ${data.side} | ${data.hazard_code || "H-02"}]</div>` +
        `<div style="font-size:14px;margin-top:4px;">${data.advice_en || ""}</div>` +
        `<div dir="rtl" style="font-size:14px;color:#fde047;margin-top:3px;">${data.advice_ar || ""}</div>`;
      try {
        const audio = new Audio("assets/audio/osta_safety_trip.mp3");
        audio.play().catch(() => {});
      } catch (e) {}
    } else if (data.type === "nebius_coaching") {
      state.lastCoaching = data;
      banner.style.display = "block";
      banner.innerHTML +=
        `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.15);font-size:13px;color:#38bdf8;">` +
        `<b>[TIER-${data.tier || 1} ${data.model || "Nemotron"} · ${Math.round(data.latency_ms || 0)}ms]:</b> ${data.advice || ""}</div>`;
    } else if (data.type === "incident_report") {
      state.lastReport = data;
      banner.style.display = "block";
      banner.innerHTML +=
        `<div style="margin-top:5px;font-size:12px;color:#f59e0b;">` +
        `<b>[TIER-2 NEBOSH REPORT · ${data.model || "Nemotron-Ultra"} · Tavily: ${data.tavily_sources || 0} sources]:</b> Saved to <code>${data.incident_dir || "incidents/"}</code> — ${data.summary || ""}</div>`;
    }
  }
  state.injectEvent = handleMessage;

  function connect() {
    try {
      const ws = new WebSocket(WS_URL);
      ws.onopen = () => { state.connected = true; };
      ws.onmessage = (ev) => { try { handleMessage(JSON.parse(ev.data)); } catch (e) {} };
      ws.onclose = () => { state.connected = false; setTimeout(connect, 2500); };
      ws.onerror = () => { ws.close(); };
    } catch (e) { setTimeout(connect, 2500); }
  }
  if (typeof window !== "undefined") connect();
})();
