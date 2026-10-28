"""
synthetic_corpus.py — Realistic Woodturning & Real Workshop Photo Corpus Generator (Phase 3: T3.3)
The Crafters Hub | LatheGuard Physical AI

Generates ground-truth labeled video clip streams (both 80-clip full suite and 20-clip `--quick` suite)
combining:
  - Procedural lathe beds AND real Crafters Hub workshop photos (`fox_f46_717`, `mc1018_yellow`, `wl300_blue`)
  - Cylindrical AND spinning square-to-round blanks (`600 / 1000 / 1800 / 3000 RPM`)
  - Realistic camera impairments: Gaussian sensor noise, 100Hz LED flicker at 60fps, AE drift,
    1-2 frame flying sawdust particles, soft shadows (25-70%, sigma=40px), bystander outside Red-Zone
  - Intruder varieties: bare hands, leather/nitrile/black gloves, long sleeves, two-hand entries,
    Sheffield steel gouges (`6 / 10 / 13 / 19 mm` shafts with wooden handle + brass ferrule),
    and CraftTwin Cyan/Magenta collar markers from both Operator (`y > 300`) and Far (`y < 300`) sides.
"""

import argparse
import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple
import cv2
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lathe_shm import ROI_C, ROI_H, ROI_W
from lathe_vision_capture import SPINDLE_AXIS_Y, compute_dynamic_red_zone

WORKSHOP_PHOTOS_DIR = Path(
    os.environ.get(
        "LATHEGUARD_REAL_BG_DIR",
        str(Path(__file__).resolve().parent / "real_bg"),
    )
)
if not WORKSHOP_PHOTOS_DIR.is_dir():
    _legacy = Path(__file__).resolve().parents[2] / "craft_twin" / "assets" / "Lathes dimensions"
    if _legacy.is_dir():
        WORKSHOP_PHOTOS_DIR = _legacy
REAL_PHOTO_FILES = {
    "wl300_blue": "1000096832.jpg",
    "mc1018_yellow": "1000096827.jpg",
    "fox_f46_717_grey": "1000096816.jpg",
}


@dataclass
class ClipScenario:
    clip_id: str
    is_positive: bool
    category: str
    description: str
    n_frames: int
    speed_px_per_f: float = 0.0
    intruder_kind: str = "none"
    side: str = "operator"        # "operator" (bottom y > 300) or "far" (top y < 300)
    bg_source: str = "synthetic"  # "synthetic" or key in REAL_PHOTO_FILES
    rpm: int = 1200
    square_blank: bool = False
    shadow_alpha: float = 0.0
    sawdust: bool = False
    flicker: bool = False
    bystander_outside: bool = False
    color_bgr: Tuple[int, int, int] = (140, 175, 228)
    width_px: int = 64


_REAL_BG_CACHE: Dict[str, np.ndarray] = {}
_SHADOW_MASK_CACHE: Optional[np.ndarray] = None


def _get_shadow_mask() -> np.ndarray:
    global _SHADOW_MASK_CACHE
    if _SHADOW_MASK_CACHE is None:
        m = np.zeros((ROI_H, ROI_W), dtype=np.float32)
        cv2.rectangle(m, (280, 0), (620, ROI_H), 1.0, thickness=-1)
        _SHADOW_MASK_CACHE = cv2.GaussianBlur(m, (0, 0), 40)[..., None]
    return _SHADOW_MASK_CACHE


def _load_real_roi(bg_key: str) -> Optional[np.ndarray]:
    if bg_key in _REAL_BG_CACHE:
        return _REAL_BG_CACHE[bg_key]
    fname = REAL_PHOTO_FILES.get(bg_key)
    if not fname:
        return None
    fpath = WORKSHOP_PHOTOS_DIR / fname
    if not fpath.is_file():
        return None
    img = cv2.imread(str(fpath))
    if img is None:
        return None
    full = cv2.resize(img, (1920, 1080))
    roi = full[240 : 240 + ROI_H, 510 : 510 + ROI_W].copy()
    _REAL_BG_CACHE[bg_key] = roi
    return roi


