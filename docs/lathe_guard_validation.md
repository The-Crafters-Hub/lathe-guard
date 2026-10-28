# LatheGuard Physical AI — Validation & Corpus Benchmark Report

**Generated (UTC):** `2026-10-26T19:27:38.506083+00:00`  
**Total Evaluated Clips:** `80` (`40` Negative Hazard-Free Clips + `40` Positive Intrusion Clips)  
**Real Workshop Hardware Backgrounds Included:**
- `fox_f46_717_grey` — FOX F46-717 Heavy Floor-Standing Lathe (`300 mm` swing, `1000 mm` bed, `M33×3.5`, `MT2`)
- `mc1018_yellow` — Master Craft MC1018 Midi Lathe (`250 mm` swing, `450 mm` bed, `1"×8 TPI`, `MT2`)
- `wl300_blue` — Wood Lathe WL300 Mini Precision Lathe (`200 mm` swing, `305 mm` bed, `3/4"×16 TPI`, `MT1`)

---

## 1. P0 Safety & Determinism Gate Summary

| Gate ID | Safety / Real-Time Criterion | Target Threshold | Measured Value | Status |
| :--- | :--- | :--- | :--- | :--- |
| **G-1** | False Trips on Negative Clips (Shadows, Square Blank, Sawdust, Flicker, Real Photos) | `0 / 40` | **`0 / 40`** | PASS |
| **G-2** | Intrusion Recall on Positive Clips (Hands, Gloves, 6mm Gouge, Collar, Real Photos) | `100.0%` (`40/40`) | **`100.0%` (`40/40`)** | PASS |
| **G-3** | P95 Detection Delay After Moat Contact (2-Frame Spatial Debounce) | `<= 3.0 frames` | **`2.0 frames`** | PASS |
| **G-4** | Max Penetration Depth at Fast Hand Speed (`30 px/frame` = `1,800 px/s`) | `<= 40.0 px` | **`32.0 px`** | PASS |
| **G-5** | CPU Vision Detector Latency (`225x150` Zero-Copy Strided Pass) | `P95 < 3.50 ms` | **`P50 = 1.26 ms`, `P95 = 2.27 ms`, `P99 = 3.24 ms`** | PASS |

---

## 2. Representative Clip Breakdown

| Clip ID | Type | Background | Armed | Tripped | Delay (Frames) | Penetration (px) | Fault Code | Result |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `neg_real_wl300_blue_static` | NEGATIVE | `wl300_blue` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_real_mc1018_yellow_static` | NEGATIVE | `mc1018_yellow` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_real_fox_f46_717_grey_static` | NEGATIVE | `fox_f46_717_grey` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_square_blank_600rpm` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_square_blank_1000rpm` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_square_blank_1800rpm` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_square_blank_3000rpm` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_shadow_25pct` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_shadow_35pct` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_shadow_50pct` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_shadow_70pct` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_sawdust_storm` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_led_100hz_flicker` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `neg_bystander_outside_moat` | NEGATIVE | `synthetic` | True | False | - | 0.0 | `NONE` | **PASS** |
| `pos_real_wl300_blue_hand_entry` | POSITIVE | `wl300_blue` | True | True | 2 | 12.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_real_mc1018_yellow_hand_entry` | POSITIVE | `mc1018_yellow` | True | True | 2 | 12.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_real_fox_f46_717_grey_hand_entry` | POSITIVE | `fox_f46_717_grey` | True | True | 1 | 2.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_fast_30px_operator_hand` | POSITIVE | `synthetic` | True | True | 2 | 32.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_slow_2px_creeping_hand` | POSITIVE | `synthetic` | True | True | 1 | 0.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_black_nitrile_glove` | POSITIVE | `synthetic` | True | True | 2 | 12.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_blue_nitrile_glove` | POSITIVE | `synthetic` | True | True | 2 | 12.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_thin_6mm_steel_gouge` | POSITIVE | `synthetic` | True | True | 3 | 22.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_13mm_bowl_gouge` | POSITIVE | `synthetic` | True | True | 2 | 20.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_crafttwin_cyan_collar` | POSITIVE | `synthetic` | True | True | 2 | 55.0 | `COLLAR` | **PASS** |
| `pos_far_side_over_top_reach` | POSITIVE | `synthetic` | True | True | 1 | 8.0 | `HAND_OR_TOOL_MOAT` | **PASS** |
| `pos_two_hands_entry` | POSITIVE | `synthetic` | True | True | 2 | 20.0 | `HAND_OR_TOOL_MOAT` | **PASS** |

---

## 3. Reproduce This Benchmark

```powershell
python services/lathe_guard/corpus/eval_corpus.py
```
