# OmniChat

Standalone FFXI chat panel — OmniWatch's chat subsystem extracted into
its own Windower addon + pygame overlay. Same routing rules, filter
engine, tabs, composer, and rendering as OmniWatch's chat panel, with
its own config folder and UDP ports so it runs **alongside** OmniWatch
(or entirely without it).

## Components

| File | Role |
|---|---|
| `OmniChat.lua` | Windower addon: packet/text capture → UDP event stream |
| `chat/*.lua` | The 8 chat modules — **byte-identical to OmniWatch's `chat/` folder** |
| `OmniChat.py` | pygame overlay: the chat panel window |
| `omnichat_routing_gui.py` | Routing/filter config editor (pygame-ce) |

## Install

1. Create `Windower/addons/OmniChat/` and put `OmniChat.lua` there.
2. (Optional) Copy the `chat/` folder from your OmniWatch addon
   directory into `Windower/addons/OmniChat/chat/`. If you skip this,
   OmniChat automatically loads the modules straight from the sibling
   `addons/OmniWatch/chat/` folder — the files are byte-identical, so
   sharing them is the normal setup. A local copy, when present, takes
   priority (useful for testing module changes on one addon only).
3. Put `OmniChat.py` and `omnichat_routing_gui.py` anywhere (the same
   folder is easiest — the panel's gear button looks for the GUI
   alongside itself, `.exe` first, then `.py`).
4. `//lua load omnichat` in-game, run `OmniChat.py`.

On first launch the overlay imports any existing
`%APPDATA%/OmniWatch/omniwatch_chat_routing*.json` into
`%APPDATA%/OmniChat/` (renamed), so your routing rules and per-job
filters carry over unchanged. Nothing is ever copied back — the two
installs stay independent after that.

## Ports (no collisions with OmniWatch's 5000–5015 / 5054 / 5061)

- **5113** — lua → python: CHAT_BATCH event stream (same wire format
  as OmniWatch; see `chat/drain.lua` header) plus a 1 Hz
  `JOB\t<job>\t<char>` heartbeat that drives per-job routing reloads
  and multibox chat pinning.
- **5111** — python → lua: bare windower console commands (the
  composer sends `input /p hello`, the name context menu sends
  `input /pcmd add Name`).

## Window model

