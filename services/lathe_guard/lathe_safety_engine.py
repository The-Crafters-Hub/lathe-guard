"""
lathe_safety_engine.py — 100Hz Deadman Heartbeat Serial Worker & FSM Engine
The Crafters Hub | LatheGuard Physical AI — Nebius × NVIDIA Hackathon Submission
Author: Hosam (Co-Founder, The Crafters Hub)
"""

import argparse
import os
import time
from typing import List, Optional, Tuple
import cv2
import numpy as np
try:
    import serial
except ImportError:
    serial = None  # Graceful fallback on headless judge machines without pyserial
from lathe_profile import load_lathe_profile
from lathe_scheduler import MASK_SAFETY_CORES, win32_realtime_context
from lathe_shm import (
    DEFAULT_SHM_NAME,
    FaultCode,
    LatheShmReader,
    ROI_H,
    ROI_W,
    SafetyState,
    StatusFlag,
)

HEARTBEAT_BYTE = b"\x55"
TRIP_BYTE = b"\xFF"
STALE_FRAME_TIMEOUT_NS = 50_000_000  # 50.0 ms max camera frame age

SCALE = 4
SMALL_W, SMALL_H = ROI_W // SCALE, ROI_H // SCALE  # 225x150 (<1.0ms zero-copy strided vision pass)
SMALL_AXIS_Y = SMALL_H // 2                         # 75


class MockSerialWatchdog:
    """In-memory loopback serial port for testing without physical COM hardware."""

    def __init__(self):
        self.tx_history = bytearray()
        self.is_open = True

    def write(self, data: bytes):
        self.tx_history.extend(data)

    def close(self):
        self.is_open = False


