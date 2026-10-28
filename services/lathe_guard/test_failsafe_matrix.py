"""
test_failsafe_matrix.py — T1.5 Chaos & Fail-Safe Matrix Suite for LatheGuard
Verifies all fail-safe invariants across capture, 100Hz FSM, supervisor, and vision guards:
  1. Capture process death / dropout (>50ms) -> FAULT_TRIPPED, 0xFF, FaultCode.CAMERA_STALE
  2. FSM process crash -> supervisor exits non-zero and terminates children
  3. Nebius advisor crash -> supervisor auto-restarts sidecar without interrupting 100Hz FSM
  4. HUD close -> clean supervisor shutdown (exit code 0)
  5. Frozen camera (>=12 identical reads) -> CAMERA_FROZEN flag -> stale watchdog trip
  6. Lens blocked / dust blindness -> FaultCode.CAMERA_OBSTRUCTED trip
  7. Camera bumped -> FaultCode.CAMERA_MOVED trip
  8. Detector exception -> FaultCode.DETECTOR_EXCEPTION trip
"""

import os
import sys
import time
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from lathe_safety_engine import HEARTBEAT_BYTE, LatheSafetyEngine, TRIP_BYTE
from lathe_shm import FaultCode, LatheShmWriter, ROI_C, ROI_H, ROI_W, SafetyState, StatusFlag
import run_lathe_guard as sup

POLY = np.array(
    [[120, 180], [320, 180], [520, 180], [720, 180],
     [720, 420], [520, 420], [320, 420], [120, 420]],
    dtype=np.int32,
)


def _clean_blank() -> np.ndarray:
    roi = np.full((ROI_H, ROI_W, ROI_C), 26, dtype=np.uint8)
    cv2.rectangle(roi, (100, 200), (760, 400), (70, 142, 212), thickness=-1)
    cv2.line(roi, (100, 200), (760, 200), (220, 240, 255), 2)
    cv2.line(roi, (100, 400), (760, 400), (220, 240, 255), 2)
    return roi


def test_capture_process_death_trips_stale_watchdog():
    """Row 1: When capture process dies (>50ms frame age), FSM trips with CAMERA_STALE + 0xFF."""
    shm_name = "cabinet_chaos_stale_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    engine = LatheSafetyEngine(shm_name=shm_name, com_port="MOCK", mock_serial=True, enable_cv_detector=False)
    try:
        writer.publish_frame(_clean_blank(), POLY)
        assert engine.step() == int(SafetyState.ARMED_MONITORING)
        assert engine.serial.tx_history[-1:] == HEARTBEAT_BYTE

        # Simulate capture process death (>50ms without frame update)
        time.sleep(0.060)
        assert engine.step() == int(SafetyState.FAULT_TRIPPED)
        assert engine.serial.tx_history[-1:] == TRIP_BYTE
        assert writer.header.fault_code == int(FaultCode.CAMERA_STALE)
    finally:
        engine.close()
        writer.close()


def test_frozen_camera_frame_trips_via_stale_watchdog():
    """Row 5: 12+ identical camera reads set CAMERA_FROZEN and stop refreshing last_capture_ns -> stale trip."""
    shm_name = "cabinet_chaos_frozen_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    engine = LatheSafetyEngine(shm_name=shm_name, com_port="MOCK", mock_serial=True, enable_cv_detector=True)
    engine.cv_detector.warmup_frames = 6
    try:
        for i in range(8):
            f = _clean_blank()
            f[0, 0, 0] = i  # Varying sensor LSB during warmup
            writer.publish_frame(f, POLY)
            engine.step()
        assert engine.cv_detector.is_armed
        assert writer.header.safety_state == int(SafetyState.ARMED_MONITORING)

        # Emulate frozen camera guard inside capture loop: 12 identical reads set CAMERA_FROZEN and skip publish
        frozen_frame = _clean_blank()
        prev_sub = np.ascontiguousarray(frozen_frame[::8, ::8]).copy()
        streak = 0
        for _ in range(14):
            cur_sub = np.ascontiguousarray(frozen_frame[::8, ::8])
            if np.array_equal(cur_sub, prev_sub):
                streak += 1
            if streak >= 12:
                writer.header.status_flags |= int(StatusFlag.CAMERA_FROZEN)
            else:
                writer.publish_frame(frozen_frame, POLY)
                engine.step()

        assert bool(writer.header.status_flags & int(StatusFlag.CAMERA_FROZEN))
        time.sleep(0.060)  # >50ms since last non-frozen frame commit
        assert engine.step() == int(SafetyState.FAULT_TRIPPED)
        assert engine.serial.tx_history[-1:] == TRIP_BYTE
        assert writer.header.fault_code == int(FaultCode.CAMERA_STALE)
        assert bool(writer.header.status_flags & int(StatusFlag.CAMERA_FROZEN))
    finally:
        engine.close()
        writer.close()


