"""
test_nebius_advisor.py — Unit + Integration tests for 3-Tier Nebius Hazard Advisor (Phase 2: T2.1-T2.6)
Tests run in mock mode (no SHM, no live serial, no real Nebius call in unit tests).
"""
import asyncio
import json
import os
import sys
import time
import pytest

# Allow import from parent
sys.path.insert(0, os.path.dirname(__file__))
if not os.environ.get("NEBIUS_API_KEY"):
    os.environ["NEBIUS_API_KEY"] = "TEST_KEY_MOCK"
if not os.environ.get("NEBIUS_BASE_URL"):
    os.environ["NEBIUS_BASE_URL"] = "https://api.studio.nebius.com/v1/"


# ─── Legacy & Core Unit Tests ────────────────────────────────────────────────

def test_hazard_context_red_zone():
    from nebius_hazard_advisor import _get_hazard_context, HAZARD_PROFILES
    ctx = _get_hazard_context(3, reason="Red-Zone Breach at pixel (450, 300)")
    assert ctx == HAZARD_PROFILES["red_zone_breach"]


def test_hazard_context_stale_frame():
    from nebius_hazard_advisor import _get_hazard_context, HAZARD_PROFILES
    ctx = _get_hazard_context(3, reason="Camera frame stale (75.3 ms > 50 ms)")
    assert ctx == HAZARD_PROFILES["camera_stale"]


def test_hazard_context_degraded():
    from nebius_hazard_advisor import _get_hazard_context, HAZARD_PROFILES
    ctx = _get_hazard_context(2, reason="")
    assert ctx == HAZARD_PROFILES["degraded_pedagogy"]


def test_nebius_client_initialized():
    from nebius_hazard_advisor import nebius_client, NEBIUS_BASE_URL
    assert nebius_client is not None
    assert "nebius" in NEBIUS_BASE_URL.lower() or "api" in NEBIUS_BASE_URL.lower()


def test_model_name_set():
    from nebius_hazard_advisor import NEBIUS_MODEL
    assert "nemotron" in NEBIUS_MODEL.lower() or "llama" in NEBIUS_MODEL.lower() or "nvidia" in NEBIUS_MODEL.lower()


def test_cooldown_constant():
    from nebius_hazard_advisor import ADVICE_COOLDOWN_S
    assert ADVICE_COOLDOWN_S >= 5.0, "Cooldown too short — would hammer Nebius API"


def test_system_prompt_bilingual():
    from nebius_hazard_advisor import SYSTEM_PROMPT
    assert "Arabic" in SYSTEM_PROMPT or "أسطى" in SYSTEM_PROMPT
    assert "English" in SYSTEM_PROMPT or "OSHA" in SYSTEM_PROMPT
    assert "NEVER invent" in SYSTEM_PROMPT


def test_hazard_profiles_complete():
    from nebius_hazard_advisor import HAZARD_PROFILES
    required = {
        "red_zone_breach",
        "collar_breach",
        "camera_stale",
        "serial_fault",
        "camera_obstructed",
        "camera_moved",
        "detector_exception",
        "degraded_pedagogy",
        "unknown",
    }
    assert required.issubset(set(HAZARD_PROFILES.keys()))


def test_fault_code_enum_alignment_with_shm():
    """Verify every FaultCode (1..8) in lathe_shm.py matches TIER0_TEMPLATES and _get_hazard_context."""
    from lathe_shm import FaultCode
    from nebius_hazard_advisor import TIER0_TEMPLATES, build_tier0_event, _get_hazard_context, HAZARD_PROFILES

    expected_map = {
        FaultCode.COLLAR: ("H-03", "collar_breach", "COLLAR"),
        FaultCode.HAND_OR_TOOL_MOAT: ("H-02", "red_zone_breach", "STOP!"),
        FaultCode.CAMERA_STALE: ("H-04", "camera_stale", "50 ms"),
        FaultCode.SERIAL_FAULT: ("H-04", "serial_fault", "serial"),
        FaultCode.SIMULATED: ("H-02", "red_zone_breach", "SIMULATED"),
        FaultCode.CAMERA_OBSTRUCTED: ("H-04", "camera_obstructed", "lens"),
        FaultCode.CAMERA_MOVED: ("H-04", "camera_moved", "shifted"),
        FaultCode.DETECTOR_EXCEPTION: ("H-04", "detector_exception", "exception"),
    }
    for fc, (exp_hcode, exp_profile_key, exp_substr) in expected_map.items():
        code_int = int(fc)
        assert code_int in TIER0_TEMPLATES
        ev = build_tier0_event(fault_code=code_int, intrusion_x=450, intrusion_y=400)
        assert ev["fault_code"] == code_int
        assert ev["fault_name"] == fc.name
        assert ev["hazard_code"] == exp_hcode
        assert exp_substr.lower() in ev["advice_en"].lower()
        ctx = _get_hazard_context(3, reason="", fault_code=code_int)
        assert ctx == HAZARD_PROFILES[exp_profile_key]
    # Verify CAMERA_STALE (3) in HEADSTOCK zone stays H-04 (does NOT become H-01)
    ev_stale_hs = build_tier0_event(fault_code=int(FaultCode.CAMERA_STALE), intrusion_x=180, intrusion_y=400)
    assert ev_stale_hs["hazard_code"] == "H-04"
    # Verify HAND_OR_TOOL_MOAT (2) and SIMULATED (5) in HEADSTOCK zone become H-01
    ev_moat_hs = build_tier0_event(fault_code=int(FaultCode.HAND_OR_TOOL_MOAT), intrusion_x=180, intrusion_y=400)
    assert ev_moat_hs["hazard_code"] == "H-01"
    ev_sim_hs = build_tier0_event(fault_code=int(FaultCode.SIMULATED), intrusion_x=180, intrusion_y=400)
    assert ev_sim_hs["hazard_code"] == "H-01"


