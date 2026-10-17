"""Deterministic disposable fixture layouts for the Clickwarden failure suite.

The complete layout, the timed behaviors and the expected-recipient table are
derived from the integer seed alone (plus ``FIXTURE_VERSION``), so the same seed
reproduces the same fixture and the same expected results in a baseline report.

Layout features (WF-01):

* adjacent 16/20/32/64-pixel targets with a 2-pixel gap (wrong-click sail zone)
* two controls sharing the visible label ``Save``
* a menu that opens only after a seeded delay
* a button that replaces its node (new runtime identity) when clicked
* a control that moves after a seeded delay onto a fixed decoy position
* an overlay covering the center of another control
* nested vertical scrollers (inner inside outer)
* a focus stealer window (launched by ``fixtures.app`` in live runs)

``render_page`` embeds the spec verbatim into a self-contained HTML page.  The
page records which element actually received each click/scroll in a fixture-side
state object; that record is the oracle.  Nothing in the page decides success,
and no runtime code reads this module: fixture truth stays test-side.
"""

from __future__ import annotations

import hashlib
import json
import random

FIXTURE_VERSION = 1
TINY_SIZES = (16, 20, 32, 64)
TINY_GAP = 2


def _box(target_id, label, x, y, w, h, *, text=None, kind="button"):
    return {"id": target_id, "label": label, "kind": kind, "x": int(x), "y": int(y),
            "w": int(w), "h": int(h), "text": label if text is None else str(text)}


