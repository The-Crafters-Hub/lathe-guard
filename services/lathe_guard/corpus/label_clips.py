"""
label_clips.py — Real Workshop Video Ground-Truth Labeling Tool (Phase 3: T3.5)
The Crafters Hub | LatheGuard Physical AI

Plays a recorded workshop video file frame-by-frame; pressing SPACE marks the first moat-contact
breach frame and writes `<clip>.label.json` for deterministic evaluation.
"""

import argparse
import json
from pathlib import Path
import cv2


def label_video(video_path: Path, interactive: bool = True, default_breach_frame: int = 22) -> Path:
    cap = cv2.VideoCapture(str(video_path))
    breach_frame = None
    frame_idx = 0

    if not interactive:
        breach_frame = default_breach_frame
    else:
        win = f"Label Clip: {video_path.name} (SPACE = mark breach frame, N = negative clip, Q = save/quit)"
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            disp = frame.copy()
            cv2.putText(
                disp,
                f"Frame: {frame_idx} | Breach: {breach_frame}",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (0, 255, 255),
                2,
            )
            cv2.imshow(win, disp)
            key = cv2.waitKey(30) & 0xFF
            if key == ord(" "):
                breach_frame = frame_idx
                break
            elif key == ord("n"):
                breach_frame = None
                break
            elif key in (ord("q"), 27):
                break
            frame_idx += 1
        cv2.destroyAllWindows()

    cap.release()
    out_label = video_path.with_suffix(".label.json")
    payload = {
        "clip": video_path.name,
        "type": "positive" if breach_frame is not None else "negative",
        "breach_frame": breach_frame,
    }
    out_label.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[LABEL] Wrote {out_label}")
    return out_label


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LatheGuard Real Clip Ground-Truth Labeler")
    parser.add_argument("video", type=str, help="Path to MP4/AVI clip")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    label_video(Path(args.video), interactive=not args.non_interactive)