# ─── Phase 2 (T2.1-T2.6) Tests ───────────────────────────────────────────────

def test_offline_first_no_key_returns_none_and_tier0():
    """T2.1: Missing NEBIUS_API_KEY must never raise; build_nebius_client('') returns None."""
    from nebius_hazard_advisor import build_nebius_client, get_nebius_coaching, build_tier0_event
    assert build_nebius_client(api_key="") is None

    t0_ev = build_tier0_event(fault_code=2, intrusion_x=412, intrusion_y=388)
    res = asyncio.run(get_nebius_coaching("FAULT_TRIPPED", "Red-zone breach", tier0_event=t0_ev, client=None))
    assert res["tier"] == 0
    assert res["model"] == "offline-tier0"
    assert "STOP!" in res["advice"] and "قف فوراً" in res["advice"]


def test_zone_label_and_tier0_schema_h1_operator_bottom():
    """T2.3 & T2.6: Verify zone_label and Tier-0 breach_event schema with H-1 operator at bottom (y > 300)."""
    from nebius_hazard_advisor import zone_label, build_tier0_event

    assert zone_label(180, 410) == ("HEADSTOCK", "OPERATOR")
    assert zone_label(450, 410) == ("CENTER", "OPERATOR")
    assert zone_label(650, 210) == ("TAILSTOCK", "FAR")

    ev = build_tier0_event(
        fault_code=2,
        intrusion_x=412,
        intrusion_y=388,
        prof={
            "axis_y": 300,
            "operator_side": "bottom",
            "zones": {"headstock_x_max": 270, "tailstock_x_min": 570},
            "px_per_mm": 2.0,
        },
    )
    assert ev["type"] == "breach_event"
    assert ev["tier"] == 0
    assert ev["fault_code"] == 2
    assert ev["fault_name"] == "HAND_OR_TOOL_MOAT"
    assert ev["zone"] == "CENTER"
    assert ev["side"] == "OPERATOR"
    assert ev["roi_xy"] == [412, 388]
    assert ev["norm_xy"] == [0.458, 0.647]
    assert ev["mm_from_axis"] == 44.0
    assert len(ev["advice_ar"]) > 15
    assert len(ev["advice_en"]) > 15


@pytest.mark.asyncio
async def test_get_coaching_fallback_on_bad_key(monkeypatch):
    """T2.2: Verify graceful fallback when Nebius API raises or times out (< 5s)."""
    from nebius_hazard_advisor import nebius_client, get_nebius_coaching
    import openai

    def mock_create(**kwargs):
        raise openai.AuthenticationError("Invalid API key", response=None, body=None)

    monkeypatch.setattr(nebius_client.chat.completions, "create", mock_create)

    t0 = time.perf_counter()
    result = await get_nebius_coaching("FAULT_TRIPPED", "Red-zone breach simulated")
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0
    assert "advice" in result
    assert result["model"] == "fallback"
    assert len(result["advice"]) > 10