def build_layout(seed: int) -> dict:
    """Build the full seeded fixture spec (JSON-serializable, deterministic)."""
    if type(seed) is not int or not 0 <= seed < 2**31:
        raise ValueError("seed must be an integer in [0, 2**31)")
    rnd = random.Random(seed)

    origin_x = 30 + rnd.randrange(0, 60)
    origin_y = 24 + rnd.randrange(0, 16)
    tiny = []
    x = origin_x
    for size in TINY_SIZES:
        tiny.append(_box(f"tiny-{size}", f"tiny-{size}", x, origin_y, size, size,
                         text="+" if size < 24 else str(size)))
        x += size + TINY_GAP

    save_y = origin_y + 90 + rnd.randrange(0, 20)
    save = [
        _box("save-left", "Save", origin_x, save_y, 96, 32),
        _box("save-right", "Save", origin_x + 230 + rnd.randrange(0, 80), save_y, 96, 32),
    ]

    menu_y = save_y + 56
    menu = {
        "button": _box("menu-open", "menu-open", origin_x, menu_y, 120, 32, text="Open menu"),
        "delay_ms": 300 + rnd.randrange(0, 300),
        "items": [
            _box("item-alpha", "item-alpha", origin_x, menu_y + 40, 130, 28),
            _box("item-beta", "item-beta", origin_x, menu_y + 70, 130, 28),
        ],
    }

    rerender_y = menu_y + 122
    rerender = {
        "initial": _box("rerender", "rerender", origin_x + 220, rerender_y, 110, 32,
                        text="Rerender"),
        "replacement": _box("rerender-2", "rerender-2", origin_x + 220, rerender_y, 110, 32,
                            text="Rerendered"),
    }

    mover_y = rerender_y + 62
    mover_box_a = _box("mover", "mover", origin_x + 40, mover_y, 110, 34, text="Mover")
    mover_box_b = _box("mover", "mover", origin_x + 250, mover_y, 110, 34, text="Mover")
    mover = {
        "id": "mover", "label": "mover", "box_a": mover_box_a, "box_b": mover_box_b,
        "move_after_ms": 700 + rnd.randrange(0, 600),
        "decoy": _box("mover-decoy", "mover-decoy", mover_box_a["x"], mover_box_a["y"],
                      110, 34, text="Decoy"),
    }

    covered_y = mover_y + 74
    covered = _box("covered", "covered", origin_x, covered_y, 140, 40, text="Covered")
    overlay_box = _box("overlay", "overlay", covered["x"], covered["y"],
                       covered["w"] - 18, covered["h"], text="", kind="overlay")
    overlay = {"id": "overlay", "label": "overlay", "box": overlay_box, "covered": covered}

    scroll_y = covered_y + 74
    scroll = {
        "outer": _box("scroll-outer", "scroll-outer", origin_x, scroll_y, 280, 120,
                      text="", kind="scroller"),
        "inner": _box("scroll-inner", "scroll-inner", origin_x, scroll_y, 180, 60,
                      text="", kind="scroller"),
    }

    targets = (list(tiny) + list(save) + [menu["button"]] + list(menu["items"]) +
               [rerender["initial"], rerender["replacement"], mover_box_a, mover["decoy"],
                covered, overlay_box, scroll["outer"], scroll["inner"]])
    for item in targets:
        item["box"] = [item["x"], item["y"], item["x"] + item["w"], item["y"] + item["h"]]
    tiny_boxes = [item["box"] for item in tiny]

    scenarios = [
        {"id": "tiny_click", "kind": "click", "target": "tiny-20", "expect": "success"},
        {"id": "tiny_injected_wrong", "kind": "click", "target": "tiny-16",
         "dispatch": "tiny-64", "expect": "wrong_dispatch", "deliberate": True},
        {"id": "duplicate_label", "kind": "click", "target": "save-left",
         "expect": "success"},
        {"id": "delayed_menu_open", "kind": "click", "target": "menu-open",
         "expect": "success"},
        {"id": "delayed_menu_item", "kind": "click", "target": "item-beta",
         "expect": "success"},
        {"id": "rerender", "kind": "click", "target": "rerender", "expect": "success"},
        {"id": "rerender_replacement", "kind": "click", "target": "rerender-2",
         "expect": "success"},
        {"id": "mover_fresh", "kind": "click", "target": "mover", "expect": "success"},
        {"id": "mover_stale", "kind": "click", "target": "mover", "dispatch": "mover-decoy",
         "expect": "wrong_dispatch", "deliberate": True},
        {"id": "overlay_refusal", "kind": "click", "target": "covered", "expect": "declined"},
        {"id": "nested_scroll", "kind": "scroll", "target": "scroll-inner",
         "expect": "success"},
        {"id": "missing_target", "kind": "observe", "target": "no-such-control",
         "expect": "missing_target"},
    ]

    return {
        "version": FIXTURE_VERSION,
        "seed": seed,
        "viewport": {"w": 900, "h": 780},
        "tiny": tiny,
        "tiny_boxes": tiny_boxes,
        "save": save,
        "menu": menu,
        "rerender": rerender,
        "mover": mover,
        "overlay": overlay,
        "scroll": scroll,
        "targets": targets,
        "scenarios": scenarios,
    }


def layout_hash(spec: dict) -> str:
    payload = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Clickwarden fixture __SEED__</title>