class RedZoneVisionDetector:
    """
    Sub-millisecond CPU-only OpenCV intrusion detector v2 for the 100Hz safety loop.

    Features:
      1. Warm-up Motion & Static-Obstruction Gate (F-1 / F-2): Refuses to arm if a hand
         or tool is already bridging the moat or moves into the zone during calibration.
      2. Per-Pixel Adaptive Threshold + Moat-Contact Rule (F-3): Mean + running variance
         background model; requires outer moat crossing (`in_moat >= 4`) and lateral
         structural edge, rejecting soft shadows (25-70%) while catching 4-6px thin steel.
      3. CraftTwin Bicolor Collar Fast-Path: Flags Cyan/Magenta tool-collar markers.
      4. Camera Integrity Guards: Detects lens obstruction/dust blindness (`CAMERA_OBSTRUCTED`)
         and rigid camera bumps (`CAMERA_MOVED` via `phaseCorrelate`).
      5. 2-Frame Temporal Debounce: Rejects single-frame flying wood shavings/sawdust.
    """

    def __init__(
        self,
        warmup_frames: int = 18,
        diff_thresh: int = 24,
        min_area_small: int = 10,
        confirm_frames: int = 2,
        max_warmup_s: float = 5.0,
        axis_y: Optional[int] = None,
    ):
        self.warmup_frames = warmup_frames
        self.diff_thresh = diff_thresh
        self.min_area_small = min_area_small
        self.confirm_frames = confirm_frames
        self.max_warmup_s = max_warmup_s
        if axis_y is None:
            prof = load_lathe_profile()
            axis_y = int(prof.get("axis_y", 300))
        self.small_axis_y = max(1, int(axis_y) // SCALE)

        self._mean: Optional[np.ndarray] = None
        self._sq_mean: Optional[np.ndarray] = None
        self._std: np.ndarray = np.full((SMALL_H, SMALL_W), 3.0, dtype=np.float32)
        self._init_ref: Optional[np.ndarray] = None
        self._baseline_ref: Optional[np.ndarray] = None
        self._hann: np.ndarray = cv2.createHanningWindow((SMALL_W, SMALL_H), cv2.CV_32F)
        self._baseline_luma: float = 0.0
        self._baseline_lap_var: float = 0.0

        self._warmup_start_t: Optional[float] = None
        self.warmup_timed_out: bool = False
        self._frames_seen: int = 0
        self._warmup_restarts: int = 0
        self._consecutive_hits: int = 0
        self._last_candidate_pt: Optional[Tuple[int, int]] = None
        self._moved_hits: int = 0
        self._obstructed_hits: int = 0

        self.is_armed: bool = False
        self.status_flags: int = int(StatusFlag.CALIBRATING)
        self.last_fault_code: int = int(FaultCode.NONE)

        self._cached_poly_bytes: Optional[bytes] = None
        self._mask_red = np.zeros((SMALL_H, SMALL_W), dtype=np.uint8)
        self._mask_moat = np.zeros((SMALL_H, SMALL_W), dtype=np.uint8)
        self._mask_tb_outer = np.zeros((SMALL_H, SMALL_W), dtype=np.uint8)

        self._k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self._k_out = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        self._k_in = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19, 19))
        self._k_v_inner = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 7))
        self._k_v_outer = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 19))

    def _update_polygon_masks(self, poly: np.ndarray) -> None:
        if len(poly) < 3:
            return
        poly_s = (poly // SCALE).astype(np.int32)
        key = poly_s.tobytes()
        if key == self._cached_poly_bytes:
            return
        self._cached_poly_bytes = key
        self._mask_red.fill(0)
        cv2.fillPoly(self._mask_red, [poly_s], 255)
        dil = cv2.dilate(self._mask_red, self._k_out)
        ero = cv2.erode(self._mask_red, self._k_in)
        cv2.subtract(dil, ero, dst=self._mask_moat)
        v_inner = cv2.dilate(self._mask_red, self._k_v_inner)
        v_outer = cv2.dilate(self._mask_red, self._k_v_outer)
        cv2.subtract(v_outer, v_inner, dst=self._mask_tb_outer)

    def _has_cross_moat_obstruction(self, gray_f: np.ndarray) -> bool:
        """Detects a moving object bridging the outer approach band relative to initial reference."""
        if self._init_ref is None:
            return False
        diff_init = cv2.absdiff(gray_f, self._init_ref)
        return float(cv2.mean(diff_init, mask=self._mask_tb_outer)[0]) > 5.0

    def _is_rigid_camera_shift(self, gray_f: np.ndarray) -> bool:
        """Verifies via phaseCorrelate + affine residual whether the camera shifted > 4 small px."""
        if self._baseline_ref is None:
            return False
        # Note: Do NOT pass window to cv2.phaseCorrelate as OpenCV 4.x multiplies inputs in-place
        (dx, dy), _ = cv2.phaseCorrelate(self._baseline_ref, gray_f)
        if (dx * dx + dy * dy) <= 16.0:  # <= 4 small px (~16 full-res px)
            return False
        m_aff = np.float32([[1.0, 0.0, dx], [0.0, 1.0, dy]])
        shifted = cv2.warpAffine(self._baseline_ref, m_aff, (SMALL_W, SMALL_H), borderMode=cv2.BORDER_REPLICATE)
        err_unshifted = float(cv2.mean(cv2.absdiff(self._baseline_ref, gray_f))[0])
        err_shifted = float(cv2.mean(cv2.absdiff(shifted, gray_f))[0])
        return err_unshifted > 2.0 and err_shifted < 0.65 * err_unshifted

    def detect(self, frame_roi: np.ndarray, poly: np.ndarray) -> List[Tuple[int, int]]:
        """Return [(x, y)] in 900x600 ROI coordinates if a confirmed breach or camera fault is present."""
        if len(poly) < 3:
            return []

        self._update_polygon_masks(poly)
        small = np.ascontiguousarray(frame_roi[::SCALE, ::SCALE])
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        gray_f = gray.astype(np.float32)

        if self._warmup_start_t is None:
            self._warmup_start_t = time.perf_counter()

        # 1. Baseline calibration with Warm-up Motion & Outer-Approach Gate (F-1 / F-2)
        if self._mean is None:
            self._mean = gray_f.copy()
            self._sq_mean = cv2.multiply(gray_f, gray_f) + 9.0
            self._init_ref = gray_f.copy()
            self._frames_seen = 1
            self.is_armed = False
            self.status_flags = int(StatusFlag.CALIBRATING)
            return []

        if not self.is_armed:
            obstructed_moat = self._has_cross_moat_obstruction(gray_f)
            step_diff = cv2.absdiff(gray_f, self._mean)
            moat_diff = cv2.mean(step_diff, mask=self._mask_tb_outer)[0]
            if moat_diff > 5.0 or obstructed_moat:
                if not obstructed_moat:
                    self._mean = gray_f.copy()
                    self._sq_mean = cv2.multiply(gray_f, gray_f) + 9.0
                    self._init_ref = gray_f.copy()
                self._frames_seen = 1
                self._warmup_restarts += 1
                self.status_flags = int(StatusFlag.CALIBRATING)
                if (time.perf_counter() - self._warmup_start_t) > self.max_warmup_s:
                    self.warmup_timed_out = True
                return []

            cv2.accumulateWeighted(gray_f, self._mean, 0.22)
            cv2.accumulateWeighted(cv2.multiply(gray_f, gray_f), self._sq_mean, 0.22)
            self._frames_seen += 1

            if self._frames_seen >= self.warmup_frames:
                var = cv2.subtract(self._sq_mean, cv2.multiply(self._mean, self._mean))
                cv2.max(var, 4.0, dst=var)
                cv2.sqrt(var, dst=self._std)
                self.is_armed = True
                self.warmup_timed_out = False
                self.status_flags = int(StatusFlag.DETECTOR_ARMED)
                self._baseline_ref = self._mean.copy()
                self._baseline_luma = max(1.0, float(cv2.mean(self._mean)[0]))
                lap = cv2.Laplacian(self._mean, cv2.CV_32F)
                _, std_lap = cv2.meanStdDev(lap)
                self._baseline_lap_var = max(1.0, float(std_lap[0, 0] ** 2))
                print("[VISION GUARD] Baseline calibrated — live hand & chisel Red-Zone detector ARMED.")
            return []

        self._frames_seen += 1

        # T1.2-5 Obstruction Guard (lens cap / heavy dust cloud)
        cur_luma = float(cv2.mean(gray_f)[0])
        luma_ratio = cur_luma / self._baseline_luma
        lap = cv2.Laplacian(gray_f, cv2.CV_32F)
        _, std_lap = cv2.meanStdDev(lap)
        lap_var = float(std_lap[0, 0] ** 2)
        if luma_ratio < 0.40 or luma_ratio > 2.50 or lap_var < 0.15 * self._baseline_lap_var:
            self._obstructed_hits += 1
            if self._obstructed_hits >= 5:
                self.status_flags |= int(StatusFlag.CAMERA_OBSTRUCTED)
                self.last_fault_code = int(FaultCode.CAMERA_OBSTRUCTED)
                return [(ROI_W // 2, ROI_H // 2)]
        else:
            self._obstructed_hits = 0

        # 2. Compute per-pixel foreground change mask (shared by Guards, Channel B & Channel A)
        signed_diff = cv2.subtract(gray_f, self._mean)
        abs_diff = cv2.absdiff(gray_f, self._mean)
        thr = np.clip(self._std * 3.5, float(self.diff_thresh), 80.0)
        fg_raw = cv2.compare(abs_diff, thr, cv2.CMP_GT)

        # T1.2-4 Camera-Moved Guard (every 15 frames, only when global diff > 2.0)
        if self._frames_seen % 15 == 0:
            if float(cv2.mean(abs_diff)[0]) > 2.0 and self._is_rigid_camera_shift(gray_f):
                self._moved_hits += 1
                if self._moved_hits >= 3:
                    self.status_flags |= int(StatusFlag.CAMERA_MOVED)
                    self.last_fault_code = int(FaultCode.CAMERA_MOVED)
                    return [(ROI_W // 2, ROI_H // 2)]
            else:
                self._moved_hits = 0

        candidate_pt: Optional[Tuple[int, int]] = None
        candidate_fault: int = int(FaultCode.NONE)

        # 3. Fast-path Channel B: CraftTwin Cyan/Magenta collar marker moving inside Red-Zone
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        cyan = cv2.inRange(hsv, (82, 115, 70), (105, 255, 255))
        magenta = cv2.inRange(hsv, (140, 115, 70), (174, 255, 255))
        collar_fg = cv2.compare(abs_diff, 11.0, cv2.CMP_GT)
        collar_red = cv2.bitwise_and(cv2.bitwise_or(cyan, magenta), self._mask_red)
        collar_red = cv2.bitwise_and(collar_red, collar_fg)
        if cv2.countNonZero(collar_red) >= 14:
            m = cv2.moments(collar_red, binaryImage=True)
            if m["m00"] > 0:
                cx = int(m["m10"] / m["m00"]) * SCALE
                cy = int(m["m01"] / m["m00"]) * SCALE
                candidate_pt = (cx, cy)
                candidate_fault = int(FaultCode.COLLAR)

        # 4. Primary Channel A: Per-pixel adaptive threshold + moat-contact rule + thin-steel preservation
        if candidate_pt is None:
            grad_x = np.abs(cv2.Sobel(signed_diff, cv2.CV_32F, 1, 0, ksize=3))
            sharp_edge = cv2.bitwise_and(
                cv2.compare(grad_x, 48.0, cv2.CMP_GT),
                cv2.compare(abs_diff, float(self.diff_thresh) * 0.85, cv2.CMP_GT),
            )
            fg_open = cv2.morphologyEx(fg_raw, cv2.MORPH_OPEN, self._k3)
            fg = cv2.bitwise_or(fg_open, sharp_edge)
            fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, self._k5)

            contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            best_axis_dist = 10_000
            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                sub_fg = fg[y : y + h, x : x + w]
                in_red = cv2.countNonZero(cv2.bitwise_and(sub_fg, self._mask_red[y : y + h, x : x + w]))
                if in_red < self.min_area_small:
                    continue
                in_moat = cv2.countNonZero(cv2.bitwise_and(sub_fg, self._mask_moat[y : y + h, x : x + w]))
                if in_moat < 4:
                    continue

                # Shadow vs physical object discriminator: require sharp lateral edge inside contour
                sub_sharp = sharp_edge[y : y + h, x : x + w]
                if cv2.countNonZero(cv2.bitwise_and(sub_sharp, sub_fg)) < 2:
                    continue

                pts = cnt.reshape(-1, 2)
                for px, py in pts:
                    if 0 <= py < SMALL_H and 0 <= px < SMALL_W and self._mask_red[py, px] > 0:
                        d_axis = abs(int(py) - self.small_axis_y)
                        if d_axis < best_axis_dist:
                            best_axis_dist = d_axis
                            candidate_pt = (int(px) * SCALE, int(py) * SCALE)
                            candidate_fault = int(FaultCode.HAND_OR_TOOL_MOAT)

        # 5. Spatial-consistent 2-frame debounce (also checks if candidate contour was a rigid camera bump)
        if candidate_pt is not None:
            if candidate_fault == int(FaultCode.HAND_OR_TOOL_MOAT) and self._is_rigid_camera_shift(gray_f):
                candidate_fault = int(FaultCode.CAMERA_MOVED)
                self.status_flags |= int(StatusFlag.CAMERA_MOVED)
            if self._last_candidate_pt is not None:
                dx_pt = candidate_pt[0] - self._last_candidate_pt[0]
                dy_pt = candidate_pt[1] - self._last_candidate_pt[1]
                if (dx_pt * dx_pt + dy_pt * dy_pt) <= 96 * 96:
                    self._consecutive_hits += 1
                else:
                    self._consecutive_hits = 1
            else:
                self._consecutive_hits = 1
            self._last_candidate_pt = candidate_pt
            if self._consecutive_hits >= self.confirm_frames:
                self.last_fault_code = candidate_fault
                return [candidate_pt]
            return []

        self._consecutive_hits = 0
        self._last_candidate_pt = None
        cv2.accumulateWeighted(gray_f, self._mean, 0.015)
        if self._frames_seen % 2 == 0:
            cv2.accumulateWeighted(cv2.multiply(gray_f, gray_f), self._sq_mean, 0.03)
            var = cv2.subtract(self._sq_mean, cv2.multiply(self._mean, self._mean))
            cv2.max(var, 4.0, dst=var)
            cv2.sqrt(var, dst=self._std)
        return []


class LatheSafetyEngine:
    """4-State Deterministic Safety FSM + 100Hz Deadman Serial Heartbeat Worker."""

    def __init__(
        self,
        shm_name: str,
        com_port: str,
        mock_serial: bool = False,
        enable_cv_detector: Optional[bool] = None,
        profile_path: Optional[str] = None,
    ):
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"  # Strictly isolated from NVIDIA GPU
        self.reader = LatheShmReader(shm_name=shm_name)
        self.mock_serial = mock_serial
        self.latency_ring_us = [0] * 100
        self.lat_idx = 0
        self.simulated_fingertips: List[Tuple[int, int]] = []
        self.profile = load_lathe_profile(profile_path)
        axis_y = int(self.profile.get("axis_y", 300))

        # Enable live OpenCV hand/chisel detector automatically on real hardware, or when requested
        use_cv = (not mock_serial) if enable_cv_detector is None else enable_cv_detector
        self.cv_detector: Optional[RedZoneVisionDetector] = RedZoneVisionDetector(axis_y=axis_y) if use_cv else None

        if mock_serial:
            self.serial = MockSerialWatchdog()
        else:
            if serial is None:
                print("[CRITICAL] pyserial not installed; cannot open physical COM port.")
                self.serial = None
                self._trip_emergency_brake("pyserial missing", fault_code=int(FaultCode.SERIAL_FAULT))
                return
            try:
                self.serial = serial.Serial(com_port, baudrate=921600, timeout=0, write_timeout=0)
            except Exception as e:
                print(f"[CRITICAL] Serial port {com_port} failed to open: {e}")
                self.serial = None
                self._trip_emergency_brake(f"Serial open failed: {e}", fault_code=int(FaultCode.SERIAL_FAULT))

    def _sync_status_flags(self) -> None:
        capture_flags = int(self.reader.header.status_flags) & int(StatusFlag.CAMERA_FROZEN)
        det_flags = self.cv_detector.status_flags if self.cv_detector is not None else int(StatusFlag.DETECTOR_ARMED)
        self.reader.header.status_flags = capture_flags | int(det_flags)

    def _trip_emergency_brake(
        self,
        reason: str,
        breach_pt: Optional[Tuple[int, int]] = None,
        fault_code: int = int(FaultCode.NONE),
    ):
        prev_state = self.reader.header.safety_state
        if breach_pt is not None:
            self.reader.header.intrusion_x = int(breach_pt[0])
            self.reader.header.intrusion_y = int(breach_pt[1])
        if fault_code != int(FaultCode.NONE):
            self.reader.header.fault_code = int(fault_code)
        self._sync_status_flags()
        self.reader.header.safety_state = int(SafetyState.FAULT_TRIPPED)
        if self.serial is not None:
            try:
                self.serial.write(TRIP_BYTE)
            except Exception:
                pass
        if prev_state != int(SafetyState.FAULT_TRIPPED):
            print(f"[EMERGENCY STOP] State -> FAULT_TRIPPED | Code={FaultCode(self.reader.header.fault_code).name} | Reason: {reason}")

    def step(self) -> int:
        """Executes a single deterministic safety evaluation cycle."""
        t0_ns = time.perf_counter_ns()
        current_state = SafetyState(self.reader.header.safety_state)

        # Latching Fault State: Never self-clear without external reset
        if current_state == SafetyState.FAULT_TRIPPED:
            if self.serial:
                self.serial.write(TRIP_BYTE)
            return int(SafetyState.FAULT_TRIPPED)

        # 1. Stale Frame / Camera Dropout Watchdog (>50ms)
        last_cap_ns = self.reader.header.last_capture_ns
        if last_cap_ns > 0 and (t0_ns - last_cap_ns) > STALE_FRAME_TIMEOUT_NS:
            self._trip_emergency_brake(
                f"Camera frame stale ({(t0_ns - last_cap_ns)/1e6:.1f} ms > 50 ms)",
                fault_code=int(FaultCode.CAMERA_STALE),
            )
            return int(SafetyState.FAULT_TRIPPED)

        # 2. Seqlock Non-Blocking Read
        latest = self.reader.read_latest(copy_frame=False)
        if latest is not None:
            frame_roi, poly, _, _ = latest

            # 3. Live Hand / Chisel Vision Intrusion (wrapped in T1.2-6 exception fail-safe)
            detected_pts: List[Tuple[int, int]] = []
            if self.cv_detector is not None:
                try:
                    detected_pts = self.cv_detector.detect(frame_roi, poly)
                except Exception as e:
                    self._trip_emergency_brake(
                        f"Vision detector exception: {e}",
                        fault_code=int(FaultCode.DETECTOR_EXCEPTION),
                    )
                    return int(SafetyState.FAULT_TRIPPED)

            self._sync_status_flags()

            # Check camera integrity faults (obstructed / moved) or spatial Red-Zone breaches
            for pt in detected_pts:
                f_code = self.cv_detector.last_fault_code if self.cv_detector is not None else int(FaultCode.HAND_OR_TOOL_MOAT)
                if f_code in (int(FaultCode.CAMERA_OBSTRUCTED), int(FaultCode.CAMERA_MOVED)):
                    self._trip_emergency_brake(
                        f"Camera integrity fault ({FaultCode(f_code).name})",
                        breach_pt=pt,
                        fault_code=f_code,
                    )
                    return int(SafetyState.FAULT_TRIPPED)
                dist = cv2.pointPolygonTest(poly, (float(pt[0]), float(pt[1])), measureDist=True)
                if dist >= -4.0:
                    self._trip_emergency_brake(
                        f"Red-Zone Breach at pixel {pt} (dist={dist:.1f}px)",
                        breach_pt=pt,
                        fault_code=f_code,
                    )
                    return int(SafetyState.FAULT_TRIPPED)

            for pt in self.simulated_fingertips:
                dist = cv2.pointPolygonTest(poly, (float(pt[0]), float(pt[1])), measureDist=True)
                if dist >= -4.0:
                    self._trip_emergency_brake(
                        f"Simulated Red-Zone Breach at pixel {pt} (dist={dist:.1f}px)",
                        breach_pt=pt,
                        fault_code=int(FaultCode.SIMULATED),
                    )
                    return int(SafetyState.FAULT_TRIPPED)

            # T1.3 Arming Gate (F-2): Transition DISARMED -> ARMED_MONITORING ONLY when detector is armed
            detector_ready = (self.cv_detector is None) or self.cv_detector.is_armed
            if current_state == SafetyState.DISARMED and detector_ready:
                self.reader.header.safety_state = int(SafetyState.ARMED_MONITORING)
                print("[SAFETY FSM] State -> ARMED_MONITORING (100Hz 0x55 Heartbeat Active)")

        # 4. Emit 100Hz Keep-Alive Pulse (0x55) ONLY if Armed (or Degraded) AND detector is armed
        active_state = SafetyState(self.reader.header.safety_state)
        detector_ready = (self.cv_detector is None) or self.cv_detector.is_armed
        if detector_ready and active_state in (SafetyState.ARMED_MONITORING, SafetyState.DEGRADED_PEDAGOGY):
            if self.serial is not None:
                try:
                    self.serial.write(HEARTBEAT_BYTE)
                except Exception as e:
                    self._trip_emergency_brake(
                        f"Serial Write Fault: {e}",
                        fault_code=int(FaultCode.SERIAL_FAULT),
                    )
                    return int(SafetyState.FAULT_TRIPPED)

        # 5. Update Rolling P99 Telemetry in Shared Memory Header
        elapsed_us = int((time.perf_counter_ns() - t0_ns) // 1000)
        self.latency_ring_us[self.lat_idx] = elapsed_us
        self.lat_idx = (self.lat_idx + 1) % 100
        self.reader.header.p99_latency_us = sorted(self.latency_ring_us)[98]
        self.reader.header.last_safety_ns = time.perf_counter_ns()

        return int(self.reader.header.safety_state)

    def close(self):
        if self.serial is not None:
            try:
                self.serial.write(TRIP_BYTE)
                self.serial.close()
            except Exception:
                pass
        self.reader.close()


def run_self_test():
    """Standalone integration test verifying FSM transitions, 100Hz heartbeat, and red-zone breach."""
    from lathe_shm import LatheShmWriter, ROI_C, ROI_H, ROI_W

    test_shm = "cabinet_safety_fsm_test"
    writer = LatheShmWriter(shm_name=test_shm, create=True)
    engine = LatheSafetyEngine(shm_name=test_shm, com_port="MOCK", mock_serial=True)

    dummy_frame = np.zeros((ROI_H, ROI_W, ROI_C), dtype=np.uint8)
    poly = np.array([[200, 200], [700, 200], [700, 400], [200, 400]], dtype=np.int32)

    with win32_realtime_context(affinity_mask=MASK_SAFETY_CORES, time_critical=True):
        # 1. Publish valid frame & verify transition DISARMED -> ARMED_MONITORING
        writer.publish_frame(dummy_frame, poly)
        state = engine.step()
        assert state == int(SafetyState.ARMED_MONITORING), "Failed to arm on valid frame!"
        assert engine.serial.tx_history[-1:] == HEARTBEAT_BYTE, "Missing 0x55 heartbeat byte!"

        # 2. Simulate Pedagogical GPU OOM -> verify safety engine keeps pulsing 0x55 in DEGRADED_PEDAGOGY
        writer.header.safety_state = int(SafetyState.DEGRADED_PEDAGOGY)
        writer.publish_frame(dummy_frame, poly)
        state = engine.step()
        assert state == int(SafetyState.DEGRADED_PEDAGOGY)
        assert engine.serial.tx_history[-1:] == HEARTBEAT_BYTE

        # 3. Inject Fingertip inside Red-Zone Polygon (450, 300) -> verify instant 0xFF trip
        engine.simulated_fingertips = [(450, 300)]
        writer.publish_frame(dummy_frame, poly)
        state = engine.step()
        assert state == int(SafetyState.FAULT_TRIPPED), "Failed to trip on red-zone breach!"
        assert engine.serial.tx_history[-1:] == TRIP_BYTE, "Failed to transmit 0xFF trip byte!"

    p99_us = writer.header.p99_latency_us
    engine.close()
    writer.close()
    print(f"[PASS] Module 4 Safety FSM Verified | P99 Evaluation Latency: {p99_us} us")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="Run standalone FSM verification suite")
    parser.add_argument("--com", default="COM3", help="Serial COM port for Arduino watchdog")
    parser.add_argument("--shm", default=DEFAULT_SHM_NAME, help="Seqlock shared memory segment name")
    parser.add_argument("--mock-serial", action="store_true", help="Use in-memory loopback serial watchdog")
    parser.add_argument(
        "--cv-detect",
        action="store_true",
        help="Force-enable live OpenCV hand/chisel intrusion detector even when --mock-serial is set",
    )
    parser.add_argument(
        "--simulate-breach-after",
        type=float,
        default=0.0,
        help="Inject a synthetic Red-Zone fingertip breach after N seconds (>0 for bench/judge demo)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional path to calibrated lathe JSON profile (overrides LATHEGUARD_PROFILE)",
    )
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
    else:
        use_cv = True if args.cv_detect else (not args.mock_serial)
        eng = LatheSafetyEngine(
            shm_name=args.shm,
            com_port=args.com,
            mock_serial=args.mock_serial,
            enable_cv_detector=use_cv,
            profile_path=args.profile,
        )
        t_start = time.perf_counter()
        breach_injected = False
        with win32_realtime_context(affinity_mask=MASK_SAFETY_CORES, time_critical=True):
            try:
                while True:
                    if (
                        args.simulate_breach_after > 0.0
                        and not breach_injected
                        and (time.perf_counter() - t_start) >= args.simulate_breach_after
                    ):
                        eng.simulated_fingertips = [(450, 300)]
                        breach_injected = True
                        print("[DEMO SIM] Injecting synthetic fingertip inside Red-Zone at (450, 300)...")
                    eng.step()
                    time.sleep(0.008)  # ~125 Hz evaluation & heartbeat cadence
            except KeyboardInterrupt:
                eng.close()
