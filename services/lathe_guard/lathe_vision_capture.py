"""
lathe_vision_capture.py — High-Speed Camera Capture & Dynamic Red-Zone Profiler
The Crafters Hub | The Cabinet AI Automation
Supports:
  - Android Phones via RTSP / HTTP (IP Webcam, RTSP Camera app)
  - Android Phones via USB Direct (DroidCam, Iriun Webcam, USB Camera Index 0, 1, 2)
  - Synthetic Mock Mode (--mock) for zero-hardware bench testing
"""

import argparse
import os
import sys
import time
from typing import List, Optional, Tuple
import cv2
import numpy as np
from lathe_profile import load_lathe_profile
from lathe_scheduler import MASK_CAPTURE_CORES, win32_realtime_context
from lathe_shm import DEFAULT_SHM_NAME, LatheShmWriter, ROI_C, ROI_H, ROI_W, StatusFlag

# Default calibrated spindle centerline in the 600x900 ROI
SPINDLE_AXIS_Y = 300
X_STATIONS = [120, 320, 520, 720]
CAMERA_FAULT_MASK = int(
    StatusFlag.CAMERA_FROZEN | StatusFlag.CAMERA_OBSTRUCTED | StatusFlag.CAMERA_MOVED
)


def compute_dynamic_red_zone(
    roi_bgr: np.ndarray,
    safety_margin_px: int = 25,
    radius_clamp: Tuple[int, int] = (60, 160),
    prev_poly: Optional[np.ndarray] = None,
    max_step_px: int = 10,
    axis_y: Optional[int] = None,
    x_stations: Optional[List[int]] = None,
) -> np.ndarray:
    """
    Profiles the vertical Sobel edge envelope perpendicular to SPINDLE_AXIS_Y across
    4 axial stations to measure real-time turning blank radius and pinch boundaries.
    Uses nearest outward silhouette edge, clamps radius to `radius_clamp` ([60, 160]),
    and rate-limits boundary changes to `+-max_step_px` per update (F-4).
    Returns an 8-vertex closed polygon (shape: 8x2, dtype: int32).
    """
    ax_y = SPINDLE_AXIS_Y if axis_y is None else int(axis_y)
    stations = X_STATIONS if x_stations is None else [int(s) for s in x_stations]

    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    grad_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
    abs_grad = cv2.convertScaleAbs(grad_y)

    r_min, r_max = int(radius_clamp[0]), int(radius_clamp[1])
    min_core_r = 30
    max_search_r = max(min_core_r + 10, r_max - safety_margin_px + 15)

    top_pts = []
    bot_pts = []

    for x in stations:
        strip = abs_grad[:, max(0, x - 6):min(ROI_W, x + 6)].mean(axis=1)
        top_band_start = max(0, ax_y - max_search_r)
        top_band_end = max(0, ax_y - min_core_r)
        bot_band_start = min(ROI_H, ax_y + min_core_r)
        bot_band_end = min(ROI_H, ax_y + max_search_r)

        above_edges = np.where(strip[top_band_start:top_band_end] > 40)[0]
        below_edges = np.where(strip[bot_band_start:bot_band_end] > 40)[0]

        # Outermost strong silhouette edge within clamped search band (compensated 2px for Sobel/stroke width)
        r_top = max(0, (ax_y - (top_band_start + int(above_edges[0]))) - 2) if len(above_edges) > 0 else 75
        r_bot = max(0, (min_core_r + int(below_edges[-1])) - 2) if len(below_edges) > 0 else 75

        raw_radius = max(r_top, r_bot) + safety_margin_px
        radius = int(np.clip(raw_radius, r_min, r_max))

        top_pts.append([x, max(10, ax_y - radius)])
        bot_pts.append([x, min(ROI_H - 10, ax_y + radius)])

    new_poly = np.array(top_pts + bot_pts[::-1], dtype=np.int32)
    if prev_poly is not None and prev_poly.shape == new_poly.shape and max_step_px > 0:
        dy = np.clip(new_poly[:, 1] - prev_poly[:, 1], -max_step_px, max_step_px)
        new_poly[:, 1] = prev_poly[:, 1] + dy

    return new_poly


