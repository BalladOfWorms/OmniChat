# Changelog

All notable changes to OmniChat are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/), with versions following [semver](https://semver.org/).

## [1.0.1] — 2026-06-10

A multiboxing quality-of-life release. The headline is a **character dropdown** in the panel header — see at a glance whose chat the panel is pinned to, and switch boxes with one click instead of editing JSON. The routing/filters GUI also stopped hiding behind the overlay and now remembers where you put it.

### Added

- **Character dropdown (multibox pin switcher)** — a header button (after Clear All) showing which character's chat the panel is pinned to. Click it for a dropdown listing **every character seen this session** (the overlay tracks senders even while their chat is gated out) plus an **Auto (latest login)** entry. Picking a name pins chat to that character **instantly** and persists as `chat_main_char` in `omnichat_settings.json` — no more hand-editing the file. The button renders amber when an explicit pin is active and neutral when following Auto; the dropdown checkmarks the active choice. On a single box it simply shows your character's name, doubling as an identity badge.

- **Pin character row in Options** — the same multibox pin switcher, as a stepper inside the Options popup (between Font size and Message composer): step through Auto and every character seen this session. Long names ellipsize to fit.

### Fixed

- **Options/character buttons didn't close their own popups** — clicking the Options button (or the character button) while its popup was open dismissed the popup and immediately reopened it in the same click, so the buttons appeared to only ever open. The popups now consume clicks landing on their own toggle button.
- **Filters GUI opened invisibly behind the overlay** — OmniChat's window is always-on-top, so the routing GUI (a normal window) rendered underneath it even when focused. The GUI now sets itself always-on-top at launch and layers above the overlay.
- **Filters GUI forgot its window position** — it now saves its position on close (`omnichat_gui_state.json`) and reopens exactly where you left it, restored via SDL before the window is created so there's no visible jump. Saved positions are sanity-bounded so a stale save from an unplugged monitor can't strand the window off-screen, while negative coordinates still work for left-of-primary monitors.

### Internal

- The multibox gate records senders before applying the pin filter, which is what lets the dropdown list characters whose chat is currently being dropped. Heartbeats from the newly pinned character pass the gate immediately, so per-job routing follows the switch within a second.

## [1.0.0] — 2026-06-10

Initial public release. A standalone chat overlay for FFXI (Windower): a Lua addon streams chat to a Python/pygame panel with per-channel tabs, routing rules, a rules-based filter engine with sender blacklist and focus-word highlighting, per-category window routing, a message composer with auto-translate sending (`{phrase}` syntax + `{ }` wrap button), dark/light themes, box opacity with solid text, 4-side window resizing, keep-game-focus mode, and a standalone routing/filters GUI.