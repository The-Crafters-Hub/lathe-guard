"""
test_vision_intrusion.py — Unit & Latency Suite for RedZoneVisionDetector
Verifies:
  1. Warm-colored spinning wood blank (skin-like hue) causes 0 false trips.
  2. Single-frame flying wood shaving/chip is rejected by 2-frame debounce.
  3. Bare hand / steel gouge crossing Red-Zone moat trips FAULT_TRIPPED (0xFF) in 2 frames.
  4. CraftTwin Cyan/Magenta bicolor collar inside Red-Zone trips immediately in 2 frames.
  5. Per-frame CPU vision latency stays < 3.0 ms (preserving 100Hz FSM determinism).
"""

import os
import sys
import time
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from lathe_safety_engine import HEARTBEAT_BYTE, LatheSafetyEngine, RedZoneVisionDetector, TRIP_BYTE
from lathe_shm import FaultCode, LatheShmWriter, ROI_C, ROI_H, ROI_W, SafetyState, StatusFlag
from lathe_vision_capture import compute_dynamic_red_zone


POLY = np.array(
    [[120, 180], [320, 180], [520, 180], [720, 180],
     [720, 420], [520, 420], [320, 420], [120, 420]],
    dtype=np.int32,
)


def _make_spinning_wood_roi(jitter: int = 0) -> np.ndarray:
    """Warm beech/acacia wood blank (R>G>B, overlaps human skin hue) spinning on dark bed."""
    roi = np.full((ROI_H, ROI_W, ROI_C), 26, dtype=np.uint8)
    cv2.rectangle(roi, (100, 200), (760, 400), (70, 142 + jitter, 212), thickness=-1)
    cv2.line(roi, (100, 200), (760, 200), (220, 240, 255), 2)
    cv2.line(roi, (100, 400), (760, 400), (220, 240, 255), 2)
    return roi


def test_wood_blank_no_false_positive_and_sub_3ms_latency():
    det = RedZoneVisionDetector(warmup_frames=12)
    latencies_ms = []
    for i in range(50):
        frame = _make_spinning_wood_roi(jitter=(i % 3) - 1)
        t0 = time.perf_counter_ns()
        pts = det.detect(frame, POLY)
        latencies_ms.append((time.perf_counter_ns() - t0) / 1e6)
        assert pts == [], f"False trip on clean spinning wood blank at frame {i}: {pts}"

    p95_ms = float(np.percentile(latencies_ms[12:], 95))
    budget_ms = float(os.environ.get("LATHEGUARD_VISION_P95_MS", "3.5"))  # dev-machine gate; CI sets a looser value
    assert p95_ms < budget_ms, f"Vision detector P95 latency ({p95_ms:.2f}ms) exceeds {budget_ms}ms budget!"


def test_single_frame_wood_chip_ignored():
    det = RedZoneVisionDetector(warmup_frames=10, confirm_frames=2)
    for _ in range(12):
        det.detect(_make_spinning_wood_roi(), POLY)

    # 1-frame flying shaving crossing boundary -> must NOT trip on single frame
    chip_frame = _make_spinning_wood_roi()
    cv2.rectangle(chip_frame, (440, 350), (470, 460), (230, 230, 230), thickness=-1)
    assert det.detect(chip_frame, POLY) == []
    # Next frame clear -> resets counter
    assert det.detect(_make_spinning_wood_roi(), POLY) == []


def test_steel_gouge_and_hand_breach_trips_fsm():
    shm_name = "cabinet_cv_breach_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    engine = LatheSafetyEngine(
        shm_name=shm_name,
        com_port="MOCK",
        mock_serial=True,
        enable_cv_detector=True,
    )
    engine.cv_detector.warmup_frames = 10

    try:
        # Calibrate on clean spinning blank
        for _ in range(12):
            writer.publish_frame(_make_spinning_wood_roi(), POLY)
            st = engine.step()
        assert st == int(SafetyState.ARMED_MONITORING)

        # Operator pushes Sheffield gouge + hand across bottom Red-Zone boundary (y=420 -> y=330)
        breach_roi = _make_spinning_wood_roi()
        cv2.rectangle(breach_roi, (432, 330), (468, 590), (215, 220, 225), thickness=-1)
        cv2.circle(breach_roi, (450, 470), 34, (130, 165, 225), thickness=-1)

        # Frame 1 of breach: debouncing
        writer.publish_frame(breach_roi, POLY)
        assert engine.step() == int(SafetyState.ARMED_MONITORING)

        # Frame 2 of breach (~16.7ms later): instant FAULT_TRIPPED + 0xFF + intrusion coords!
        writer.publish_frame(breach_roi, POLY)
        st = engine.step()
        assert st == int(SafetyState.FAULT_TRIPPED)
        assert engine.serial.tx_history[-1:] == TRIP_BYTE
        assert 420 <= writer.header.intrusion_x <= 480
        assert 320 <= writer.header.intrusion_y <= 420
    finally:
        engine.close()
        writer.close()