def generate_mock_lathe_frame(frame_idx: int, inject_intrusion: bool = False, static_blank: bool = False) -> np.ndarray:
    """Generates a synthetic 1920x1080 lathe frame with a rotating blank (and optional hand/chisel breach)."""
    full_frame = np.full((1080, 1920, 3), 24, dtype=np.uint8)
    simulated_radius = 100 if static_blank else int(100 + 40 * np.sin(frame_idx * 0.05))
    center_y = 240 + SPINDLE_AXIS_Y  # 540 in full frame

    # Draw simulated workpiece
    cv2.rectangle(
        full_frame,
        (510 + 100, center_y - simulated_radius),
        (510 + 760, center_y + simulated_radius),
        (70, 140, 210),
        thickness=-1,
    )
    # Add simulated wood grain / motion lines for Sobel gradient detection
    cv2.line(
        full_frame,
        (610, center_y - simulated_radius),
        (1270, center_y - simulated_radius),
        (220, 240, 255),
        3,
    )
    cv2.line(
        full_frame,
        (610, center_y + simulated_radius),
        (1270, center_y + simulated_radius),
        (220, 240, 255),
        3,
    )

    if inject_intrusion:
        # Draw steel gouge + operator hand entering from bottom tool-rest edge across Red-Zone moat
        # Full-frame ROI offset is x+510, y+240 -> targets ROI x~450, y~340..590
        cv2.rectangle(full_frame, (510 + 432, 240 + 330), (510 + 468, 240 + 595), (210, 215, 220), thickness=-1)
        cv2.circle(full_frame, (510 + 450, 240 + 470), 34, (130, 165, 225), thickness=-1)

    return full_frame


def open_camera(source: str):
    """
    Opens camera from:
      - Device index (e.g. '0', '1' for USB DroidCam / Iriun / Webcams)
      - Local video file path (e.g. '.mp4', '.avi' for deterministic judge replay)
      - RTSP URL (e.g. 'rtsp://192.168.1.50:8080/h264_pcm.sdp')
      - HTTP MJPEG URL (e.g. 'http://192.168.1.50:8080/video')
    """
    if source.isdigit():
        dev_id = int(source)
        print(f"[*] Opening local USB camera / virtual device index {dev_id}...")
        cap = cv2.VideoCapture(dev_id, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(dev_id)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        cap.set(cv2.CAP_PROP_FPS, 60)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap
    elif os.path.isfile(source):
        print(f"[*] Opening local video file for replay: {source}...")
        return cv2.VideoCapture(source)
    else:
        print(f"[*] Opening network stream from Android phone: {source}...")
        if source.startswith("rtsp://"):
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                "rtsp_transport;udp|fflags;nobuffer|flags;low_delay|framedrop;1"
            )
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap


