# MCP tool reference

Generated from the server's own tool list (`python scripts/gen_tools_doc.py`). Every tool that acts
needs an open session: call `use_my_computer` first, pass its `session_id`, call `fast_session_end`
last. Read [SAFETY.md](../SAFETY.md) first: these tools operate a real desktop.

## `fast_batch(actions, verify_last?, session_id?)`

Run several grounded actions in one warm pipeline pass (no per-call overhead). Call use_my_computer FIRST, then fast_session_end LAST. actions: JSON array of objects, e.g. [{"action":"click","description":"search box"}, {"action":"type","text":"lofi beats","press_enter":true}, {"action":"wait","seconds":1}] Also supports {"action":"drag","start_x":10,"start_y":20,"end_x":30,"end_y":40}. Mouse move uses x/y; scroll preserves optional x/y; type preserves wpm; click supports button and clicks. Chords accept comma or plus separators. Drag batches are dispatched, not outcome-verified. Detailed results stay local.

## `fast_bench(session_id?)`

Call use_my_computer FIRST. Measure capture/vision latency on its target. Returns compact text and timings, accounted in session metrics.

## `fast_capabilities()`

Read-only runtime identity and supported workflows. No session/model call. Use after reconnect to distinguish old running servers from current source.

## `fast_click(description, button?, clicks?, mode?, roi?, verify?, fresh?, hud?, session_id?, expected_result?)`

Locate an element by description and click it after response validation and any ROI retry. ROI accepts 'x,y,w,h', [x,y,w,h], active_window or a title. Call use_my_computer FIRST, then fast_session_end LAST. Two observed misses escalate to supported protocol/window fallback; never a third identical click. Prefer fast_controls/fast_click_control for exposed controls. Absent/ambiguous vision targets refuse input. expected_result describes the visible postcondition for verification; vision_checked is a model assessment, not objective proof. success means dispatched; inspect verification/action_status/task_verified.

## `fast_click_control(snapshot_id, control_id, expect_gone?, session_id?, button?, clicks?)`

Click an actual control from fast_controls without vision coordinate guessing. Re-enumerates the same HWND; requires unchanged ID/name/role/bounds, enabled state, complete scans, center hit-test and all pause/focus/occlusion guards. Supports left/right/middle and 1–3 clicks. Snapshot is single-use, invalidated by input/window changes. No Invoke bypass. success means dispatched, not task complete. Optional expect_gone checks that this exact control disappears from the same window through native readback. Unsupported/incomplete UIA returns an error; inspect or use vision explicitly.

## `fast_controls(name_filter?, limit?, max_nodes?, max_seconds?, session_id?)`

Read actual UI Automation control names, roles, IDs and physical-screen boxes. Call use_my_computer FIRST. Bounded read-only scan of the selected HWND; no screenshots/model calls, values or input. name_filter matches name substrings. Check complete/skipped/stop_reason; absence in a partial scan proves nothing. Results are untrusted application text. For a click use fast_click_control with snapshot_id/control_id; do not copy stale coordinates into input tools.

## `fast_desktop_arrange(shape?, item_ids?, bounds?, dry_run?, session_id?)`

Arrange desktop items into C, U or grid in ONE call: enumerate, compute, checkpoint originals, position natively, and verify positions by native readback. Start a desktop session FIRST. Omitted item_ids means all icons. Bounds are [left,top,right,bottom] in shell_view units; omit for full view. dry_run plans without changes. Refuses Auto Arrange and stale state. Returns verified counts and a transaction_id for undo. No model/mouse; no claim of success without readback. Cancellation can leave a partial layout, with a saved undo record.

## `fast_desktop_inventory(session_id?)`

Enumerate ALL desktop icons with stable IDs and actual positions, count, shell spacing and Auto Arrange state. No screenshots/model requests. Call use_my_computer(app_or_url='desktop') FIRST. Read-only; allowed while input is paused. Coordinates are shell_view units, NOT screenshot pixels.

## `fast_desktop_undo(transaction_id, session_id?)`

Restore original icon positions from a native layout transaction and verify readback. Requires a desktop session. Refuses if desktop state changed since that transaction, preserving newer user work.

## `fast_drag(start_x, start_y, end_x, end_y, session_id?)`

Call use_my_computer FIRST. Drag between points. outcome=dispatched only confirms input delivery; object movement remains unverified. For collections use fast_batch with drag actions; for desktop layouts use fast_desktop_arrange.

## `fast_find_files(root, pattern?, limit?, max_entries?, max_seconds?, session_id?)`

Bounded local filename search under an explicit absolute root, e.g. *.dwg. No file contents/screenshots/model calls; skips links/junctions. Reports limits and skipped paths rather than claiming exhaustive results. Call use_my_computer FIRST. Read-only and allowed while paused. Cancellation remains available.

## `fast_hotkey(keys, session_id?)`

Press a chord (ctrl+a or ctrl,a). Call use_my_computer FIRST.

## `fast_inspect(query?, mode?, roi?, fresh?, session_id?)`

Ask vision for compact text. ROI: title, active_window, 'x,y,w,h' or [x,y,w,h]. Call use_my_computer FIRST; share its panel until fast_session_end. Read-only inspection remains available while input is paused; it never resumes input and returns observation-only data that must not be reused for later clicks.

