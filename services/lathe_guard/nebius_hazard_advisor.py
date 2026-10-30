"""
nebius_hazard_advisor.py — 3-Tier Async Nebius AI Hazard Reasoning, Voice Coach & NEBOSH Incident Reporter
The Crafters Hub | LatheGuard Physical AI — Nebius × NVIDIA Hackathon Submission
Author: Hosam (Co-Founder, The Crafters Hub)

Architecture (3-Tier Model Routing):
  - Runs as an INDEPENDENT async sidecar alongside lathe_safety_engine.py
  - Polls the Seqlock SHM for FSM state changes at ~5Hz (NEVER inline in the 100Hz safety loop)
  - Tier 0 (< 5 ms, Offline-First Local Template): Instant bilingual (Arabic + English) coaching
    keyed by (FaultCode, Zone, Side), broadcast as `breach_event` before any network call.
  - Tier 1 (~400-800 ms, NVIDIA Nemotron-3_5-Lightning via Nebius Token Factory): Contextual
    master-artisan ("Osta") coaching via non-blocking `asyncio.to_thread`, broadcast as `nebius_coaching`.
  - Tier 2 (Async Background, Largest Discovered NVIDIA Nemotron Model): Structured NEBOSH-style
    incident report mapped to CraftTwin hazard codes H-01..H-04, saved to `incidents/<ts>_<code>/report.md`
    and broadcast as `incident_report`.
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

# LatheGuard shared memory reader (same process space)
try:
    from lathe_shm import (
        DEFAULT_SHM_NAME,
        FaultCode,
        LatheShmReader,
        ROI_H,
        ROI_W,
        SafetyState,
        StatusFlag,
    )
    SHM_AVAILABLE = True
except ImportError:
    SHM_AVAILABLE = False
    ROI_W, ROI_H = 900, 600
    DEFAULT_SHM_NAME = "cabinet_lathe_shm"
    print("[WARN] lathe_shm not importable — running in standalone mock mode")

# ─── T2.1: Offline-first credential loading (Environment first, .env second) ──
_ENV_PATHS = [
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent.parent / ".env",
]
for _p in _ENV_PATHS:
    if _p.is_file():
        load_dotenv(dotenv_path=str(_p), override=False)

NEBIUS_API_KEY: str = os.environ.get("NEBIUS_API_KEY", "").strip()
NEBIUS_BASE_URL: str = os.environ.get("NEBIUS_BASE_URL", "https://api.studio.nebius.com/v1/").strip()
NEBIUS_ORG_ID: str = os.environ.get("NEBIUS_ORG_ID", "").strip()
NEBIUS_MODEL: str = os.environ.get("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning").strip()
NEBIUS_REPORT_MODEL: str = os.environ.get("NEBIUS_REPORT_MODEL", "").strip()

TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "").strip()
TAVILY_URL: str = os.environ.get("TAVILY_URL", "https://api.tavily.com/search").strip()
TAVILY_TIMEOUT_S: float = float(os.environ.get("TAVILY_TIMEOUT_S", "6.0") or 6.0)

_OFFLINE_WARNED = False
_UNSET = object()


def tavily_search_sync(
    query: str,
    api_key: Optional[str] = None,
    max_results: int = 3,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Runtime Tavily Search API call (REST, zero extra dependencies).
    Returns the parsed JSON (`results`: [{title,url,content}], `answer`) or raises on failure.
    Used ONLY off the safety path (Tier-2 incident report); never blocks the 100Hz loop.
    """
    import urllib.request

    key = (TAVILY_API_KEY if api_key is None else api_key).strip()
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    body = json.dumps(
        {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": True,
            "exclude_domains": [
                "facebook.com",
                "youtube.com",
                "reddit.com",
                "pinterest.com",
                "tiktok.com",
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout or TAVILY_TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


_HAZARD_QUERIES = {
    "H-01": "OSHA 1910.213 AAW woodturning lathe headstock chuck entanglement kickback safety standard",
    "H-02": "AAW OSHA woodturning lathe tool rest 1/8 inch 3mm gap clearance no gloves red zone safety rule",
    "H-03": "AAW woodturning lathe gouge overhang fulcrum tool rest bevel catch prevention safety standard",
    "H-04": "ISO 13849 OSHA 1910.213 machine guarding deadman relay watchdog fault emergency stop reset",
}


async def fetch_tavily_guidance(
    hazard_code: str,
    fault_name: str = "",
    search_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Async, fail-soft wrapper: fetches live published safety guidance for the incident's hazard code.
    Returns {"query","answer","sources":[{title,url,snippet}],"latency_ms"} or None (no key / error / timeout).
    """
    fn = search_fn or tavily_search_sync
    if search_fn is None and not TAVILY_API_KEY:
        return None
    query = _HAZARD_QUERIES.get(hazard_code, _HAZARD_QUERIES["H-02"])
    t0 = time.perf_counter()
    try:
        data = await asyncio.to_thread(fn, query)
    except Exception as e:
        _safe_print(f"[TAVILY] [WARN] Skipping live guidance ({type(e).__name__}: {e})")
        return None
    sources = [
        {
            "title": (r.get("title") or "").strip(),
            "url": (r.get("url") or "").strip(),
            "snippet": (r.get("content") or "").strip()[:300],
        }
        for r in (data.get("results") or [])
        if r.get("url")
    ]
    if not sources:
        return None
    return {
        "query": query,
        "answer": (data.get("answer") or "").strip(),
        "sources": sources,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def format_tavily_section(guidance: Optional[Dict[str, Any]]) -> str:
    """Markdown block appended to report.md citing the Tavily sources."""
    if not guidance:
        return ""
    lines = [
        "\n## Live Reference Guidance (Tavily Search)\n",
        f"_Query: `{guidance['query']}` ({guidance['latency_ms']} ms)_\n",
    ]
    if guidance.get("answer"):
        lines.append(f"{guidance['answer']}\n")
    for s in guidance["sources"]:
        lines.append(f"- [{s['title'] or s['url']}]({s['url']})")
    return "\n".join(lines) + "\n"


def _safe_print(msg: str) -> None:
    """Prints safely on Windows cp1252/cp1256 consoles without raising UnicodeEncodeError."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def build_nebius_client(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 4.0,
    organization: Optional[str] = None,
) -> Optional[Any]:
    """Creates a non-retrying timeout-bounded Nebius OpenAI client, or None if no key is configured."""
    global _OFFLINE_WARNED
    key = NEBIUS_API_KEY if api_key is None else api_key.strip()
    url = NEBIUS_BASE_URL if base_url is None else base_url.strip()
    org = (NEBIUS_ORG_ID if organization is None else organization).strip() or None
    if not key or OpenAI is None:
        if not _OFFLINE_WARNED:
            _safe_print("[NEBIUS ADVISOR] No NEBIUS_API_KEY configured - running in offline-first Tier-0 local mode.")
            _OFFLINE_WARNED = True
        return None
    return OpenAI(api_key=key, base_url=url, organization=org, timeout=timeout, max_retries=0)


nebius_client = build_nebius_client()

# ─── Default Lathe Geometry Profile (H-1: Operator at Bottom y > 300) ─────────
DEFAULT_PROFILE: Dict[str, Any] = {
    "name": "crafters_hub_midi_lathe",
    "roi": {"x0": 510, "y0": 240},
    "axis_y": 300,
    "x_stations": [120, 320, 520, 720],
    "operator_side": "bottom",
    "margin_px": 25,
    "radius_clamp": [60, 160],
    "px_per_mm": None,
    "zones": {"headstock_x_max": 270, "tailstock_x_min": 570},
    "fps": 60,
    "mains_hz": 50,
}

# ───Prompts & Hazard Catalog ──────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Osta (أسطى) — a master Egyptian woodturning mentor embedded in the LatheGuard AI safety system at The Crafters Hub in Cairo.
You speak with the calm authority and warmth of a seasoned Cairo workshop teacher.
When LatheGuard detects a hazard, provide:
1. A brief, clear diagnosis of what triggered the safety event (1-2 sentences max).
2. A corrective coaching instruction (1 sentence, actionable, OSHA/AAW standard).
3. A short bilingual closing phrase (Arabic then English).
Strict Rules:
- Keep total response under 95 words.
- Do NOT use bullet points or markdown lists — speak naturally.
- NEVER invent measurements, speeds, or distances that were not explicitly provided in the user prompt."""

REPORT_SYSTEM_PROMPT = """You are the Principal Industrial Safety Auditor (NEBOSH / OSHA 1910.213 standard) for The Crafters Hub Woodturning Studio.
Generate a concise, structured markdown incident report (<220 words) containing:
1. **Incident Summary** (Fault code, zone, approach side, and CraftTwin Hazard Code mapping: H-01 Spinning Chuck/Headstock Pinch, H-02 Red-Zone Hand/Tool Intrusion, H-03 Tool Catch/Collar Over-Reach, H-04 Sensor/Watchdog Integrity Fault).
2. **Root-Cause Hypothesis** (Why this breach or fault occurred in woodturning practice).
3. **Hierarchy of Controls Recommendation** (Engineering + Administrative / Pedagogical actions before re-arming).
Rule: Never fabricate physical measurements not present in the telemetry, do NOT invent sensors not in the system (LatheGuard uses an overhead 60 FPS RGB camera + 100Hz Arduino deadman relay, not LiDAR/TOF), and NEVER recommend wearing gloves at a rotating wood lathe (entanglement hazard per OSHA 1910.213 & AAW; PPE is ANSI Z87.1 face shield/eye protection, hearing protection, and respiratory dust protection only)."""

HAZARD_PROFILES = {
    "red_zone_breach": "A hand or chisel tip entered the red-zone danger boundary near the spinning spindle.",
    "collar_breach": "A CraftTwin bicolor tool collar crossed inside the red-zone pinch boundary.",
    "camera_stale": "The vision camera dropped out or froze — no fresh frame received for >50ms.",
    "camera_obstructed": "The overhead camera lens became obstructed or blinded by heavy dust/lighting drop.",
    "camera_moved": "The overhead camera shifted from its calibrated mounting position.",
    "serial_fault": "The hardware watchdog serial connection to the relay board failed.",
    "detector_exception": "An internal vision detector exception triggered the fail-safe brake.",
    "degraded_pedagogy": "The GPU training renderer slowed. Safety monitoring is still active but the 3D simulation has degraded.",
    "unknown": "A safety event was detected by the LatheGuard system.",
}

# ─── T2.3: Tier-0 Instant Local Bilingual Templates (< 5 ms, zero network) ───
# Strictly aligned with lathe_shm.FaultCode:
#   NONE=0, COLLAR=1, HAND_OR_TOOL_MOAT=2, CAMERA_STALE=3, SERIAL_FAULT=4,
#   SIMULATED=5, CAMERA_OBSTRUCTED=6, CAMERA_MOVED=7, DETECTOR_EXCEPTION=8
TIER0_TEMPLATES: Dict[int, Dict[str, str]] = {
    1: {  # FaultCode.COLLAR
        "ar": "تنبيه طوق الأمان! طوق الإزميل تجاوز حد الأمان عند {zone_ar}. ارجع بالإزميل للخلف وثبّت الكعب على المسند.",
        "en": "COLLAR BREACH! Tool safety collar crossed into the {zone} Red-Zone ({side}). Pull the gouge back onto the tool rest.",
        "hazard_code": "H-03",
    },
    2: {  # FaultCode.HAND_OR_TOOL_MOAT
        "ar": "قف فوراً يا صنايعي! يدك أو الإزميل دخل منطقة الخطر عند {zone_ar}. ارفع يدك وأعد ضبط المسند على بعد ٣ مم قبل إعادة التشغيل.",
        "en": "STOP! Hand or chisel breached the {zone} Red-Zone from the {side} side. Withdraw clear and set the tool rest within 3 mm before re-arming.",
        "hazard_code": "H-02",
    },
    3: {  # FaultCode.CAMERA_STALE
        "ar": "فصل أمان تلقائي! انقطعت إشارة كاميرا المراقبة لأكثر من ٥٠ مللي ثانية. افحص كابل الكاميرا قبل إعادة التفعيل.",
        "en": "FAIL-SAFE TRIP! Camera frame age exceeded 50 ms (dropout or frozen stream). Verify camera connection before re-arming.",
        "hazard_code": "H-04",
    },
    4: {  # FaultCode.SERIAL_FAULT
        "ar": "فصل أمان تلقائي! تعذر الاتصال بوحدة القاطع الكهربائي. افحص توصيلة الـ USB والريلاي.",
        "en": "FAIL-SAFE TRIP! Hardware watchdog serial link failed. Inspect relay USB connection before re-arming.",
        "hazard_code": "H-04",
    },
    5: {  # FaultCode.SIMULATED
        "ar": "إيقاف طوارئ تجريبي! تم رصد اقتراب إصبع افتراضي عند {zone_ar}.",
        "en": "SIMULATED BREACH! Virtual fingertip entered the {zone} Red-Zone ({side}).",
        "hazard_code": "H-02",
    },
    6: {  # FaultCode.CAMERA_OBSTRUCTED
        "ar": "فصل أمان تلقائي! عدسة الكاميرا محجوبة أو مغطاة بالغبار. نظّف العدسة وتأكد من إضاءة المخرطة.",
        "en": "FAIL-SAFE TRIP! Overhead camera lens is obstructed or blinded by dust. Clean the lens and verify workshop lighting.",
        "hazard_code": "H-04",
    },
    7: {  # FaultCode.CAMERA_MOVED
        "ar": "فصل أمان تلقائي! تحركت الكاميرا من مكانها المعاير. ثبّت حامل الكاميرا وأعد المعايرة.",
        "en": "FAIL-SAFE TRIP! Camera shifted from its calibrated mount (>16 px). Re-secure the camera arm and re-calibrate.",
        "hazard_code": "H-04",
    },
    8: {  # FaultCode.DETECTOR_EXCEPTION
        "ar": "فصل أمان تلقائي! حدث خطأ داخلي في محرك الرؤية الحاسوبية وتم إيقاف المخرطة للأمان.",
        "en": "FAIL-SAFE TRIP! Internal vision detector exception triggered emergency stop.",
        "hazard_code": "H-04",
    },
}

ZONE_AR = {
    "HEADSTOCK": "الغراب الثابت (الظرف)",
    "CENTER": "منتصف الشغلة",
    "TAILSTOCK": "الغراب المتحرك",
}

# ─── State machine tracking ───────────────────────────────────────────────────
_last_reported_state: int = -1
_last_advice_ts: float = 0.0
ADVICE_COOLDOWN_S: float = 8.0
_cached_report_model: Optional[str] = None
SESSION_ID: str = uuid.uuid4().hex[:10]


def zone_label(x: int, y: int, prof: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Maps 900x600 ROI pixel coordinates (x, y) to (zone, side) using the active lathe profile."""
    p = prof or DEFAULT_PROFILE
    zones = p.get("zones", {"headstock_x_max": 270, "tailstock_x_min": 570})
    if x < int(zones.get("headstock_x_max", 270)):
        zone = "HEADSTOCK"
    elif x > int(zones.get("tailstock_x_min", 570)):
        zone = "TAILSTOCK"
    else:
        zone = "CENTER"
    op_below = str(p.get("operator_side", "bottom")).lower() == "bottom"
    axis_y = int(p.get("axis_y", 300))
    side = "OPERATOR" if ((y > axis_y) == op_below) else "FAR"
    return zone, side


def build_tier0_event(
    fault_code: int,
    intrusion_x: int = 450,
    intrusion_y: int = 420,
    prof: Optional[Dict[str, Any]] = None,
    session_id: str = SESSION_ID,
) -> Dict[str, Any]:
    """Generates the instant (< 5 ms) Tier-0 bilingual breach_event dictionary."""
    p = prof or DEFAULT_PROFILE
    x = int(intrusion_x) if intrusion_x > 0 else ROI_W // 2
    y = int(intrusion_y) if intrusion_y > 0 else ROI_H // 2
    zone, side = zone_label(x, y, p)

    fault_name = "HAND_OR_TOOL_MOAT"
    if SHM_AVAILABLE:
        try:
            fault_name = FaultCode(int(fault_code)).name
        except ValueError:
            fault_name = "HAND_OR_TOOL_MOAT"

    code_key = int(fault_code) if int(fault_code) in TIER0_TEMPLATES else 2
    tpl = TIER0_TEMPLATES[code_key]
    zone_ar = ZONE_AR.get(zone, "منطقة الدوران")

    # Headstock chuck pinch overrides hazard code to H-01 for spatial breaches (COLLAR=1, HAND_OR_TOOL_MOAT=2, SIMULATED=5)
    hazard_code = "H-01" if (code_key in (1, 2, 5) and zone == "HEADSTOCK") else tpl["hazard_code"]

    advice_ar = tpl["ar"].format(zone_ar=zone_ar, zone=zone, side=side)
    advice_en = tpl["en"].format(zone_ar=zone_ar, zone=zone, side=side)

    axis_y = int(p.get("axis_y", 300))
    px_per_mm = p.get("px_per_mm")
    mm_from_axis: Optional[float] = None
    if px_per_mm and float(px_per_mm) > 0:
        mm_from_axis = round(abs(y - axis_y) / float(px_per_mm), 1)

    return {
        "type": "breach_event",
        "tier": 0,
        "ts": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "fault_code": int(fault_code) if int(fault_code) > 0 else 2,
        "fault_name": fault_name,
        "hazard_code": hazard_code,
        "roi_xy": [x, y],
        "norm_xy": [round(x / float(ROI_W), 3), round(y / float(ROI_H), 3)],
        "zone": zone,
        "side": side,
        "mm_from_axis": mm_from_axis,
        "advice_ar": advice_ar,
        "advice_en": advice_en,
        "advice": f"{advice_en} | {advice_ar}",
    }


def _get_hazard_context(state: int, reason: str = "", fault_code: int = 0) -> str:
    """Map FSM state and FaultCode (1..8) / reason to a human-readable hazard profile."""
    r_low = reason.lower()
    if fault_code == 1 or "collar" in r_low:
        return HAZARD_PROFILES["collar_breach"]
    if fault_code in (2, 5) or "red" in r_low or "breach" in r_low or "moat" in r_low or "simulat" in r_low:
        return HAZARD_PROFILES["red_zone_breach"]
    if fault_code == 3 or "stale" in r_low:
        return HAZARD_PROFILES["camera_stale"]
    if fault_code == 4 or "serial" in r_low:
        return HAZARD_PROFILES["serial_fault"]
    if fault_code == 6 or "obstruct" in r_low:
        return HAZARD_PROFILES["camera_obstructed"]
    if fault_code == 7 or "moved" in r_low:
        return HAZARD_PROFILES["camera_moved"]
    if fault_code == 8 or "exception" in r_low:
        return HAZARD_PROFILES["detector_exception"]
    if "camera" in r_low:
        return HAZARD_PROFILES["camera_stale"]
    if state == 2:  # DEGRADED_PEDAGOGY
        return HAZARD_PROFILES["degraded_pedagogy"]
    return HAZARD_PROFILES["unknown"]


def _call_chat_completion_sync(
    client: Any,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 220,
) -> Any:
    """Synchronous OpenAI chat completion call executed inside `asyncio.to_thread`."""
    return client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=max_tokens,
        temperature=0.35,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


async def get_nebius_coaching(
    state_name: str,
    hazard_context: str,
    tier0_event: Optional[Dict[str, Any]] = None,
    status_flags: int = 0,
    client: Any = _UNSET,
) -> Dict[str, Any]:
    """
    T2.4 Tier-1: Calls Nebius Token Factory (NVIDIA Nemotron) off the event loop via `asyncio.to_thread`.
    Falls back cleanly to Tier-0 local text if offline or on timeout/error.
    """
    active_client = nebius_client if client is _UNSET else client
    fallback_text = (
        tier0_event["advice"]
        if tier0_event and "advice" in tier0_event
        else "Safety event detected. Stop the lathe, secure the workpiece, and verify tool rest position before restarting."
    )

    if active_client is None:
        return {
            "tier": 0,
            "advice": fallback_text,
            "state": state_name,
            "model": "offline-tier0",
            "latency_ms": 0.0,
            "tokens": 0,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    t0 = time.perf_counter()
    details = [f"LatheGuard FSM triggered: {state_name}", f"Hazard context: {hazard_context}"]
    if tier0_event:
        details.append(
            f"Telemetry: fault={tier0_event.get('fault_name')} ({tier0_event.get('hazard_code')}), "
            f"zone={tier0_event.get('zone')}, approach_side={tier0_event.get('side')}, "
            f"roi_xy={tier0_event.get('roi_xy')}, status_flags=0x{int(status_flags):02X}"
        )
        if tier0_event.get("mm_from_axis") is not None:
            details.append(f"Measured distance from spindle axis: {tier0_event['mm_from_axis']} mm")
    details.append("Provide concise Osta coaching advice for the operator without inventing any unstated numbers.")
    user_message = "\n".join(details)

    try:
        response = await asyncio.to_thread(
            _call_chat_completion_sync,
            active_client,
            NEBIUS_MODEL,
            SYSTEM_PROMPT,
            user_message,
            220,
        )
        raw_content = response.choices[0].message.content or ""
        if "</think>" in raw_content:
            raw_content = raw_content.split("</think>", 1)[-1]
        advice = raw_content.strip() or fallback_text
        latency_ms = (time.perf_counter() - t0) * 1000.0
        model_used = getattr(response, "model", None) or NEBIUS_MODEL
        tokens_used = response.usage.total_tokens if getattr(response, "usage", None) else 0

        _safe_print(f"[NEBIUS TIER-1] [OK] {model_used} | {latency_ms:.0f}ms | {tokens_used} tokens")
        _safe_print(f"[OSTA COACHING] {advice}")
        return {
            "tier": 1,
            "advice": advice,
            "state": state_name,
            "model": model_used,
            "latency_ms": round(latency_ms, 1),
            "tokens": tokens_used,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        _safe_print(f"[NEBIUS TIER-1] [WARN] Fallback to Tier-0 ({e})")
        return {
            "tier": 0,
            "advice": fallback_text,
            "state": state_name,
            "model": "fallback",
            "error": str(e),
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "timestamp": datetime.now(UTC).isoformat(),
        }


def discover_report_model(client: Any = _UNSET) -> str:
    """T2.5: Discovers the largest available NVIDIA Nemotron model on Nebius Token Factory (cached)."""
    global _cached_report_model
    report_override = NEBIUS_REPORT_MODEL or os.environ.get("NEBIUS_REPORT_MODEL", "").strip()
    if report_override:
        return report_override
    if _cached_report_model:
        return _cached_report_model

    active_client = nebius_client if client is _UNSET else client
    if active_client is None:
        _cached_report_model = NEBIUS_MODEL
        return _cached_report_model

    try:
        models_resp = active_client.models.list()
        ids = [m.id for m in getattr(models_resp, "data", []) if "nemotron" in m.id.lower()]
        if ids:
            # Prefer larger reasoning/instruct Nemotron models for Tier-2 post-incident analysis
            def _rank(m_id: str) -> Tuple[int, str]:
                low = m_id.lower()
                if "ultra" in low or "253b" in low or "550b" in low:
                    return (5, m_id)
                if "super" in low or "70b" in low or "49b" in low:
                    return (4, m_id)
                if "nano" in low or "8b" in low:
                    return (2, m_id)
                if "lightning" in low:
                    return (3, m_id)
                return (1, m_id)

            ids.sort(key=_rank, reverse=True)
            _cached_report_model = ids[0]
            return _cached_report_model
    except Exception:
        pass

    _cached_report_model = NEBIUS_MODEL
    return _cached_report_model


async def generate_tier2_incident_report(
    tier0_event: Dict[str, Any],
    tier1_result: Dict[str, Any],
    incidents_root: Optional[Path] = None,
    client: Any = _UNSET,
    tavily_search_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    T2.5 Tier-2: Generates an async NEBOSH-style incident note mapped to CraftTwin H-01..H-04
    and writes `incidents/<ts>_<fault_code>/report.md`.
    """
    root = incidents_root or (Path(__file__).resolve().parent / "incidents")
    ts_slug = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    fault_code = int(tier0_event.get("fault_code", 2))
    inc_dir = root / f"{ts_slug}_{fault_code}"
    inc_dir.mkdir(parents=True, exist_ok=True)

    active_client = nebius_client if client is _UNSET else client
    report_model = await asyncio.to_thread(discover_report_model, active_client)

    default_md = (
        f"# LatheGuard Safety Incident Report (`{tier0_event.get('hazard_code', 'H-02')}`)\n\n"
        f"- **Timestamp (UTC):** {tier0_event.get('ts')}\n"
        f"- **Session ID:** `{tier0_event.get('session_id')}`\n"
        f"- **Fault Code:** `{tier0_event.get('fault_name')}` (`{fault_code}`)\n"
        f"- **Location:** Zone `{tier0_event.get('zone')}` | Side `{tier0_event.get('side')}` | ROI `{tier0_event.get('roi_xy')}`\n\n"
        f"## 1. Incident Summary\n"
        f"{tier0_event.get('advice_en')}\n\n"
        f"## 2. Immediate Coaching (Tier 1)\n"
        f"{tier1_result.get('advice', tier0_event.get('advice_en'))}\n\n"
        f"## 3. Hierarchy of Controls (NEBOSH / AAW)\n"
        f"- **Engineering Control:** 100Hz hardware deadman relay tripped (`0xFF`).\n"
        f"- **Administrative Control:** Verify 3 mm tool-rest clearance and spindle lock before manual reset.\n"
    )

    summary_text = tier0_event.get("advice_en", "Safety incident recorded.")
    # Runtime Tavily call (off the safety path, fail-soft): live published guidance for this hazard code.
    tavily = await fetch_tavily_guidance(
        str(tier0_event.get("hazard_code", "H-02")),
        str(tier0_event.get("fault_name", "")),
        search_fn=tavily_search_fn,
    )
    tavily_md = format_tavily_section(tavily)
    default_md += tavily_md
    if active_client is not None:
        ref_ctx = ""
        if tavily:
            ref_ctx = "Live reference guidance (Tavily): " + (tavily["answer"] or tavily["sources"][0]["snippet"]) + "\n"
        prompt = (
            f"{ref_ctx}"
            f"Incident Telemetry:\n"
            f"- Fault: {tier0_event.get('fault_name')} (Code {fault_code})\n"
            f"- CraftTwin Hazard Code: {tier0_event.get('hazard_code')}\n"
            f"- Zone: {tier0_event.get('zone')}, Approach Side: {tier0_event.get('side')}\n"
            f"- ROI Coordinates: {tier0_event.get('roi_xy')}\n"
            f"- Distance from axis (mm): {tier0_event.get('mm_from_axis')}\n"
            f"- Tier-1 Osta Coaching Given: {tier1_result.get('advice')}\n"
            f"Write the structured NEBOSH incident report now."
        )
        try:
            resp = await asyncio.to_thread(
                _call_chat_completion_sync,
                active_client,
                report_model,
                REPORT_SYSTEM_PROMPT,
                prompt,
                1400,
            )
            raw = resp.choices[0].message.content or ""
            if "</think>" in raw:
                raw = raw.split("</think>", 1)[-1]
            if raw.strip():
                default_md = (
                    f"# LatheGuard NEBOSH Incident Report (`{tier0_event.get('hazard_code', 'H-02')}`)\n\n"
                    f"- **Timestamp (UTC):** {tier0_event.get('ts')}\n"
                    f"- **Model:** `{getattr(resp, 'model', report_model)}`\n\n"
                    f"{raw.strip()}\n"
                ) + tavily_md
                first_line = [ln.strip("#* ") for ln in raw.strip().splitlines() if ln.strip()]
                if first_line:
                    summary_text = first_line[0][:200]
        except Exception as e:
            _safe_print(f"[NEBIUS TIER-2] [WARN] Using local NEBOSH template ({e})")

    report_path = inc_dir / "report.md"
    report_path.write_text(default_md, encoding="utf-8")

    rel_dir = f"incidents/{inc_dir.name}"
    return {
        "type": "incident_report",
        "tier": 2,
        "model": report_model if active_client is not None else "offline-template",
        "incident_dir": rel_dir,
        "summary": summary_text,
        "hazard_code": tier0_event.get("hazard_code", "H-02"),
        "tavily_sources": len(tavily["sources"]) if tavily else 0,
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def handle_state_transition(
    current_state: int,
    ws_broadcast_fn: Callable[[str], Any],
    fault_code: int = 2,
    intrusion_x: int = 450,
    intrusion_y: int = 420,
    status_flags: int = 0,
    prof: Optional[Dict[str, Any]] = None,
    client: Optional[Any] = None,
    spawn_tier2: bool = True,
) -> bool:
    """
    Processes a single FSM state observation with F-5 fixes:
      1. `FAULT_TRIPPED` (state 3) ALWAYS bypasses `ADVICE_COOLDOWN_S`.
      2. `_last_reported_state` is updated ONLY when the state is actually handled.
      3. Emits Tier-0 `breach_event` (< 5 ms) BEFORE awaiting Tier-1 `nebius_coaching`.
    Returns True if a hazard event was handled.
    """
    global _last_reported_state, _last_advice_ts

    HAZARD_STATES = {
        (int(SafetyState.FAULT_TRIPPED) if SHM_AVAILABLE else 3): "FAULT_TRIPPED",
        (int(SafetyState.DEGRADED_PEDAGOGY) if SHM_AVAILABLE else 2): "DEGRADED_PEDAGOGY",
    }
    fault_state_val = int(SafetyState.FAULT_TRIPPED) if SHM_AVAILABLE else 3

    if current_state == _last_reported_state:
        return False

    if current_state not in HAZARD_STATES:
        # Clean transition back to DISARMED or ARMED_MONITORING
        _last_reported_state = current_state
        return False

    now = time.time()
    is_fault_trip = current_state == fault_state_val
    if (not is_fault_trip) and (now - _last_advice_ts) <= ADVICE_COOLDOWN_S:
        # Cooldown active for DEGRADED_PEDAGOGY; do NOT update _last_reported_state so it isn't swallowed
        return False

    state_name = HAZARD_STATES[current_state]
    eff_fault = int(fault_code) if int(fault_code) > 0 else (2 if is_fault_trip else 0)

    # 1. Tier 0: Instant local bilingual event (< 5 ms)
    tier0_ev = build_tier0_event(
        fault_code=eff_fault if eff_fault > 0 else 2,
        intrusion_x=intrusion_x,
        intrusion_y=intrusion_y,
        prof=prof,
    )
    await ws_broadcast_fn(json.dumps(tier0_ev))

    # 2. Tier 1: Contextual NVIDIA Nemotron coaching (non-blocking thread)
    hazard_ctx = _get_hazard_context(current_state, fault_code=eff_fault)
    t1_res = await get_nebius_coaching(
        state_name,
        hazard_ctx,
        tier0_event=tier0_ev,
        status_flags=status_flags,
        client=client,
    )
    await ws_broadcast_fn(
        json.dumps(
            {
                "type": "nebius_coaching",
                **t1_res,
            }
        )
    )

    _last_advice_ts = time.time()
    _last_reported_state = current_state

    # 3. Tier 2: Async NEBOSH incident report on FAULT_TRIPPED
    if is_fault_trip and spawn_tier2:
        async def _run_tier2():
            t2_ev = await generate_tier2_incident_report(tier0_ev, t1_res, client=client)
            await ws_broadcast_fn(json.dumps(t2_ev))

        asyncio.create_task(_run_tier2())

    return True


async def monitor_safety_state(
    ws_broadcast_fn: Callable[[str], Any],
    shm_name: str = DEFAULT_SHM_NAME,
    prof: Optional[Dict[str, Any]] = None,
):
    """
    Polls the Seqlock SHM at 5Hz for FSM state changes.
    When a hazard state is detected, runs 3-tier routing (Tier 0 -> Tier 1 -> Tier 2).
    """
    reader = None
    if prof is None:
        try:
            from lathe_profile import load_lathe_profile
            prof = load_lathe_profile(os.environ.get("LATHEGUARD_PROFILE"))
            print(f"[NEBIUS ADVISOR] Lathe profile loaded: {prof.get('name')} (px_per_mm={prof.get('px_per_mm')})")
        except Exception as e:
            print(f"[NEBIUS ADVISOR] Profile load failed ({e}) - using defaults")
    if SHM_AVAILABLE:
        try:
            reader = LatheShmReader(shm_name=shm_name)
            print(f"[NEBIUS ADVISOR] SHM reader attached to '{shm_name}'")
        except Exception as e:
            print(f"[NEBIUS ADVISOR] SHM unavailable ({e}) — running in simulation mode")

    print("[NEBIUS ADVISOR] Monitoring LatheGuard FSM at 5Hz (3-Tier Routing Active)...")
    while True:
        await asyncio.sleep(0.2)

        current_state = -1
        fault_code = 0
        intr_x, intr_y = 450, 420
        status_flags = 0

        if reader:
            try:
                current_state = int(reader.header.safety_state)
                fault_code = int(reader.header.fault_code)
                intr_x = int(reader.header.intrusion_x)
                intr_y = int(reader.header.intrusion_y)
                status_flags = int(reader.header.status_flags)
                if current_state == int(SafetyState.FAULT_TRIPPED) and fault_code == 0:
                    last_cap = int(reader.header.last_capture_ns or 0)
                    age_ns = time.perf_counter_ns() - last_cap
                    fault_code = int(FaultCode.CAMERA_STALE) if (last_cap > 0 and age_ns > 50_000_000) else int(FaultCode.HAND_OR_TOOL_MOAT)
            except Exception:
                current_state = -1
        else:
            current_state = int(time.time()) % 4
            fault_code = 2

        if current_state < 0:
            continue

        await handle_state_transition(
            current_state=current_state,
            ws_broadcast_fn=ws_broadcast_fn,
            fault_code=fault_code,
            intrusion_x=intr_x,
            intrusion_y=intr_y,
            status_flags=status_flags,
            prof=prof,
        )


# ─── WebSocket broadcast server ───────────────────────────────────────────────
connected_clients: set = set()


async def ws_handler(websocket):
    connected_clients.add(websocket)
    print(f"[WS] Client connected: {websocket.remote_address}. Total: {len(connected_clients)}")
    try:
        await websocket.wait_closed()
    finally:
        connected_clients.discard(websocket)
        print(f"[WS] Client disconnected. Total: {len(connected_clients)}")


async def broadcast(message: str):
    if connected_clients:
        await asyncio.gather(
            *(ws.send(message) for ws in connected_clients),
            return_exceptions=True,
        )


async def main(shm_name: str, ws_port: int):
    import websockets
    print(f"[NEBIUS ADVISOR] Starting WebSocket server on port {ws_port}...")
    print(f"[NEBIUS ADVISOR] Tier-1 Model: {NEBIUS_MODEL}")
    print(f"[NEBIUS ADVISOR] Nebius Endpoint: {NEBIUS_BASE_URL}")

    async with websockets.serve(ws_handler, "0.0.0.0", ws_port):
        await monitor_safety_state(broadcast, shm_name=shm_name)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nebius AI 3-Tier Hazard Advisor for LatheGuard")
    parser.add_argument(
        "--shm",
        default=DEFAULT_SHM_NAME if SHM_AVAILABLE else "cabinet_lathe_shm",
        help="Seqlock shared memory name (default: cabinet_lathe_shm)",
    )
    parser.add_argument(
        "--ws-port",
        type=int,
        default=8766,
        help="WebSocket port for broadcasting coaching to HUD & CraftTwin (default: 8766)",
    )
    parser.add_argument(
        "--test-breach",
        action="store_true",
        help="Run a one-shot 3-tier (Tier-0 + Tier-1 + Tier-2) breach self-test and exit",
    )
    args = parser.parse_args()

    if args.test_breach:
        async def _run_test_breach():
            try:
                from lathe_profile import load_lathe_profile
                prof = load_lathe_profile(os.environ.get("LATHEGUARD_PROFILE") or "mc1018_yellow")
            except Exception:
                prof = DEFAULT_PROFILE
            t0_ev = build_tier0_event(fault_code=2, intrusion_x=412, intrusion_y=388, prof=prof)
            print("[TIER 0]", json.dumps(t0_ev, ensure_ascii=False))
            t1_res = await get_nebius_coaching("FAULT_TRIPPED", "Red-zone breach", tier0_event=t0_ev)
            print("[TIER 1]", json.dumps(t1_res, ensure_ascii=False))
            t2_res = await generate_tier2_incident_report(t0_ev, t1_res)
            print("[TIER 2]", json.dumps(t2_res, ensure_ascii=False))
        asyncio.run(_run_test_breach())
    else:
        asyncio.run(main(shm_name=args.shm, ws_port=args.ws_port))