def run_capture_loop(
    source: str,
    shm_name: str,
    mock_mode: bool = False,
    max_frames: int = 0,
    visual_breach_after_s: float = 0.0,
    loop_file: bool = True,
    enable_frozen_guard: Optional[bool] = None,
    profile_path: Optional[str] = None,
):
    prof = load_lathe_profile(profile_path)
    roi_cfg = prof.get("roi", {"x0": 510, "y0": 240, "w": ROI_W, "h": ROI_H})
    roi_x0 = max(0, min(1920 - ROI_W, int(roi_cfg.get("x0", 510))))
    roi_y0 = max(0, min(1080 - ROI_H, int(roi_cfg.get("y0", 240))))
    prof_axis_y = int(prof.get("axis_y", SPINDLE_AXIS_Y))
    prof_stations = [int(s) for s in prof.get("x_stations", X_STATIONS)]
    prof_margin = int(prof.get("margin_px", 25))
    rc_raw = prof.get("radius_clamp", [60, 160])
    prof_clamp = (int(rc_raw[0]), int(rc_raw[1]))

    with win32_realtime_context(affinity_mask=MASK_CAPTURE_CORES, time_critical=False):
        writer = LatheShmWriter(shm_name=shm_name, create=True)
        cap = None
        is_file_source = (not mock_mode) and os.path.isfile(source)
        file_fps = 60.0

        if not mock_mode:
            cap = open_camera(source)
            if not cap.isOpened():
                print(f"[ERROR] Failed to open camera source: {source}", file=sys.stderr)
                writer.close()
                return
            if is_file_source:
                detected_fps = float(cap.get(cv2.CAP_PROP_FPS) or 60.0)
                file_fps = detected_fps if 5.0 <= detected_fps <= 240.0 else 60.0

        use_frozen_guard = (not mock_mode and not is_file_source) if enable_frozen_guard is None else enable_frozen_guard
        prev_sub_frame: Optional[np.ndarray] = None
        identical_frame_streak = 0

        init_top = max(10, prof_axis_y - 100)
        init_bot = min(ROI_H - 10, prof_axis_y + 100)
        poly = np.array(
            [[x, init_top] for x in prof_stations] + [[x, init_bot] for x in reversed(prof_stations)],
            dtype=np.int32,
        )
        frame_idx = 0
        t_start = time.perf_counter()

        print(
            f"[+] Capture worker active! Publishing to SHM '{shm_name}' "
            f"(ROI {ROI_W}x{ROI_H}, profile={prof.get('lathe_id', 'default')})"
        )

        try:
            while max_frames == 0 or frame_idx < max_frames:
                if mock_mode:
                    inject_vis = visual_breach_after_s > 0.0 and (time.perf_counter() - t_start) >= visual_breach_after_s
                    full_frame = generate_mock_lathe_frame(
                        frame_idx,
                        inject_intrusion=inject_vis,
                        static_blank=(visual_breach_after_s > 0.0),
                    )
                    time.sleep(1.0 / 60.0)  # Simulate 60 FPS sensor cadence
                else:
                    ret, full_frame = cap.read()
                    if not ret:
                        if is_file_source:
                            if loop_file:
                                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                continue
                            break
                        time.sleep(0.001)
                        continue
                    if is_file_source:
                        time.sleep(1.0 / file_fps)

                # Ensure frame is 1920x1080 or resize to target
                if full_frame.shape[0] != 1080 or full_frame.shape[1] != 1920:
                    full_frame = cv2.resize(full_frame, (1920, 1080))

                # Pre-crop 600x900 ROI around lathe bed (74% bandwidth reduction)
                roi_bgr = full_frame[roi_y0 : roi_y0 + ROI_H, roi_x0 : roi_x0 + ROI_W]

                # T1.4 Frozen-frame guard: if >= 12 consecutive reads are bit-identical,
                # set CAMERA_FROZEN and stop refreshing last_capture_ns so the 50ms stale watchdog trips.
                if use_frozen_guard:
                    cur_sub = np.ascontiguousarray(roi_bgr[::8, ::8])
                    if prev_sub_frame is not None and np.array_equal(cur_sub, prev_sub_frame):
                        identical_frame_streak += 1
                    else:
                        identical_frame_streak = 0
                        prev_sub_frame = cur_sub.copy()
                    if identical_frame_streak >= 12:
                        writer.header.status_flags |= int(StatusFlag.CAMERA_FROZEN)
                        frame_idx += 1
                        continue

                # Update Dynamic Red-Zone Polygon at 2 Hz (every 30 frames at 60 FPS)
                # Freeze polygon updates while any CAMERA_* fault flag is active
                if frame_idx % 30 == 0 and (int(writer.header.status_flags) & CAMERA_FAULT_MASK) == 0:
                    poly = compute_dynamic_red_zone(
                        roi_bgr,
                        safety_margin_px=prof_margin,
                        radius_clamp=prof_clamp,
                        prev_poly=poly if frame_idx > 0 else None,
                        axis_y=prof_axis_y,
                        x_stations=prof_stations,
                    )

                writer.publish_frame(roi_bgr, poly)
                frame_idx += 1

                if frame_idx % 30 == 0:
                    top_y = poly[0, 1]
                    bot_y = poly[-1, 1]
                    mode_str = "MOCK" if mock_mode else ("FILE" if is_file_source else "CAMERA")
                    print(f"[{mode_str}] Frame {frame_idx} | Dynamic Red-Zone Y-Span: [{top_y}..{bot_y}] px")
        finally:
            if cap is not None:
                cap.release()
            writer.close()
            print("[+] Capture worker closed cleanly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lathe Guard Camera Capture Worker")
    parser.add_argument(
        "--source",
        default="0",
        help="Camera source: USB device index ('0', '1'), video file path, RTSP URL, or HTTP URL"
    )
    parser.add_argument("--shm", default=DEFAULT_SHM_NAME, help="Shared memory segment name")
    parser.add_argument("--mock", action="store_true", help="Run synthetic shrinking wood blank generator")
    parser.add_argument("--frames", type=int, default=0, help="Stop after N frames (0 = infinite)")
    parser.add_argument(
        "--visual-breach-after",
        type=float,
        default=0.0,
        help="In --mock mode, render a visual steel gouge + hand entering the Red-Zone after N seconds",
    )
    parser.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Loop local video file at EOF (default: True)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional path to calibrated lathe JSON profile (overrides LATHEGUARD_PROFILE)",
    )
    args = parser.parse_args()

    run_capture_loop(
        args.source,
        args.shm,
        mock_mode=args.mock,
        max_frames=args.frames,
        visual_breach_after_s=args.visual_breach_after,
        loop_file=args.loop,
        profile_path=args.profile,
    )