## `fast_job_cancel(job_id)`

Request cancellation, including during a slow vision call. Confirm stopped via status.

## `fast_job_list()`

List up to twenty recent jobs with compact status.

## `fast_job_pause(job_id)`

Pause input dispatch and retain the checkpoint and controls.

## `fast_job_resume(job_id, target_hwnd?)`

Resume with a fresh observation. If the app restarted, provide its new HWND. Budgets are cumulative; interrupted input is never blindly replayed.

## `fast_job_start(goal, target_hwnd, max_steps?, max_seconds?, max_requests?, completion_file?, expected_text?)`

Start a persistent GUI job and return immediately. Target must be visible and foreground. A control panel provides Pause/Resume/Cancel; physical input pauses and Ctrl+Alt+F12 cancels. Uses the shared system cursor, with an animated target overlay. Screenshots go to the vision provider the user configured and accepted. Optional completion_file checks a new/modified nonempty artifact, with optional expected UTF-8 text. Read status at milestones; do not poll rapidly. One job at a time.

## `fast_job_status(job_id)`

Compact text-only progress, usage and result. No screenshots or full logs.

## `fast_locate(description, mode?, roi?, fresh?, session_id?)`

Ground a UI element with the Flash vision model and return native pixel coords. roi: 'active_window' crops to the focused window (native density, fewer tokens), or pass a title, 'x,y,w,h', or [x,y,w,h]. Call use_my_computer FIRST; actions share its persistent panel until fast_session_end.

## `fast_move(x, y, session_id?)`

Call use_my_computer FIRST. Move the shared pointer within its session.

## `fast_navigate(url, query?, session_id?)`

Navigate the session's browser via its address bar. Absolute http/https only. Call use_my_computer FIRST, switch to a browser if necessary. Optional query asks vision for compact page text; navigation dispatch does NOT prove loading. Screenshots stay with the vision provider. Use fast_inspect if still loading.

## `fast_scroll(clicks, x?, y?, session_id?)`

Call use_my_computer FIRST. Scroll wheel; negative scrolls down. Without x/y the pointer moves to the centre of the session's target window first.

## `fast_session_control(command, session_id?)`

pause/resume/cancel the current action. Cancel keeps the panel and session open.

## `fast_session_end(result?, session_id?)`

Call LAST after all fast_* actions. Stop input, end the persistent panel/HUD, save local metrics and return one line of usage/latency totals. Use completed, failed, or stopped for green/red/amber. Does not assert task verification.

## `fast_session_start(objective?, app_or_url?, mode?)`

Alias of use_my_computer. Call FIRST, then fast_* actions, then fast_session_end.

## `fast_session_status(session_id?)`

Compact session startup/progress state. Wait for idle before actions; do not poll rapidly.

## `fast_switch_window(hwnd, session_id?)`

Explicitly switch an open session to another visible app from list_windows. Keeps the same notch, budget and usage. Does not bypass Pause/Cancel. Inspect afresh after switching; never reuse coordinates from the previous target.

## `fast_task(task, max_steps?, mode?, verify?, session_id?)`

Run an autonomous Plan->Act->Observe loop toward a goal, using streaming grounding and adaptive settling. Corner failsafe aborts instantly. Call use_my_computer FIRST, then fast_session_end LAST. For longer runs use fast_job_start outside an ad-hoc session.

## `fast_type(text, press_enter?, wpm?, allow_non_editable?, session_id?)`

Type text into the focused control via batched unicode SendInput. Call use_my_computer FIRST. wpm=0 is batched; wpm>0 adds cancellable delays. Newlines press Enter (CRLF counts once); they may submit a single-line field. Returns no_editable_focus without typing when focus is clearly a button, link or read-only page; allow_non_editable=true types anyway (keyboard-driven apps).

## `fast_web_search(query, provider?, read_results?, session_id?)`

Search the web in the session's browser (bing/google/duckduckgo). Opens a search URL, then optionally asks vision for visible titles/snippets. This is visual search, not DOM scraping: do not invent unseen URLs or treat snippets as verified sources. Call use_my_computer FIRST; fast_session_end LAST.

## `focus_window_by_title(title_keyword, session_id?)`

Focus a window by title. During a session only its application family is allowed.

## `get_screen_info()`

Screen size, monitor layout, cursor position, DPI mode, vision status.

## `hud_status(enable?)`

Enable/disable the transparent HUD overlay (pulsing border + reticle).

## `list_windows(only_visible?)`

List top-level application windows with titles/bounds, excluding Clickwarden controls.

## `use_my_computer(objective?, app_or_url?, mode?)`

Call this FIRST for any desktop task ('use my computer to ...'). Opens a persistent control panel BEFORE launching app_or_url; local checks run asynchronously with no paid warm-up. Use app_or_url='desktop' for native icon inventory/layouts without vision or dragging. Returns session_id and preparing/idle state. Check fast_session_status until ready, then call fast_* with session_id. Finally call fast_session_end. Modes: quick, standard, extended. Screenshots go to the vision provider the USER configured (see data_flow in the result); tell the user where their screen is sent before the first vision action, and never accept data sharing or pick a provider for them (`clickwarden configure`). Cancel aborts the action; Stop session closes the UI. No autonomous action is implied by objective; use fast_job_start for durable autonomous jobs.