@pytest.mark.asyncio
async def test_degraded_then_fault_within_cooldown_both_coached_and_tier0_emitted_first(tmp_path):
    """
    T2.2, T2.3, T2.5, T2.6:
      1. DEGRADED_PEDAGOGY (2) followed immediately (<8s) by FAULT_TRIPPED (3) -> BOTH are coached (F-5 fix).
      2. Tier-0 `breach_event` is emitted BEFORE the mocked Tier-1 API call finishes.
      3. Tier-2 `incident_report` generates `report.md` asynchronously.
    """
    import nebius_hazard_advisor as adv

    adv._last_reported_state = 1  # ARMED_MONITORING
    adv._last_advice_ts = 0.0

    events = []
    api_entered_after_tier0 = False

    async def ws_collect(msg: str):
        events.append(json.loads(msg))

    class DummyMsg:
        content = "<think>internal</think>Pull the gouge back immediately and lock the spindle. سلامتك أولاً — Safety first."

    class DummyChoice:
        message = DummyMsg()

    class DummyUsage:
        total_tokens = 48

    class DummyResp:
        choices = [DummyChoice()]
        model = "nvidia/Nemotron-3_5-Lightning"
        usage = DummyUsage()

    class DummyCompletions:
        def create(self, **kwargs):
            nonlocal api_entered_after_tier0
            # Verify Tier-0 breach_event was ALREADY broadcast before API call even finishes!
            if any(e.get("type") == "breach_event" for e in events):
                api_entered_after_tier0 = True
            time.sleep(0.02)
            return DummyResp()

    class DummyChat:
        completions = DummyCompletions()

    class DummyModels:
        def list(self):
            class M:
                id = "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1"
            class R:
                data = [M()]
            return R()

    class DummyClient:
        chat = DummyChat()
        models = DummyModels()

    dummy_client = DummyClient()

    # Step 1: DEGRADED_PEDAGOGY (state 2) triggers coaching
    handled_deg = await adv.handle_state_transition(
        current_state=2,
        ws_broadcast_fn=ws_collect,
        fault_code=0,
        client=dummy_client,
        spawn_tier2=False,
    )
    assert handled_deg is True
    assert len(events) == 2  # Tier 0 + Tier 1 for DEGRADED_PEDAGOGY

    # Step 2: Within 0.05s (< 8s cooldown!), FAULT_TRIPPED (state 3) occurs -> MUST bypass cooldown!
    events.clear()
    handled_fault = await adv.handle_state_transition(
        current_state=3,
        ws_broadcast_fn=ws_collect,
        fault_code=2,
        intrusion_x=412,
        intrusion_y=388,
        client=dummy_client,
        spawn_tier2=False,
    )
    assert handled_fault is True
    assert api_entered_after_tier0 is True
    assert len(events) == 2
    assert events[0]["type"] == "breach_event" and events[0]["tier"] == 0
    assert events[1]["type"] == "nebius_coaching" and events[1]["tier"] == 1
    assert "<think>" not in events[1]["advice"]

    # Step 3: Verify Tier-2 incident report generator creates report.md and discovers largest Nemotron
    adv._cached_report_model = None
    t2_ev = await adv.generate_tier2_incident_report(
        events[0],
        events[1],
        incidents_root=tmp_path,
        client=dummy_client,
    )
    assert t2_ev["type"] == "incident_report"
    assert t2_ev["tier"] == 2
    assert "253B" in t2_ev["model"]
    report_files = list(tmp_path.glob("*/report.md"))
    assert len(report_files) == 1
    assert "H-02" in report_files[0].read_text(encoding="utf-8")


# ─── Tavily runtime integration (Tier-2 live reference guidance) ──────────────
def _fake_tavily(query, **_kw):
    return {
        "answer": "Keep tool rest within 3 mm.",
        "results": [{"title": "Safety", "url": "https://example.org/s", "content": "tool rest guidance"}],
    }


def test_tavily_guidance_success_and_section():
    import nebius_hazard_advisor as nha
    g = asyncio.run(nha.fetch_tavily_guidance("H-02", "HAND_OR_TOOL_MOAT", search_fn=_fake_tavily))
    assert g and g["sources"][0]["url"] == "https://example.org/s"
    md = nha.format_tavily_section(g)
    assert "Tavily" in md and "https://example.org/s" in md


def test_tavily_failsoft_on_error_and_no_key(monkeypatch):
    import nebius_hazard_advisor as nha

    def boom(query, **_kw):
        raise TimeoutError("down")

    assert asyncio.run(nha.fetch_tavily_guidance("H-02", search_fn=boom)) is None
    monkeypatch.setattr(nha, "TAVILY_API_KEY", "")
    assert asyncio.run(nha.fetch_tavily_guidance("H-02")) is None
    assert nha.format_tavily_section(None) == ""


def test_tier2_report_includes_tavily_sources(tmp_path):
    import nebius_hazard_advisor as nha
    ev = nha.build_tier0_event(2, 412, 388) if False else {
        "ts": "t", "session_id": "s", "fault_code": 2, "fault_name": "HAND_OR_TOOL_MOAT",
        "hazard_code": "H-02", "zone": "CENTER", "side": "OPERATOR", "roi_xy": [1, 2],
        "advice_en": "STOP",
    }
    out = asyncio.run(nha.generate_tier2_incident_report(
        ev, {"advice": "x"}, incidents_root=tmp_path, client=None, tavily_search_fn=_fake_tavily))
    assert out["tavily_sources"] == 1
    report = next(tmp_path.rglob("report.md")).read_text(encoding="utf-8")
    assert "Live Reference Guidance (Tavily Search)" in report
