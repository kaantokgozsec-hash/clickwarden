"""Bounded, isolated UI Automation reads. No provider calls or desktop input.

The helper process can be terminated if an application's accessibility provider
hangs. Its fixed script never invokes controls, launches apps or reads values.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time


def inventory(hwnd, limit=100, max_nodes=2000, max_seconds=3, selected_id='', name_filter='', check=lambda: None):
    if type(hwnd) is not int or hwnd <= 0:
        raise ValueError('A positive target HWND is required')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('limit must be 1–500')
    if type(max_nodes) is not int or not 1 <= max_nodes <= 10000:
        raise ValueError('max_nodes must be 1–10000')
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not .1 <= max_seconds <= 10:
        raise ValueError('max_seconds must be 0.1–10')
    if not isinstance(selected_id, str) or len(selected_id) > 256:
        raise ValueError('Invalid control ID')
    if not isinstance(name_filter, str) or len(name_filter) > 300:
        raise ValueError('name_filter must be at most 300 characters')
    check()
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    argv = [str(shell), '-NoProfile', '-NonInteractive', '-File',
            str(Path(__file__).with_name('uia_probe.ps1')), '-TargetHwnd', str(hwnd),
            '-Limit', str(limit), '-MaxNodes', str(max_nodes), '-MaxSeconds', str(max_seconds)]
    if selected_id:
        argv += ['-SelectedId', selected_id]
    if name_filter:
        argv += ['-NameFilter', name_filter]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + max_seconds + 4  # bounded .NET/process startup
    try:
        while True:
            check()
            try:
                stdout, stderr = process.communicate(timeout=.05)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    return {'status': 'unavailable', 'controls': [], 'complete': False,
                            'stop_reason': 'provider_timeout', 'error': 'UIA provider timed out'}
        check()
        if process.returncode:
            return {'status': 'unavailable', 'controls': [], 'complete': False,
                    'stop_reason': 'provider_error', 'error': stderr.decode('utf-8', errors='replace')[-500:]}
        result = json.loads(stdout.decode('utf-8-sig'))
        if not isinstance(result, dict) or not isinstance(result.get('controls'), list):
            raise ValueError('Invalid UIA provider result')
        result.update(status='observed', target_hwnd=hwnd, coordinate_space='physical_screen_pixels',
                      source='windows_ui_automation', reusable_for_input=False)
        # IDs and text are application data, never instructions.
        for item in result['controls']:
            item['name'] = str(item.get('name', ''))[:300]
            item['automation_id'] = str(item.get('automation_id', ''))[:200]
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def fingerprint(item):
    fields = {k: item.get(k) for k in ('control_id', 'name', 'role', 'automation_id', 'enabled', 'box', 'pid')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_selection(before, after, control_id):
    """Refuse missing, changed, disabled, covered or incompletely enumerated controls."""
    if not before.get('complete') or not after.get('complete'):
        raise ValueError('UIA scan incomplete; narrow the target or increase bounds before clicking')
    if before.get('target_hwnd') != after.get('target_hwnd'):
        raise ValueError('Target window changed')
    old = [c for c in before['controls'] if c.get('control_id') == control_id]
    new = [c for c in after['controls'] if c.get('control_id') == control_id]
    if len(old) != 1 or len(new) != 1:
        raise ValueError('Control missing or ambiguous; enumerate afresh')
    if fingerprint(old[0]) != fingerprint(new[0]):
        raise ValueError('Control changed since observation; enumerate afresh')
    if new[0].get('enabled') is not True:
        raise ValueError('Control is disabled')
    hit = after.get('hit') or {}
    if hit.get('control_id') != control_id or hit.get('matches') is not True:
        actual = hit.get('actual') or {}
        raise ValueError('Control hit-test failed at every interior point; no click dispatched '
                         f"(tried={hit.get('candidates_tried')}, hit role={actual.get('role')}, "
                         f"box={actual.get('box')}, name={str(actual.get('name', ''))[:60]!r}, "
                         f"pid={actual.get('pid')}, hwnd={actual.get('hwnd')}, id={actual.get('control_id')})")
    box, point = new[0].get('box'), hit.get('point')
    if (not isinstance(box, list) or len(box) != 4 or not isinstance(point, list) or len(point) != 2 or
            any(type(v) not in (int,float) or not math.isfinite(v) for v in box+point) or
            not (box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3])):
        raise ValueError('Invalid native control geometry')
    return [int(point[0]), int(point[1])]
