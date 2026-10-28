"""
test_safety_math.py — Unit & Boundary Test Suite for CraftTwin AI Lathe VFD Speed Rules
The Crafters Hub | CraftTwin AI
Author: Hosam (Co-Founder, The Crafters Hub)
Standards: AAW Guidelines, OSHA 29 CFR 1910.213, NEBOSH Hierarchy of Controls
"""

import pytest

def mm_to_inches(diameter_mm: float) -> float:
    """Converts millimeters to inches."""
    return diameter_mm / 25.4

def calculate_speed_factor(rpm: float, diameter_mm: float) -> float:
    """
    Calculates peripheral speed factor: RPM * D_inches.
    AAW Golden Rule:
    - Roughing / shaping: RPM * D_inches <= 6,000
    - Finishing / truing:  RPM * D_inches <= 9,000
    """
    return rpm * mm_to_inches(diameter_mm)

def validate_vfd_speed(rpm: float, diameter_mm: float, is_roughing: bool = True) -> dict:
    """
    Evaluates speed safety status based on workpiece diameter.
    Returns dictionary with status ('optimal', 'suboptimal', 'danger') and safety metrics.
    """
    d_inches = mm_to_inches(diameter_mm)
    speed_factor = round(rpm * d_inches, 2)
    threshold = 6000.0 if is_roughing else 9000.0
    max_safe_rpm = round(threshold / max(0.1, d_inches))

    # Minimum threshold to avoid grain tearout on small diameters
    min_optimal_rpm = round(2000.0 / max(0.1, d_inches))

    if speed_factor > threshold:
        return {
            "status": "danger",
            "speed_factor": speed_factor,
            "max_safe_rpm": max_safe_rpm,
            "is_safe": False,
            "message": "Hazardous overspeed! Exceeds AAW & OSHA centrifugal safety limit."
        }
    elif rpm < min_optimal_rpm and diameter_mm < 45.0:
        return {
            "status": "suboptimal",
            "speed_factor": speed_factor,
            "max_safe_rpm": max_safe_rpm,
            "is_safe": True,
            "message": "Sub-optimal speed: Low surface speed causes grain tearout on small diameter."
        }
    else:
        return {
            "status": "optimal",
            "speed_factor": speed_factor,
            "max_safe_rpm": max_safe_rpm,
            "is_safe": True,
            "message": "Optimal woodturning surface speed meeting AAW standards."
        }

# --- TEST CASES ---

@pytest.mark.parametrize("rpm,diam_mm,is_roughing,expected_safe", [
    # 1. 100mm (~3.94 in) spindle at 1,000 RPM (Roughing): 1000 * 3.937 = 3937 <= 6000 -> PASS
    (1000.0, 100.0, True, True),
    # 2. 300mm (~11.81 in) bowl blank at 2,000 RPM (Roughing): 2000 * 11.811 = 23622 > 6000 -> FAIL
    (2000.0, 300.0, True, False),
    # 3. 300mm (~11.81 in) bowl blank at 500 RPM (Roughing): 500 * 11.811 = 5905.5 <= 6000 -> PASS
    (500.0, 300.0, True, True),
    # 4. 400mm (~15.75 in) platter at 300 RPM (Roughing): 300 * 15.748 = 4724 <= 6000 -> PASS
    (300.0, 400.0, True, True),
    # 5. 50.8mm (2.0 in) spindle at 3,000 RPM (Finishing): 3000 * 2.0 = 6000 <= 9000 -> PASS
    (3000.0, 50.8, False, True),
    # 6. 100mm (~3.94 in) at 3,000 RPM (Finishing): 3000 * 3.937 = 11811 > 9000 -> FAIL
    (3000.0, 100.0, False, False),
    # 7. Boundary: 1 inch (25.4mm) at 3,200 RPM max lathe speed (Finishing): 3200 * 1.0 = 3200 <= 9000 -> PASS
    (3200.0, 25.4, False, True),
    # 8. Boundary: 6 inch (152.4mm) at 1,000 RPM (Roughing): 1000 * 6.0 = 6000 <= 6000 -> PASS
    (1000.0, 152.4, True, True),
    # 9. Boundary: 6 inch (152.4mm) at 1,050 RPM (Roughing): 1050 * 6.0 = 6300 > 6000 -> FAIL
    (1050.0, 152.4, True, False),
])
def test_vfd_boundary_cases(rpm, diam_mm, is_roughing, expected_safe):
    res = validate_vfd_speed(rpm, diam_mm, is_roughing)
    assert res["is_safe"] == expected_safe

def test_surface_feet_per_minute():
    """Verify Surface Feet Per Minute (SFM) calculation."""
    # Dia = 100mm, RPM = 1200
    # D_meters = 0.1, V = pi * 0.1 * 1200 / 60 = 6.283 m/s
    # SFM = 6.283 * 196.85 = 1236.8
    d_mm = 100.0
    rpm = 1200.0
    v_mps = (3.14159265 * (d_mm / 1000.0) * rpm) / 60.0
    sfm = v_mps * 196.85
    assert 1200 < sfm < 1300

if __name__ == "__main__":
    pytest.main(["-v", __file__])