<style>
  html,body{margin:0;background:#fafafa;font:14px "Segoe UI",sans-serif;position:relative}
  #stage{position:relative;width:100%;height:100%}
  button{position:absolute;font:12px "Segoe UI",sans-serif;padding:0;margin:0;
         border:1px solid #33455f;background:#dfe8ff;cursor:pointer;overflow:hidden}
  button.hidden{display:none}
  .scroller{position:absolute;overflow:auto;border:2px solid #33455f;background:#fff}
  .filler{height:600px;background:linear-gradient(#f1f1f1,#c9c9c9)}
  .spacer{height:26px}
  .overlay{position:absolute;box-sizing:border-box;background:rgba(200,0,0,.22);
           border:1px solid #c00000}
</style></head><body>
<div id="stage"></div>
<script>
const SPEC = __SPEC__;
const state = {version: SPEC.version, seed: SPEC.seed, page_id: Math.random().toString(36).slice(2),
               revision: 0, sequence: 0, counters: {}, events: [], scroll: {},
               menu_open: false, moved: false, rerendered: false,
               dpr: window.devicePixelRatio,
               viewport: [window.innerWidth, window.innerHeight],
               visual_scale: (window.visualViewport ? window.visualViewport.scale : null)};
let postTimer = null;
function post(){
  if (postTimer) return;
  postTimer = setTimeout(function(){
    postTimer = null;
    try { fetch('/state', {method:'POST', body: JSON.stringify(state)}).catch(function(){}); }
    catch (error) {}
  }, 40);
}
setInterval(post, 300);
function bump(){ state.revision++; }
function recordEvent(target, kind, extra){
  state.sequence++;
  state.revision++;
  const event = Object.assign({target: target, kind: kind,
                               t: Math.round(performance.now())}, extra || {});
  state.events.push(event);
  post();
  return event;
}
function recordClick(target, event){
  state.counters[target] = (state.counters[target] || 0) + 1;
  recordEvent(target, 'click', {x: event && event.clientX, y: event && event.clientY});
}
function place(el, box){
  el.style.left = box.x + 'px'; el.style.top = box.y + 'px';
  el.style.width = box.w + 'px'; el.style.height = box.h + 'px';
}
function button(item, extraClass){
  const el = document.createElement('button');
  el.setAttribute('data-target', item.id);
  el.setAttribute('aria-label', item.label);
  el.textContent = item.text;
  place(el, item);
  if (extraClass) el.className = extraClass;
  el.addEventListener('click', function(event){ recordClick(item.id, event); });
  document.getElementById('stage').appendChild(el);
  return el;
}
function scroller(item){
  const el = document.createElement('div');
  el.setAttribute('data-target', item.id);
  el.setAttribute('aria-label', item.label);
  el.setAttribute('role', 'region');
  el.className = 'scroller';
  place(el, item);
  el.addEventListener('scroll', function(){
    state.scroll[item.id] = el.scrollTop;
    recordEvent(item.id, 'scroll', {top: el.scrollTop});
  });
  document.getElementById('stage').appendChild(el);
  return el;
}
SPEC.tiny.forEach(function(item){ button(item); });
SPEC.save.forEach(function(item){ button(item); });

const menuButton = button(SPEC.menu.button);
const menuItems = SPEC.menu.items.map(function(item){
  const el = button(item); el.classList.add('hidden'); return el;
});
menuButton.addEventListener('click', function(){
  setTimeout(function(){
    menuItems.forEach(function(el){ el.classList.remove('hidden'); });
    state.menu_open = true; bump(); post();
  }, SPEC.menu.delay_ms);
});

const rerenderButton = button(SPEC.rerender.initial);
rerenderButton.addEventListener('click', function(){
  rerenderButton.remove();
  button(SPEC.rerender.replacement);
  state.rerendered = true; bump(); post();
});

const decoy = button(SPEC.mover.decoy);
const mover = button(SPEC.mover.box_a);
setTimeout(function(){
  place(mover, SPEC.mover.box_b);
  state.moved = true; bump(); post();
}, SPEC.mover.move_after_ms);

button(SPEC.overlay.covered);
const overlay = document.createElement('div');
overlay.setAttribute('data-target', SPEC.overlay.id);
overlay.setAttribute('aria-label', SPEC.overlay.label);
overlay.className = 'overlay';
place(overlay, SPEC.overlay.box);
overlay.addEventListener('click', function(event){ recordClick(SPEC.overlay.id, event); });
document.getElementById('stage').appendChild(overlay);

const outer = scroller(SPEC.scroll.outer);
const spacer = document.createElement('div'); spacer.className = 'spacer';
outer.appendChild(spacer);
const inner = scroller(SPEC.scroll.inner);
place(inner, {x: 0, y: 0, w: SPEC.scroll.inner.w, h: SPEC.scroll.inner.h});
inner.style.position = 'static';
outer.appendChild(inner);
[outer, inner].forEach(function(el){
  const filler = document.createElement('div'); filler.className = 'filler';
  el.appendChild(filler);
});
post();
</script></body></html>
"""


def render_page(spec: dict) -> str:
    """Embed the spec verbatim into the self-contained fixture page."""
    payload = json.dumps(spec, separators=(",", ":"), ensure_ascii=False)
    payload = payload.replace("</", "<\\/")
    return (_PAGE.replace("__SPEC__", payload)
                 .replace("__SEED__", str(spec["seed"])))
