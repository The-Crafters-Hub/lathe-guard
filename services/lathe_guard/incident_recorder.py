"""
incident_recorder.py — Black-Box Flight Recorder for LatheGuard Safety Trips (Phase 4: T4.1)
The Crafters Hub | LatheGuard Physical AI

Architecture & Privacy-by-Design (PDPL-Aligned):
  - Runs on `MASK_PEDAGOGY_CORES` (read-only Seqlock SHM consumer, zero impact on Core 0 100Hz FSM)
  - Maintains an in-memory 8-second circular ring buffer of compressed JPEG frames (q=75, ~15 FPS)
  - On `FAULT_TRIPPED`: captures +1.5s post-trip frames and writes:
      `incidents/<YYYYMMDD_HHMMSS>_<faultcode>/{clip.mp4, event.json, snapshot.jpg}`
  - Overlays Red-Zone polygon, breach crosshair, UTC timestamp, FaultCode name, and state banner
  - Automatically purges local incident folders older than 30 days at startup; video never leaves disk.
"""

import argparse
from collections import deque
from datetime import datetime, UTC
import json
import shutil
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
import cv2
import numpy as np

from lathe_scheduler import MASK_PEDAGOGY_CORES, win32_realtime_context
from lathe_shm import (
    DEFAULT_SHM_NAME,
    FaultCode,
    LatheShmReader,
    ROI_H,
    ROI_W,
    SafetyState,
    StatusFlag,
)

INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"


def purge_old_incidents(root_dir: Path = INCIDENTS_DIR, max_age_days: float = 30.0) -> int:
    """Deletes incident directories whose modification time is older than `max_age_days`."""
    if not root_dir.is_dir():
        return 0
    cutoff = time.time() - (max_age_days * 86400.0)
    purged = 0
    for child in root_dir.iterdir():
        if child.is_dir():
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    purged += 1
            except Exception:
                pass
    return purged


def annotate_incident_frame(
    frame_bgr: np.ndarray,
    poly: np.ndarray,
    state_val: int,
    fault_code: int,
    bx: int,
    by: int,
    ts_iso: str,
) -> np.ndarray:
    """Draws the forensic overlay (Red-Zone polygon, breach crosshair, UTC timestamp, fault code)."""
    vis = frame_bgr.copy()
    is_fault = state_val == int(SafetyState.FAULT_TRIPPED)
    color = (0, 0, 255) if is_fault else (0, 220, 90)

    if len(poly) >= 3:
        overlay = vis.copy()
        cv2.fillPoly(overlay, [poly], color)
        cv2.addWeighted(overlay, 0.22 if is_fault else 0.12, vis, 0.78 if is_fault else 0.88, 0, vis)
        cv2.polylines(vis, [poly], True, color, 2)

    if bx >= 0 and by >= 0:
        cv2.circle(vis, (bx, by), 20, (0, 0, 255), 3)
        cv2.drawMarker(vis, (bx, by), (0, 255, 255), cv2.MARKER_CROSS, 26, 2)

    try:
        state_name = SafetyState(state_val).name
    except ValueError:
        state_name = str(state_val)
    try:
        fault_name = FaultCode(fault_code).name
    except ValueError:
        fault_name = str(fault_code)

    cv2.rectangle(vis, (0, 0), (ROI_W, 38), (15, 15, 15), -1)
    cv2.putText(
        vis,
        f"BLACK-BOX | {state_name} | FAULT={fault_name} | {ts_iso}",
        (12, 25),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        color,
        2,
        cv2.LINE_AA,
    )
    return vis