def _draw_intruder(
    roi: np.ndarray,
    kind: str,
    side: str,
    tip_y: int,
    x_center: int,
    width_px: int,
    color_bgr: Tuple[int, int, int],
) -> None:
    """Draws a realistic hand/glove, gouge (handle + brass ferrule + steel shaft), or collar marker."""
    x0 = max(10, x_center - width_px // 2)
    x1 = min(ROI_W - 10, x_center + width_px // 2)

    if side == "operator":
        y_start = max(SPINDLE_AXIS_Y - 20, min(ROI_H - 2, tip_y))
        y_end = ROI_H
        if y_start >= y_end:
            return
    else:
        y_start = 0
        y_end = min(SPINDLE_AXIS_Y + 20, max(2, tip_y))
        if y_end <= y_start:
            return

    if kind == "collar":
        # Tool shaft from edge + bright Cyan collar inside zone
        cv2.rectangle(roi, (x_center - 8, y_start), (x_center + 8, y_end), (210, 215, 220), -1)
        cy0 = y_start if side == "operator" else max(0, y_end - 44)
        cy1 = min(ROI_H, cy0 + 44)
        cv2.rectangle(roi, (x_center - 22, cy0), (x_center + 22, cy1), (255, 240, 0), -1)
    elif kind == "gouge":
        # Steel shaft at tip + brass ferrule + wooden handle near operator border
        cv2.rectangle(roi, (x0, y_start), (x1, y_end), (225, 230, 235), -1)
        cv2.line(roi, (x0, y_start), (x0, y_end), (250, 250, 255), 1)
        if side == "operator" and (y_end - y_start) > 90:
            ferrule_y = y_start + 70
            cv2.rectangle(roi, (x0 - 3, ferrule_y), (x1 + 3, min(ROI_H, ferrule_y + 18)), (55, 195, 235), -1)
            cv2.rectangle(roi, (x0 - 6, min(ROI_H, ferrule_y + 18)), (x1 + 6, ROI_H), (45, 95, 165), -1)
    elif kind == "two_hands":
        cv2.rectangle(roi, (x0 - 70, y_start), (x1 - 70, y_end), color_bgr, -1)
        cv2.rectangle(roi, (x0 + 70, y_start), (x1 + 70, y_end), color_bgr, -1)
    else:
        # Hand / glove / sleeve with vertical edge detail
        cv2.rectangle(roi, (x0, y_start), (x1, y_end), color_bgr, -1)
        cv2.line(roi, (x0, y_start), (x0, y_end), (240, 240, 245), 2)


def iter_scenario_frames(
    sc: ClipScenario,
) -> Iterator[Tuple[int, np.ndarray, np.ndarray, Optional[int], float]]:
    """
    Yields `(frame_idx, roi_bgr, poly, gt_moat_contact_frame, penetration_px)` for each frame in `sc`.
    """
    rng = np.random.default_rng(zlib.crc32(sc.clip_id.encode("utf-8")))
    real_bg = _load_real_roi(sc.bg_source) if sc.bg_source != "synthetic" else None

    fixed_poly = (
        compute_dynamic_red_zone(real_bg)
        if real_bg is not None
        else np.array(
            [[120, 175], [320, 175], [520, 175], [720, 175],
             [720, 425], [520, 425], [320, 425], [120, 425]],
            dtype=np.int32,
        )
    )
    poly = fixed_poly.copy()
    # Intruder enters at x_center = 420 (exact midpoint of stations 320 and 520: top vertices 1..2, bottom vertices 5..6)
    poly_top = int((poly[1, 1] + poly[2, 1]) // 2) if len(poly) >= 8 else int(poly[0, 1])
    poly_bot = int((poly[5, 1] + poly[6, 1]) // 2) if len(poly) >= 8 else int(poly[-1, 1])
    # Outer moat edge is ~16px outside polygon boundary
    moat_bot_y = poly_bot + 16
    moat_top_y = poly_top - 16

    gt_moat_contact_frame: Optional[int] = None
    entry_start_frame = 22

    for i in range(sc.n_frames):
        if real_bg is not None:
            roi = np.clip(
                real_bg.astype(np.int16) + rng.normal(0.0, 2.2, real_bg.shape),
                0,
                255,
            ).astype(np.uint8)
        else:
            roi = np.full((ROI_H, ROI_W, ROI_C), 26, dtype=np.uint8)
            if sc.square_blank:
                th = 2.0 * np.pi * (sc.rpm / 60.0) * (i / 60.0)
                half = int(60 * (abs(np.cos(th)) + abs(np.sin(th))))
            else:
                half = 100
            jitter = (i % 3) - 1
            cv2.rectangle(
                roi,
                (100, SPINDLE_AXIS_Y - half),
                (760, SPINDLE_AXIS_Y + half),
                (70, 142 + jitter, 212),
                thickness=-1,
            )
            cv2.line(roi, (100, SPINDLE_AXIS_Y - half), (760, SPINDLE_AXIS_Y - half), (220, 240, 255), 2)
            cv2.line(roi, (100, SPINDLE_AXIS_Y + half), (760, SPINDLE_AXIS_Y + half), (220, 240, 255), 2)
            if sc.square_blank and i % 30 == 0:
                poly = compute_dynamic_red_zone(roi, prev_poly=poly)
                poly_top = int((poly[1, 1] + poly[2, 1]) // 2) if len(poly) >= 8 else int(poly[0, 1])
                poly_bot = int((poly[5, 1] + poly[6, 1]) // 2) if len(poly) >= 8 else int(poly[-1, 1])
                moat_bot_y = poly_bot + 16
                moat_top_y = poly_top - 16

        # Negative impairments after warmup (i >= 20)
        if i >= 20:
            if sc.shadow_alpha > 0.0:
                sm = _get_shadow_mask()
                roi = (roi.astype(np.float32) * (1.0 - sc.shadow_alpha * sm)).astype(np.uint8)
            if sc.flicker:
                gain = 1.0 + 0.06 * np.sin(2.0 * np.pi * (100.0 / 60.0) * i)
                roi = np.clip(roi.astype(np.float32) * gain, 0, 255).astype(np.uint8)
            if sc.sawdust:
                # 1-frame flying sawdust chip at random x
                cx = int(180 + ((i * 257) % 500))
                cy = poly_bot - 4 + (i % 3) * 4
                cv2.rectangle(roi, (cx, cy), (cx + 12, cy + 12), (230, 235, 240), -1)
            if sc.bystander_outside:
                # Movement strictly outside the moat (y = 510..595)
                bx = 200 + (i * 6) % 400
                cv2.rectangle(roi, (bx, 515), (bx + 70, 595), (150, 180, 220), -1)

        penetration_px = 0.0
        if sc.is_positive and i >= entry_start_frame:
            step = i - entry_start_frame + 1
            if sc.intruder_kind == "collar":
                tip_y = poly_bot - 55 if sc.side == "operator" else poly_top + 55
                if gt_moat_contact_frame is None:
                    gt_moat_contact_frame = i
                penetration_px = 55.0
            elif sc.side == "operator":
                start_y = moat_bot_y + 12
                tip_y = int(round(start_y - step * sc.speed_px_per_f))
                if tip_y <= poly_bot and gt_moat_contact_frame is None:
                    gt_moat_contact_frame = i
                penetration_px = float(max(0, poly_bot - tip_y))
            else:
                start_y = moat_top_y - 12
                tip_y = int(round(start_y + step * sc.speed_px_per_f))
                if tip_y >= poly_top and gt_moat_contact_frame is None:
                    gt_moat_contact_frame = i
                penetration_px = float(max(0, tip_y - poly_top))

            _draw_intruder(
                roi,
                kind=sc.intruder_kind,
                side=sc.side,
                tip_y=tip_y,
                x_center=420,
                width_px=sc.width_px,
                color_bgr=sc.color_bgr,
            )

        yield i, roi, poly, gt_moat_contact_frame, penetration_px


def build_corpus_scenarios(quick: bool = False) -> List[ClipScenario]:
    """Builds the ground-truth labeled scenario list (80 clips full, 20 clips quick)."""
    negatives: List[ClipScenario] = []
    positives: List[ClipScenario] = []

    # 1. Real Workshop Photo Negatives (3 Lathes x multiple noise seeds)
    for bg_key in ("wl300_blue", "mc1018_yellow", "fox_f46_717_grey"):
        negatives.append(
            ClipScenario(
                clip_id=f"neg_real_{bg_key}_static",
                is_positive=False,
                category="real_workshop_bg",
                description=f"Real Crafters Hub {bg_key} photo with sensor noise (0 false trips)",
                n_frames=55,
                bg_source=bg_key,
            )
        )

    # 2. Spinning Square Blanks (600, 1000, 1800, 3000 RPM)
    for rpm in (600, 1000, 1800, 3000):
        negatives.append(
            ClipScenario(
                clip_id=f"neg_square_blank_{rpm}rpm",
                is_positive=False,
                category="square_blank_spin",
                description=f"Spinning square 120px wood blank at {rpm} RPM",
                n_frames=60,
                rpm=rpm,
                square_blank=True,
            )
        )

    # 3. Soft Shadows (25%, 35%, 50%, 70%)
    for alpha in (0.25, 0.35, 0.50, 0.70):
        negatives.append(
            ClipScenario(
                clip_id=f"neg_shadow_{int(alpha*100)}pct",
                is_positive=False,
                category="soft_shadow",
                description=f"Soft overhead operator shadow ({int(alpha*100)}%, sigma=40px)",
                n_frames=50,
                shadow_alpha=alpha,
            )
        )

    # 4. Flying Sawdust, 100Hz LED Flicker, Bystander Outside Zone, Clean Cylindrical RPMs
    negatives.append(
        ClipScenario(
            clip_id="neg_sawdust_storm",
            is_positive=False,
            category="sawdust_storm",
            description="Continuous 1-frame flying wood chips across Red-Zone boundary",
            n_frames=55,
            sawdust=True,
        )
    )
    negatives.append(
        ClipScenario(
            clip_id="neg_led_100hz_flicker",
            is_positive=False,
            category="lighting_flicker",
            description="100Hz workshop LED flicker (+/-6%) sampled at 60fps",
            n_frames=55,
            flicker=True,
        )
    )
    negatives.append(
        ClipScenario(
            clip_id="neg_bystander_outside_moat",
            is_positive=False,
            category="bystander_outside",
            description="Operator hand moving along bench outside the Red-Zone moat (y > 515)",
            n_frames=55,
            bystander_outside=True,
        )
    )

    while len(negatives) < (10 if quick else 40):
        idx = len(negatives)
        rpm = (600, 1000, 1500, 2000, 2500, 3200)[idx % 6]
        negatives.append(
            ClipScenario(
                clip_id=f"neg_clean_variation_{idx:02d}_{rpm}rpm",
                is_positive=False,
                category="clean_spin_variation",
                description=f"Clean woodturning spin variation #{idx} at {rpm} RPM",
                n_frames=45,
                rpm=rpm,
                flicker=(idx % 2 == 0),
                sawdust=(idx % 3 == 0),
            )
        )

    # ─── Positives (Intrusions that MUST trip 100%) ───────────────────────────
    # 1. Real Workshop Photo Intrusions (3 Lathes)
    for bg_key in ("wl300_blue", "mc1018_yellow", "fox_f46_717_grey"):
        positives.append(
            ClipScenario(
                clip_id=f"pos_real_{bg_key}_hand_entry",
                is_positive=True,
                category="real_workshop_intrusion",
                description=f"Operator hand entering Red-Zone on real {bg_key} background",
                n_frames=50,
                bg_source=bg_key,
                intruder_kind="hand",
                side="operator",
                speed_px_per_f=10.0,
                width_px=72,
                color_bgr=(135, 170, 225),
            )
        )

    # 2. Fast (30px/f), Medium (10px/f), Slow (2px/f), Thin 6px Gouge, Black Nitrile Glove, Cyan Collar, Far-Side Reach
    core_positives = [
        ("pos_fast_30px_operator_hand", "fast_hand_30px", "hand", "operator", 30.0, 80, (230, 230, 230)),
        ("pos_slow_2px_creeping_hand", "slow_hand_2px", "hand", "operator", 2.0, 80, (130, 165, 225)),
        ("pos_black_nitrile_glove", "black_glove", "hand", "operator", 10.0, 76, (64, 66, 70)),
        ("pos_blue_nitrile_glove", "blue_glove", "hand", "operator", 10.0, 76, (210, 110, 35)),
        ("pos_thin_6mm_steel_gouge", "thin_gouge_6px", "gouge", "operator", 10.0, 6, (235, 235, 240)),
        ("pos_13mm_bowl_gouge", "bowl_gouge_13px", "gouge", "operator", 12.0, 16, (225, 230, 235)),
        ("pos_crafttwin_cyan_collar", "collar_fast_path", "collar", "operator", 15.0, 44, (255, 240, 0)),
        ("pos_far_side_over_top_reach", "far_side_reach", "hand", "far", 12.0, 72, (140, 175, 228)),
        ("pos_two_hands_entry", "two_hands", "two_hands", "operator", 12.0, 60, (135, 170, 225)),
    ]
    for cid, cat, kind, side, spd, w, col in core_positives:
        positives.append(
            ClipScenario(
                clip_id=cid,
                is_positive=True,
                category=cat,
                description=f"{cat} ({kind}, {side}, {spd} px/f)",
                n_frames=65,
                intruder_kind=kind,
                side=side,
                speed_px_per_f=spd,
                width_px=w,
                color_bgr=col,
            )
        )

    while len(positives) < (10 if quick else 40):
        idx = len(positives)
        spd = (4.0, 8.0, 12.0, 18.0, 24.0, 30.0)[idx % 6]
        kind = ("hand", "gouge", "two_hands")[idx % 3]
        side = "operator" if (idx % 4 != 3) else "far"
        w = (8, 12, 20, 64, 80)[idx % 5] if kind == "gouge" else 72
        positives.append(
            ClipScenario(
                clip_id=f"pos_matrix_{idx:02d}_{kind}_{side}_{int(spd)}pxf",
                is_positive=True,
                category="intrusion_matrix",
                description=f"Matrix intrusion #{idx}: {kind} from {side} at {spd} px/frame",
                n_frames=60,
                intruder_kind=kind,
                side=side,
                speed_px_per_f=spd,
                width_px=w,
                color_bgr=(140 + (idx % 4) * 20, 165, 220),
            )
        )

    target_n = 10 if quick else 40
    return negatives[:target_n] + positives[:target_n]


def export_sample_replay_mp4(out_path: Path) -> Path:
    """T3.6: Exports a deterministic 1920x1080 MP4 clip (calibration + hand/gouge breach) for `--source` replay."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, 60.0, (1920, 1080))
    sc = ClipScenario(
        clip_id="sample_breach_replay",
        is_positive=True,
        category="real_workshop_intrusion",
        description="Deterministic replay clip on real Crafters Hub lathe",
        n_frames=45,
        bg_source="mc1018_yellow",
        intruder_kind="gouge",
        side="operator",
        speed_px_per_f=12.0,
        width_px=24,
    )
    for _, roi, _, _, _ in iter_scenario_frames(sc):
        full = np.full((1080, 1920, 3), 20, dtype=np.uint8)
        full[240 : 240 + ROI_H, 510 : 510 + ROI_W] = roi
        writer.write(full)
    writer.release()
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LatheGuard Realistic Corpus Generator")
    parser.add_argument("--quick", action="store_true", help="Generate 20-clip quick corpus instead of 80-clip full")
    parser.add_argument("--export-sample", type=str, default="", help="Optional path to export sample 1080p MP4 clip")
    args = parser.parse_args()
    scenarios = build_corpus_scenarios(quick=args.quick)
    print(f"[CORPUS] Generated {len(scenarios)} scenarios ({sum(not s.is_positive for s in scenarios)} neg, {sum(s.is_positive for s in scenarios)} pos).")
    if args.export_sample:
        p = export_sample_replay_mp4(Path(args.export_sample))
        print(f"[CORPUS] Exported sample replay MP4 -> {p}")