The OS window *is* the chat panel (borderless, always-on-top by
default). Drag the top header strip — or Shift+drag anywhere — to
move it. Resize from any of the four edges (single axis each; the
left and top edges move the window so the opposite edge stays put)
or the bottom-right corner grip (both axes) — the cursor changes
over each zone. `Ctrl+Q` quits (there's no title bar). Position and
size persist across sessions.

The header's **Options** button opens a popup with:

- **Theme** — Dark or Light. Light mode swaps the chrome to light
  grays and darkens every channel color to stay readable.
- **Always on top** toggle.
- **Box opacity** (20–100%) — background translucency only; text
  stays fully solid at every level. Implemented as a layered-window
  color key plus an ordered-dither background, so the game shows
  through the box while glyphs remain crisp.
- **UI scale** (50–200%) — scales all panel text at once.
- **Font size** (Small/Medium/Large) and **message composer** on/off.
- **Focus words/phrases** (defined in the Filters GUI's footer) —
  when a chat line in any channel contains one of your phrases, the
  word itself gets a pulsing amber shade for ~12 seconds, then a
  steady faint shade so it stays findable when scrolling back.
  Matching is case-insensitive substring ("kraken club" catches
  "Selling KRAKEN CLUB 100g"). Inactive tabs holding an unseen hit
  pulse their label until you visit them. Phrases save into
  `_meta.focus_phrases` in the routing JSON; the overlay watches the
  file and applies Filters-GUI saves live within ~2 seconds — no
  restart needed (this live reload covers ALL routing edits, not
  just focus phrases).
- **Keep game focus** (default ON) — clicking tabs, buttons, and the
  scrollbar never takes keyboard focus away from FFXI (the window uses
  the no-activate style). Clicking into the composer text field is the
  one exception: it borrows focus so you can type, and hands it back
  to the game the moment you send (Enter) or cancel (Esc). Mouse-wheel
  scrolling over the unfocused panel works on Windows 10/11 ("scroll
  inactive windows", on by default).
- **Type anywhere** (default ON) — type into the composer without
  clicking the panel at all; see the section below. With this on you
  rarely need to click into the composer — the game keeps focus the
  whole time.
- **Exit OmniChat** — the borderless window has no [X]; this and
  Ctrl+Q are the two ways out.

All of it persists across sessions.

Everything else behaves exactly like the OmniWatch chat panel: tab
strip with unread badges and overflow arrows, right-click a tab to
hide it, per-tab scroll with scrollbar + jump-to-bottom badge,
Clear Tab / Clear All / Show-all-tabs / Filters (gear) header buttons,
right-click the header strip to dump the routing diagnostic,
left-click a sender name to set up a /tell, right-click for the
Tell / Invite / Blacklist context menu, and the composer row with
channel cycling and slash-command escape.

## NPC dialog "continue" arrow

While your character is in a cutscene or NPC dialog (the game's Event
status), a small pulsing **▼** appears at the end of the last chat
line — the panel's version of the game's blinking continue cursor.
There's more to read; hit Enter in the game to advance. The arrow
follows the character the panel is pinned to, only draws at the live
bottom of the scrollback, and disappears a few seconds after the
dialog ends.

## Type anywhere (global typing)

With the **Type anywhere** toggle ON (Options popup), every keystroke
you make *while the game window is focused* goes into OmniChat's
composer instead of the game — no clicking the panel, no focus change:

- **Enter** sends the line and keeps capturing (rapid-fire several
  lines in a row).
- **Esc** clears the current draft and keeps capturing.
- Alt-tabbing or clicking another app pauses capture automatically
  (your draft is preserved); refocusing the game resumes it.
- The **toggle is the only off switch** — flip it OFF to play
  normally and type in-game; the composer then works click-to-type
  as before.

The trade is deliberate: while the toggle is ON you can't use FFXI's
own keyboard (its `/` chat, keyboard hotbars, movement keys) — the
toggle is your instant way back.

> **Run OmniChat as administrator if Windower runs elevated.**
> Windows (UIPI) silently blocks a normal-privilege keyboard hook
> from seeing input destined for an elevated window: the feature
> reports ready but captures nothing. OmniChat detects the mismatch
> and prints an advisory in the session log. Either elevate OmniChat
> too (Properties → Compatibility → "Run this program as an
> administrator" makes it permanent), or run Windower un-elevated.


## In-game commands

```
//oc status            pipeline health (capture / drain / sockets)
//oc ping              push a test event to the overlay's System tab
//oc dump [N]          show last N captured events (default 20)
//oc reset             clear the chat history ring
//oc debug [on|off]    unified diagnostics (probe logs under OmniChat/data/)
//oc condense [on|off] condensed multi-hit melee/AoE display
//oc class <id>        classify a mob id
```

## Filters GUI window behavior

The routing/filters GUI opens **always-on-top** (OmniChat's own window is
topmost, so a normal window would land invisibly behind it) and
**remembers its position** — move it where you like and it reopens there
next time (saved to `omnichat_gui_state.json` on close).

## Config files (`%APPDATA%/OmniChat`, or `~/.omnichat` off-Windows)

```
omnichat_settings.json            window pos/size, hidden tabs, font size,
                                  chat main character (multibox pin)
omnichat_chat_routing.json        global routing config
omnichat_chat_routing-<JOB>.json  per-job overrides
logs/session_*.log                console output of windowed builds
                                  (rotating, last 5 kept)
```

## Porting fixes between OmniWatch and OmniChat

The `chat/` Lua modules are deliberately kept byte-identical between
the two addons — a fix in one ports to the other by copying the file.
The Python side shares its lineage with `OmniWatch.py`'s chat sections
(routing, classifiers, wrap/render, composer, draw_chat_panel), so
diffs port across with at most cosmetic adjustment. The lua→python
wire format is unchanged from OmniWatch's.

## Multiboxing

`chat/drain.lua` tags every datagram with `@CharName@`, and the overlay
**pins to one character** so the same /say overheard by two logged-in
clients doesn't appear twice. Switch the pin with the **Pin character**
stepper in the Options popup: it cycles **Auto (latest login)** and
every character seen this session. Picking one pins chat to that
character instantly and persists (`chat_main_char` in
`omnichat_settings.json`); Auto follows whichever character's heartbeat
arrived first after the overlay started.

Two things to know:
- **The composer is separate from the pin.** Typed sends go out through
  whichever game client bound port 5111 first (normally the first one
  launched). If you reload the addon on another box, sends can migrate
  there while the pin stays put.
- Tells received by a **non-pinned** character don't appear; switch the
  pin (or check that client's native log) to read them.