"""
test_phase3_phase4.py — Unit & Integration Suite for Phase 3 (Profiles/Corpus) & Phase 4 (Recorder/HUD/Supervisor)
The Crafters Hub | LatheGuard Physical AI
"""

import json
import os
import sys
import time
from pathlib import Path
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "corpus"))

from calibrate_lathe import compute_calibration_profile, render_calibration_preview
from eval_corpus import evaluate_corpus
from incident_recorder import BlackBoxIncidentRecorder, purge_old_incidents
from lathe_hud_preview import render_hud_frame
from lathe_profile import list_available_profiles, load_lathe_profile
from lathe_shm import FaultCode, ROI_H, ROI_W, SafetyState, StatusFlag
import run_lathe_guard as sup

POLY = np.array(
    [[120, 180], [320, 180], [520, 180], [720, 180],
     [720, 420], [520, 420], [320, 420], [120, 420]],
    dtype=np.int32,
)


def test_lathe_profiles_and_calibration_wizard():
    """T3.1 & T3.2: Verify all 4 lathe profiles load and calibration wizard computes px_per_mm."""
    names = list_available_profiles()
    for req in ("crafters_hub_midi_lathe", "fox_f46_717_grey", "mc1018_yellow", "wl300_blue"):
        assert req in names
        p = load_lathe_profile(req)
        assert p["operator_side"] == "bottom"
        assert p["axis_y"] == 300

    cal = compute_calibration_profile(
        name="test_cal",
        axis_pts=((100, 302), (700, 298)),
        zone_pts=((120, 300), (720, 300)),
        ruler_pts=((200, 420), (350, 420)),
        ruler_mm=100.0,
        operator_side="bottom",
    )
    assert cal["axis_y"] == 300
    assert cal["px_per_mm"] == 1.5
    roi = np.full((ROI_H, ROI_W, 3), 26, dtype=np.uint8)
    vis = render_calibration_preview(roi, cal)
    assert vis.shape == (ROI_H, ROI_W, 3)

    from lathe_vision_capture import compute_dynamic_red_zone
    from lathe_safety_engine import RedZoneVisionDetector

    custom_poly = compute_dynamic_red_zone(
        roi,
        safety_margin_px=30,
        radius_clamp=(70, 140),
        axis_y=280,
        x_stations=[150, 350, 550, 750],
    )
    assert list(custom_poly[:4, 0]) == [150, 350, 550, 750]
    assert int(custom_poly[0, 1]) == 280 - (75 + 30)
    det_custom = RedZoneVisionDetector(axis_y=280)
    assert det_custom.small_axis_y == 70


def test_quick_corpus_gates_all_pass():
    """T3.3 & T3.4: 20-clip quick corpus (including real Crafters Hub lathe photos) passes all 5 P0 gates."""
    summary = evaluate_corpus(quick=True)
    assert summary["false_trips"] == 0
    assert summary["pos_hits"] == summary["pos_clips"]
    assert summary["all_passed"] is True


def test_blackbox_incident_recorder_and_30day_purge(tmp_path):
    """T4.1: Verify BlackBoxIncidentRecorder writes playable clip.mp4 + event.json and purges >30d folders."""
    # Create a fake 31-day-old incident folder
    old_dir = tmp_path / "20250101_000000_2"
    old_dir.mkdir(parents=True)
    (old_dir / "event.json").write_text("{}", encoding="utf-8")
    old_ts = time.time() - (31.5 * 86400.0)
    os.utime(old_dir, (old_ts, old_ts))

    rec = BlackBoxIncidentRecorder(incidents_root=tmp_path, fps=15.0, pre_seconds=2.0)
    assert not old_dir.exists(), "31-day-old incident folder should have been auto-purged at startup!"

    base = np.full((ROI_H, ROI_W, 3), 30, dtype=np.uint8)
    for _ in range(6):
        rec.push_frame(base, POLY, int(SafetyState.ARMED_MONITORING), 0, -1, -1)
    for _ in range(3):
        rec.push_frame(base, POLY, int(SafetyState.FAULT_TRIPPED), int(FaultCode.HAND_OR_TOOL_MOAT), 412, 388)

    inc_dir = rec.flush_incident(status_flags=int(StatusFlag.DETECTOR_ARMED))
    assert (inc_dir / "event.json").is_file()
    assert (inc_dir / "snapshot.jpg").is_file()
    ev = json.loads((inc_dir / "event.json").read_text(encoding="utf-8"))
    assert ev["fault_code"] == int(FaultCode.HAND_OR_TOOL_MOAT)
    assert ev["frames_recorded"] == 9

    clip_file = inc_dir / ev["clip_file"]
    assert clip_file.is_file() and clip_file.stat().st_size > 500
    cap = cv2.VideoCapture(str(clip_file))
    ret, frame = cap.read()
    cap.release()
    assert ret and frame is not None and frame.shape == (ROI_H, ROI_W, 3)


def test_hud_v2_arabic_subtitle_and_relay_pill_render():
    """T4.2 & H-3: Verify HUD v2 renders bilingual Arabic/English subtitle and relay pill without crashing."""
    base = np.zeros((ROI_H, ROI_W, 3), dtype=np.uint8)
    coaching = {
        "tier": 1,
        "advice": "STOP! Hand breached Red-Zone. | قف فوراً يا صنايعي! يدك دخلت منطقة الخطر.",
        "latency_ms": 604.0,
        "model": "nvidia/Nemotron-3_5-Lightning",
        "zone": "CENTER",
        "side": "OPERATOR",
        "hazard_code": "H-02",
    }
    vis = render_hud_frame(
        frame_roi=base,
        poly=POLY,
        state=SafetyState.FAULT_TRIPPED,
        fault_code=int(FaultCode.HAND_OR_TOOL_MOAT),
        status_flags=int(StatusFlag.DETECTOR_ARMED),
        p99_us=740,
        fid=42,
        bx=412,
        by=388,
        coaching=coaching,
    )
    assert vis.shape == (ROI_H, ROI_W, 3)
    # Subtitle region (y=530..595) must have non-background glyph pixels rendered
    subtitle_patch = vis[530:595, 14:700]
    assert int(np.count_nonzero(subtitle_patch > 60)) > 150


def test_supervisor_demo_rearm_rejected_on_physical_com():
    """T4.3: --demo-rearm with a physical COM port (without --mock or --mock-serial) must exit with code 2."""
    args = sup.parse_args(["--com", "COM3", "--demo-rearm", "3.0"])
    rc = sup.run_supervisor(args)
    assert rc == 2