class BlackBoxIncidentRecorder:
    """In-memory JPEG ring buffer + MP4/JSON incident exporter."""

    def __init__(self, incidents_root: Path = INCIDENTS_DIR, fps: float = 15.0, pre_seconds: float = 8.0):
        self.incidents_root = incidents_root
        self.fps = fps
        self.max_pre_frames = max(10, int(round(fps * pre_seconds)))
        self.ring: Deque[Tuple[bytes, np.ndarray, int, int, int, int, str]] = deque(maxlen=self.max_pre_frames)
        purge_old_incidents(self.incidents_root, max_age_days=30.0)

    def push_frame(
        self,
        frame_bgr: np.ndarray,
        poly: np.ndarray,
        state_val: int,
        fault_code: int,
        bx: int = -1,
        by: int = -1,
    ) -> None:
        ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if ok:
            self.ring.append(
                (
                    buf.tobytes(),
                    poly.copy(),
                    int(state_val),
                    int(fault_code),
                    int(bx),
                    int(by),
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                )
            )

    def flush_incident(
        self,
        post_frames: Optional[List[Tuple[np.ndarray, np.ndarray, int, int, int, int]]] = None,
        status_flags: int = 0,
    ) -> Path:
        """Writes `clip.mp4`, `snapshot.jpg`, and `event.json` into a timestamped incident folder."""
        all_items = list(self.ring)
        if post_frames:
            for frm, poly, st, fc, bx, by in post_frames:
                ok, buf = cv2.imencode(".jpg", frm, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                if ok:
                    all_items.append(
                        (
                            buf.tobytes(),
                            poly.copy(),
                            int(st),
                            int(fc),
                            int(bx),
                            int(by),
                            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                        )
                    )

        fault_code = 2
        bx_last, by_last = -1, -1
        for _, _, st, fc, bx, by, _ in reversed(all_items):
            if st == int(SafetyState.FAULT_TRIPPED) or fc > 0:
                fault_code = fc if fc > 0 else 2
                bx_last, by_last = bx, by
                break

        ts_slug = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        inc_dir = self.incidents_root / f"{ts_slug}_{fault_code}"
        suffix = 1
        while inc_dir.exists():
            inc_dir = self.incidents_root / f"{ts_slug}_{fault_code}_{suffix}"
            suffix += 1
        inc_dir.mkdir(parents=True, exist_ok=True)

        clip_path = inc_dir / "clip.mp4"
        writer = cv2.VideoWriter(
            str(clip_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            float(self.fps),
            (ROI_W, ROI_H),
        )
        if not writer.isOpened():
            clip_path = inc_dir / "clip.avi"
            writer = cv2.VideoWriter(
                str(clip_path),
                cv2.VideoWriter_fourcc(*"MJPG"),
                float(self.fps),
                (ROI_W, ROI_H),
            )

        snapshot_saved = False
        frames_written = 0
        for jpg_bytes, poly, st, fc, bx, by, ts_iso in all_items:
            arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
            decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if decoded is None:
                continue
            ann = annotate_incident_frame(decoded, poly, st, fc, bx, by, ts_iso)
            writer.write(ann)
            frames_written += 1
            if (not snapshot_saved) and (st == int(SafetyState.FAULT_TRIPPED) or frames_written == len(all_items)):
                cv2.imwrite(str(inc_dir / "snapshot.jpg"), ann)
                snapshot_saved = True

        writer.release()

        try:
            fault_name = FaultCode(fault_code).name
        except ValueError:
            fault_name = "HAND_OR_TOOL_MOAT"

        event_payload: Dict[str, Any] = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "fault_code": int(fault_code),
            "fault_name": fault_name,
            "intrusion_xy": [int(bx_last), int(by_last)],
            "norm_xy": [
                round(max(0, bx_last) / float(ROI_W), 3),
                round(max(0, by_last) / float(ROI_H), 3),
            ],
            "status_flags": int(status_flags),
            "frames_recorded": frames_written,
            "clip_file": clip_path.name,
            "snapshot_file": "snapshot.jpg",
            "pdpl_notice": "Local workshop safety black-box record; auto-purged after 30 days.",
        }
        (inc_dir / "event.json").write_text(json.dumps(event_payload, indent=2), encoding="utf-8")
        return inc_dir


def run_recorder_loop(shm_name: str, incidents_root: Path = INCIDENTS_DIR, fps: float = 15.0) -> None:
    with win32_realtime_context(affinity_mask=MASK_PEDAGOGY_CORES, time_critical=False):
        reader = LatheShmReader(shm_name=shm_name)
        recorder = BlackBoxIncidentRecorder(incidents_root=incidents_root, fps=fps)
        interval_s = 1.0 / max(1.0, fps)
        latched_recorded = False
        print(f"[RECORDER] Black-box flight recorder attached to '{shm_name}' ({fps:.0f} FPS, 8s ring)...")
        try:
            while True:
                t0 = time.perf_counter()
                latest = reader.read_latest(copy_frame=True)
                st = int(reader.header.safety_state)
                fc = int(reader.header.fault_code)
                bx = int(reader.header.intrusion_x)
                by = int(reader.header.intrusion_y)
                flags = int(reader.header.status_flags)

                if latest is not None:
                    frame_roi, poly, _, _ = latest
                    recorder.push_frame(frame_roi, poly, st, fc, bx, by)

                if st == int(SafetyState.FAULT_TRIPPED) and not latched_recorded:
                    # Capture +1.5s post-fault frames
                    post_n = int(round(fps * 1.5))
                    for _ in range(post_n):
                        time.sleep(interval_s)
                        lat_post = reader.read_latest(copy_frame=True)
                        if lat_post is not None:
                            f_p, p_p, _, _ = lat_post
                            recorder.push_frame(
                                f_p,
                                p_p,
                                int(reader.header.safety_state),
                                int(reader.header.fault_code),
                                int(reader.header.intrusion_x),
                                int(reader.header.intrusion_y),
                            )
                    out_dir = recorder.flush_incident(status_flags=flags)
                    print(f"[RECORDER] Saved black-box incident clip & snapshot -> {out_dir}")
                    latched_recorded = True
                elif st != int(SafetyState.FAULT_TRIPPED):
                    latched_recorded = False

                elapsed = time.perf_counter() - t0
                if elapsed < interval_s:
                    time.sleep(interval_s - elapsed)
        except KeyboardInterrupt:
            pass
        finally:
            reader.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LatheGuard Black-Box Incident Flight Recorder")
    parser.add_argument("--shm", default=DEFAULT_SHM_NAME)
    parser.add_argument("--fps", type=float, default=15.0)
    args = parser.parse_args()
    run_recorder_loop(args.shm, fps=args.fps)