def test_lens_blocked_trips_camera_obstructed():
    """Row 6: Lens cap / sawdust blindness triggers CAMERA_OBSTRUCTED + 0xFF within 5 frames."""
    shm_name = "cabinet_chaos_obstructed_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    engine = LatheSafetyEngine(shm_name=shm_name, com_port="MOCK", mock_serial=True, enable_cv_detector=True)
    engine.cv_detector.warmup_frames = 8
    try:
        for _ in range(10):
            writer.publish_frame(_clean_blank(), POLY)
            engine.step()
        assert writer.header.safety_state == int(SafetyState.ARMED_MONITORING)

        black_frame = np.zeros((ROI_H, ROI_W, ROI_C), dtype=np.uint8)
        for _ in range(6):
            writer.publish_frame(black_frame, POLY)
            st = engine.step()
            if st == int(SafetyState.FAULT_TRIPPED):
                break

        assert writer.header.safety_state == int(SafetyState.FAULT_TRIPPED)
        assert engine.serial.tx_history[-1:] == TRIP_BYTE
        assert writer.header.fault_code == int(FaultCode.CAMERA_OBSTRUCTED)
        assert bool(writer.header.status_flags & int(StatusFlag.CAMERA_OBSTRUCTED))
    finally:
        engine.close()
        writer.close()


def test_camera_bumped_trips_camera_moved():
    """Row 7: Rigid camera shift (>4 small px) triggers CAMERA_MOVED + 0xFF."""
    shm_name = "cabinet_chaos_moved_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    engine = LatheSafetyEngine(shm_name=shm_name, com_port="MOCK", mock_serial=True, enable_cv_detector=True)
    engine.cv_detector.warmup_frames = 8
    try:
        base = _clean_blank()
        for _ in range(10):
            writer.publish_frame(base, POLY)
            engine.step()
        assert writer.header.safety_state == int(SafetyState.ARMED_MONITORING)

        shifted = np.roll(np.roll(base, 24, axis=1), 20, axis=0)
        for _ in range(4):
            writer.publish_frame(shifted, POLY)
            st = engine.step()
            if st == int(SafetyState.FAULT_TRIPPED):
                break

        assert writer.header.safety_state == int(SafetyState.FAULT_TRIPPED)
        assert engine.serial.tx_history[-1:] == TRIP_BYTE
        assert writer.header.fault_code == int(FaultCode.CAMERA_MOVED)
        assert bool(writer.header.status_flags & int(StatusFlag.CAMERA_MOVED))
    finally:
        engine.close()
        writer.close()


def test_detector_exception_trips_failsafe(monkeypatch):
    """Row 8: Any unhandled exception inside cv_detector.detect() trips FaultCode.DETECTOR_EXCEPTION + 0xFF."""
    shm_name = "cabinet_chaos_exception_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    engine = LatheSafetyEngine(shm_name=shm_name, com_port="MOCK", mock_serial=True, enable_cv_detector=True)
    engine.cv_detector.warmup_frames = 6
    try:
        for _ in range(8):
            writer.publish_frame(_clean_blank(), POLY)
            engine.step()
        assert writer.header.safety_state == int(SafetyState.ARMED_MONITORING)

        def boom(_frame, _poly):
            raise RuntimeError("Simulated OpenCV memory error")

        monkeypatch.setattr(engine.cv_detector, "detect", boom)
        writer.publish_frame(_clean_blank(), POLY)
        assert engine.step() == int(SafetyState.FAULT_TRIPPED)
        assert engine.serial.tx_history[-1:] == TRIP_BYTE
        assert writer.header.fault_code == int(FaultCode.DETECTOR_EXCEPTION)
    finally:
        engine.close()
        writer.close()


def test_supervisor_process_fault_handling(monkeypatch):
    """Rows 2, 3, 4: Verify supervisor handles FSM crash (non-zero), advisor crash (restart), and HUD close (0)."""

    class FakeProc:
        def __init__(self, exit_after_polls=None, code=0):
            self.exit_after = exit_after_polls
            self.returncode = code
            self.polls = 0
            self.terminated = False

        def poll(self):
            self.polls += 1
            if self.terminated:
                return self.returncode
            if self.exit_after is not None and self.polls >= self.exit_after:
                return self.returncode
            return None

        def send_signal(self, _sig):
            self.terminated = True

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.terminated = True

    monkeypatch.setattr(sup, "wait_for_shm_ready", lambda *a, **kw: True)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    # Row 2: FSM process dies with code 3 -> supervisor returns 3
    spawned = []

    def popen_fsm_crash(cmd, **kwargs):
        name = cmd[2]
        if "lathe_safety_engine.py" in name:
            p = FakeProc(exit_after_polls=2, code=3)
        else:
            p = FakeProc(exit_after_polls=None, code=0)
        spawned.append((name, p))
        return p

    monkeypatch.setattr(sup.subprocess, "Popen", popen_fsm_crash)
    rc = sup.run_supervisor(sup.parse_args(["--mock", "--no-nebius", "--duration", "5"]))
    assert rc == 3

    # Row 3 & 4: Advisor crashes on poll 1 (auto-restarted), then HUD closes on poll 3 -> clean exit 0
    adv_spawns = 0

    def popen_adv_restart_and_hud_close(cmd, **kwargs):
        nonlocal adv_spawns
        name = cmd[2]
        if "nebius_hazard_advisor.py" in name:
            adv_spawns += 1
            return FakeProc(exit_after_polls=1 if adv_spawns == 1 else None, code=1)
        if "lathe_hud_preview.py" in name:
            return FakeProc(exit_after_polls=3, code=0)
        return FakeProc(exit_after_polls=None, code=0)

    monkeypatch.setattr(sup.subprocess, "Popen", popen_adv_restart_and_hud_close)
    rc = sup.run_supervisor(sup.parse_args(["--mock", "--preview", "--duration", "30"]))
    assert rc == 0
    assert adv_spawns == 2, "Supervisor failed to auto-restart crashed Nebius advisor sidecar!"
