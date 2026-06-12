# Changelog

All notable changes to OmniChat are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/), with versions following [semver](https://semver.org/).

## [1.0.4] — 2026-06-12

### Fixed

- **Type-anywhere hook thread crashed on startup (captured nothing)** — the thread fetched GetCurrentThreadId from user32 instead of kernel32, raising AttributeError right after the "ready" line and dying; with no message pump on the owning thread, the keyboard hook never received a callback and every key passed through to the game while the log looked healthy. The lookup now targets kernel32, and all other DLL→function mappings in the hook path were audited clean.

## [1.0.3] — 2026-06-12

### Added

- **Type anywhere (global typing)** — an Options toggle that lets you type into OmniChat *without ever clicking the panel*. While it's ON and the game window has focus, every keystroke routes straight into the composer and is withheld from the game: **Enter sends** (and keeps capturing, so you can fire off several lines), **Esc clears the current draft** (and keeps capturing — the toggle is the only off switch). Capture auto-pauses when the game loses focus (alt-tab, clicking another app) with your draft preserved, and resumes when the game is focused again. Toggle OFF and the feature goes dormant — click the input box to type, exactly as before. Built on a low-level keyboard hook that never steals focus from the game.
  - **Run OmniChat as administrator if Windower is elevated.** Windows (UIPI) silently blocks a normal-privilege keyboard hook from seeing input destined for an elevated window — the hook installs and reports ready, but captures nothing. OmniChat detects the mismatch at startup and prints an advisory in the session log. Either run OmniChat elevated too, or run Windower un-elevated.
- **Session logs for windowed builds** — frozen `--noconsole` builds have no console, so all diagnostics now go to rotating session logs under `<config>/logs/session_*.log` (the 5 most recent are kept). Startup failures that previously died silently — like another instance already holding the chat port — now also raise a native error dialog.
- **Casting-interruption routing (per actor)** — FFXI broadcasts "<Name>'s casting is interrupted." for every caster in range, which buried other players' interrupts in the System tab. These lines now ride a single **"Casting interrupted"** channel classified by who the caster actually is (Self / Party / Alliance / Mob / Other — multibox-aware, so your alts count as Self), and each actor's row in the Filters GUI controls it independently. Defaults: **your own interruptions show in Battle**, every other actor class is hidden. Want stun confirmations on bosses? Enable the row under Mob. Want a party member's interrupts? Enable it under Party.
- **NPC dialog "continue" arrow** — while your character is in a cutscene or NPC dialog (FFXI's Event status), the chat panel shows a small pulsing **▼** at the end of the last line — the panel's version of the game's blinking continue cursor, so you know there's more to read and an Enter is owed in-game. Multibox-aware: each client reports its own state, and the arrow follows the character the panel is pinned to. It only draws at the live bottom (scrolled up = reading history) and fades out a few seconds after the dialog ends.

### Fixed

- **Window opened minimized/off-screen and couldn't be recovered** — exiting while minimized persisted Windows' minimized-position sentinel (−32000, −32000) as the saved window position, so every later launch placed the window invisibly off-screen. Position saves now reject minimized/off-screen coordinates (keeping the last good position instead), restores discard any saved position that isn't on a real monitor (multi-monitor aware), and startup explicitly un-minimizes and shows the window as a final backstop.
- **Keyboard input dead system-wide after closing OmniChat** — the type-anywhere keyboard hook wasn't released on exit, so a lingering hook could keep swallowing keystrokes in *every* application until Windows timed it out (switching windows happened to jostle it loose). The hook is now cleanly released on every exit path (explicit teardown before quit plus an `atexit` backstop), and the instant shutdown begins the hook passes all keys through, so there is no window where input can be eaten during exit.
- **64-bit pointer truncation in the keyboard hook** — the hook's Win32 calls had no ctypes type declarations, so handles and pointers defaulted to 32-bit ints. Full signatures are now declared for every call in the hook path.

### Removed

- **Auto-translate sending** — the 1.0.0 feature that converted `{phrase}` composer text into in-game auto-translate tokens never worked reliably across delivery mechanisms and has been removed (along with the composer's `{ }` wrap button). Incoming auto-translate still renders as readable `{phrase}` text in the panel; typed braces now simply send as literal text, matching what actually happened anyway.

## [1.0.2] — 2026-06-10

### Added

- **Pin character switcher (multibox)** — a **Pin character** stepper in the Options popup (between Font size and Message composer): step through **Auto (latest login)** and **every character seen this session** (the overlay tracks senders even while their chat is gated out). Picking one pins chat to that character **instantly** and persists as `chat_main_char` in `omnichat_settings.json` — no more hand-editing the file. Long names ellipsize to fit; heartbeats from the newly pinned character pass the gate immediately, so per-job routing follows the switch within a second.

### Fixed

- **Options/character buttons didn't close their own popups** — clicking the Options button (or the character button) while its popup was open dismissed the popup and immediately reopened it in the same click, so the buttons appeared to only ever open. The popups now consume clicks landing on their own toggle button.

### Internal

- The multibox gate records senders before applying the pin filter, which is what lets the stepper list characters whose chat is currently being dropped.

## [1.0.1] — 2026-06-10

### Fixed

- **Filters GUI opened invisibly behind the overlay** — OmniChat's window is always-on-top, so the routing GUI (a normal window) rendered underneath it even when focused. The GUI now sets itself always-on-top at launch and layers above the overlay.
- **Filters GUI forgot its window position** — it now saves its position on close (`omnichat_gui_state.json`) and reopens exactly where you left it, restored via SDL before the window is created so there's no visible jump. Saved positions are sanity-bounded so a stale save from an unplugged monitor can't strand the window off-screen, while negative coordinates still work for left-of-primary monitors.

## [1.0.0] — 2026-06-10

Initial public release. A standalone chat overlay for FFXI (Windower): a Lua addon streams chat to a Python/pygame panel with per-channel tabs, routing rules, a rules-based filter engine with sender blacklist and focus-word highlighting, per-category window routing, a message composer, dark/light themes, box opacity with solid text, 4-side window resizing, keep-game-focus mode, and a standalone routing/filters GUI.