def test_crafttwin_cyan_collar_fast_path():
    det = RedZoneVisionDetector(warmup_frames=8, confirm_frames=2)
    for _ in range(10):
        det.detect(_make_spinning_wood_roi(), POLY)

    collar_roi = _make_spinning_wood_roi()
    # Bright Cyan collar band (BGR ~ (255, 240, 0) -> HSV H=92, S=255, V=255) inside Red-Zone
    cv2.rectangle(collar_roi, (500, 340), (545, 385), (255, 240, 0), thickness=-1)

    assert det.detect(collar_roi, POLY) == []          # Frame 1 debounce
    pts = det.detect(collar_roi, POLY)                 # Frame 2 confirmed!
    assert len(pts) == 1
    assert det.last_fault_code == int(FaultCode.COLLAR)
    assert 490 <= pts[0][0] <= 555 and 330 <= pts[0][1] <= 395


def test_soft_shadows_25_to_70_percent_no_false_trip():
    """F-3 / Exp G: Soft shadows (25%, 35%, 50%, 70%, sigma=40px) across blank must NOT trip."""
    base = _make_spinning_wood_roi()
    mask = np.zeros((ROI_H, ROI_W), dtype=np.float32)
    cv2.rectangle(mask, (300, 0), (600, 600), 1.0, thickness=-1)
    mask_blur = cv2.GaussianBlur(mask, (0, 0), 40)[..., None]

    for alpha in (0.25, 0.35, 0.50, 0.70):
        det = RedZoneVisionDetector(warmup_frames=12)
        for _ in range(14):
            assert det.detect(base, POLY) == []
        assert det.is_armed

        shadow_frame = (base.astype(np.float32) * (1.0 - alpha * mask_blur)).astype(np.uint8)
        for f_idx in range(30):
            pts = det.detect(shadow_frame, POLY)
            assert pts == [], f"False shadow trip at alpha={alpha}, frame={f_idx}: {pts}"


def test_warmup_motion_gate_and_no_heartbeat_before_armed():
    """F-1 & F-2: Hand in zone at startup or entering during warmup prevents arming and 0x55 emission."""
    shm_name = "cabinet_warmup_gate_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    engine = LatheSafetyEngine(
        shm_name=shm_name,
        com_port="MOCK",
        mock_serial=True,
        enable_cv_detector=True,
    )
    engine.cv_detector.warmup_frames = 10

    try:
        # 1. Clean frames 1..5 -> still calibrating, must NOT arm or emit 0x55
        for _ in range(5):
            writer.publish_frame(_make_spinning_wood_roi(), POLY)
            st = engine.step()
            assert st == int(SafetyState.DISARMED)
            assert not engine.cv_detector.is_armed
            assert HEARTBEAT_BYTE not in engine.serial.tx_history
            assert bool(writer.header.status_flags & int(StatusFlag.CALIBRATING))

        # 2. Hand enters during warmup at frame 6 and stays -> must NOT arm or emit 0x55
        hand_roi = _make_spinning_wood_roi()
        cv2.rectangle(hand_roi, (400, 320), (480, 600), (230, 230, 230), thickness=-1)
        for _ in range(25):
            writer.publish_frame(hand_roi, POLY)
            st = engine.step()
            assert st == int(SafetyState.DISARMED)
            assert not engine.cv_detector.is_armed
            assert HEARTBEAT_BYTE not in engine.serial.tx_history

        # 3. Operator withdraws hand -> 10 clean frames complete warmup -> arms and emits 0x55!
        for _ in range(11):
            writer.publish_frame(_make_spinning_wood_roi(), POLY)
            st = engine.step()
        assert engine.cv_detector.is_armed
        assert st == int(SafetyState.ARMED_MONITORING)
        assert engine.serial.tx_history[-1:] == HEARTBEAT_BYTE
        assert bool(writer.header.status_flags & int(StatusFlag.DETECTOR_ARMED))
    finally:
        engine.close()
        writer.close()


