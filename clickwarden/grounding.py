"""Fail-closed image grounding schema. No I/O or input injection."""
import math


def validate_grounding(value, width, height):
    if not isinstance(value, dict):
        return {"success": False, "error": "grounding must be a JSON object"}
    status = value.get("status")
    if status in ("absent", "ambiguous"):
        return {"success": False, "grounding_status": status,
                "error": "Target " + status, "description": str(value.get("description", ""))[:240]}
    if status != "found":
        return {"success": False, "error": "grounding requires found/absent/ambiguous status"}
    box = value.get("box")
    if (not isinstance(box, list) or len(box) != 4 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in box)):
        return {"success": False, "error": "found target requires a finite [left,top,right,bottom] box"}
    l, t, r, b = box
    if not (0 <= l < r <= width and 0 <= t < b <= height):
        return {"success": False, "error": "target box outside image or empty"}
    confidence = value.get("confidence")
    if confidence not in ("high", "medium", "low"):
        return {"success": False, "error": "invalid grounding confidence"}
    # Use the box interior, not a second independently guessed point. Tiny boxes
    # with no integer interior are refused, never silently clamped onto a neighbor.
    x, y = math.floor((l+r)/2), math.floor((t+b)/2)
    if not (l <= x < r and t <= y < b):
        return {"success": False, "error": "target box has no safe integer center"}
    if "point" in value:
        point = value["point"]
        if (not isinstance(point, list) or len(point) != 2 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in point) or
                not (l <= point[0] < r and t <= point[1] < b)):
            return {"success": False, "error": "reported point contradicts target box"}
    return {"success": True, "grounding_status": "found", "image_box": box,
            "image_point": [x, y], "confidence": confidence}
