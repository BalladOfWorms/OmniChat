# OmniChat
Standalone chat panel from OmniWatch
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

## Auto-translate in the composer

Wrap a phrase in curly braces — `Need help? {Yes, please.}` — and it
sends as the real in-game auto-translate token. The **{ } button** in
the composer row does the wrapping for you: with text typed, one
click wraps the whole message (click again to unwrap); with an empty
field it inserts `{}` and parks the cursor inside so you just type
the phrase. Either way the message sends as the token (visible to JP players
in Japanese, exactly like using Tab in the native chat). Matching is
case-insensitive against the full auto-translate list, English or
Japanese names. Incoming auto-translate already renders as `{phrase}`
in the panel, so you can copy any phrase you see straight into the
composer. Text in braces that isn't a real phrase is sent literally.

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
```

## Porting fixes between OmniWatch and OmniChat

The `chat/` Lua modules are deliberately kept byte-identical between
the two addons — a fix in one ports to the other by copying the file.
The Python side shares its lineage with `OmniWatch.py`'s chat sections
(routing, classifiers, wrap/render, composer, draw_chat_panel), so
diffs port across with at most cosmetic adjustment. The lua→python
wire format is unchanged from OmniWatch's.

## Multiboxing

`chat/drain.lua` tags every datagram with `@CharName@`. When two
characters are logged in, the overlay pins chat to the character named
in the `chat_main_char` setting (edit `omnichat_settings.json`); when
unset, it follows whichever character's JOB heartbeat arrived most
recently.