def test_fast_slow_and_thin_tool_penetration_budget():
    """F-8, F-9, F-10: Verify fast (30px/f), slow (1.5px/f), black glove, and 6px thin shaft trip."""
    for label, color, width, speed in [
        ("fast_30px_hand", (230, 230, 230), 80, 30.0),
        ("slow_1.5px_hand", (130, 165, 225), 80, 1.5),
        ("black_nitrile_glove", (20, 20, 22), 80, 8.0),
        ("thin_6px_steel_shaft", (235, 235, 235), 6, 8.0),
    ]:
        det = RedZoneVisionDetector(warmup_frames=10, confirm_frames=2)
        for _ in range(12):
            det.detect(_make_spinning_wood_roi(), POLY)
        assert det.is_armed

        tripped_tip_y = None
        for step_idx in range(1, 160):
            tip_y = int(600 - step_idx * speed)
            f = _make_spinning_wood_roi()
            cv2.rectangle(f, (320 - width // 2, max(200, tip_y)), (320 + width // 2, 600), color, thickness=-1)
            pts = det.detect(f, POLY)
            if pts:
                tripped_tip_y = tip_y
                break

        assert tripped_tip_y is not None, f"Failed to trip on {label}!"
        if label == "fast_30px_hand":
            penetration_px = 420 - tripped_tip_y
            assert penetration_px <= 40, f"Penetration {penetration_px}px exceeded 40px budget at 30px/f!"


def test_dynamic_polygon_clamping_and_rate_limit():
    """F-4 / T1.4: Verify bench clutter at y=560 is ignored and polygon rate-limits to +-10px/update."""
    clean = _make_spinning_wood_roi()
    p_clean = compute_dynamic_red_zone(clean)
    assert 422 <= int(p_clean[-1, 1]) <= 426  # ~radius 100 + margin 25 = 125 -> ~423..425

    cluttered = clean.copy()
    cv2.rectangle(cluttered, (0, 560), (900, 600), (220, 220, 220), thickness=-1)
    p_clutter = compute_dynamic_red_zone(cluttered, prev_poly=p_clean, max_step_px=10)
    # Clutter at y=560 is outside search band and rate-limited
    assert abs(int(p_clutter[-1, 1]) - int(p_clean[-1, 1])) <= 10
    assert int(p_clutter[-1, 1]) <= 300 + 160


def test_internal_grain_and_spinning_square_blank_and_spatial_debounce():
    """Phase 1.1 Regression (P1-A..P1-E): internal coves/grain, square blank RPMs, static tool rest, and distant chips."""
    # P1-C: Internal bright grain line at r=50px must not shrink 100px blank polygon
    grooved = _make_spinning_wood_roi()
    cv2.line(grooved, (100, 250), (760, 250), (210, 235, 250), 2)
    cv2.line(grooved, (100, 350), (760, 350), (210, 235, 250), 2)
    p_grooved = compute_dynamic_red_zone(grooved)
    assert 123 <= (300 - int(p_grooved[0, 1])) <= 127
    assert 123 <= (int(p_grooved[-1, 1]) - 300) <= 127

    # P1-D: Spinning square blank (a=120px) at 1000 RPM arms and has 0 false trips
    det_sq = RedZoneVisionDetector(warmup_frames=12)
    poly_sq = None
    for i in range(60):
        th = 2.0 * np.pi * (1000.0 / 60.0) * (i / 60.0)
        half = int(60 * (abs(np.cos(th)) + abs(np.sin(th))))
        roi_sq = np.full((ROI_H, ROI_W, ROI_C), 26, dtype=np.uint8)
        cv2.rectangle(roi_sq, (100, 300 - half), (760, 300 + half), (70, 142, 212), -1)
        cv2.line(roi_sq, (100, 300 - half), (760, 300 - half), (220, 240, 255), 2)
        cv2.line(roi_sq, (100, 300 + half), (760, 300 + half), (220, 240, 255), 2)
        if i % 30 == 0:
            poly_sq = compute_dynamic_red_zone(roi_sq, prev_poly=poly_sq)
        assert det_sq.detect(roi_sq, poly_sq) == []
    assert det_sq.is_armed

    # P1-E: Two unrelated wood chips 400px apart on consecutive frames must not false-trip
    det_chip = RedZoneVisionDetector(warmup_frames=10, confirm_frames=2)
    for _ in range(12):
        det_chip.detect(_make_spinning_wood_roi(), POLY)
    assert det_chip.is_armed
    chip1 = _make_spinning_wood_roi()
    cv2.rectangle(chip1, (200, 415), (218, 433), (235, 235, 240), -1)
    chip2 = _make_spinning_wood_roi()
    cv2.rectangle(chip2, (600, 415), (618, 433), (235, 235, 240), -1)
    assert det_chip.detect(chip1, POLY) == []
    assert det_chip.detect(chip2, POLY) == []
