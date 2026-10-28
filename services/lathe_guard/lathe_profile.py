"""
lathe_profile.py — Lathe Geometry & Workshop Profile Loader (Phase 3: T3.1)
The Crafters Hub | LatheGuard Physical AI

Supports loading calibrated JSON profiles for all 3 physical Crafters Hub studio lathes:
  - `crafters_hub_midi_lathe` (Default standard baseline)
  - `fox_f46_717_grey`        (FOX F46-717 Heavy Swivel-Head Lathe, 300mm swing, 1000mm bed)
  - `mc1018_yellow`           (Master Craft MC1018 Midi Lathe, 250mm swing, 450mm bed)
  - `wl300_blue`              (Wood Lathe WL300 Mini Lathe, 200mm swing, 305mm bed)
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

DEFAULT_LATHE_PROFILE: Dict[str, Any] = {
    "name": "crafters_hub_midi_lathe",
    "display_name": "The Crafters Hub Standard Midi Lathe (Default)",
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


def list_available_profiles() -> List[str]:
    """Returns stem names of all `.json` profiles inside `profiles/`."""
    if not PROFILES_DIR.is_dir():
        return [DEFAULT_LATHE_PROFILE["name"]]
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load_lathe_profile(name_or_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads a lathe profile by JSON path or profile name inside `profiles/`.
    Merges onto `DEFAULT_LATHE_PROFILE` so all required keys are guaranteed present.
    """
    prof = json.loads(json.dumps(DEFAULT_LATHE_PROFILE))
    if not name_or_path:
        target = PROFILES_DIR / "crafters_hub_midi_lathe.json"
    else:
        cand = Path(name_or_path)
        if cand.is_file():
            target = cand
        elif (PROFILES_DIR / name_or_path).is_file():
            target = PROFILES_DIR / name_or_path
        elif (PROFILES_DIR / f"{name_or_path}.json").is_file():
            target = PROFILES_DIR / f"{name_or_path}.json"
        else:
            target = PROFILES_DIR / "crafters_hub_midi_lathe.json"

    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prof.update(loaded)
        except Exception as e:
            print(f"[PROFILE WARN] Failed to parse {target}: {e} — using defaults.")
    return prof
