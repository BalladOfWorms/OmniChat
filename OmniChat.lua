-- OmniChat — standalone chat panel addon for Windower 4 / FFXI.
--
-- Extracted from OmniWatch's chat subsystem. Captures FFXI chat,
-- combat, and status events from packets + text events, synthesizes
-- colored chat lines via the chat/ module set, and streams them over
-- UDP to the OmniChat.py pygame overlay.
--
-- Runs ALONGSIDE OmniWatch: distinct UDP ports (no collisions).
--   lua → python  : 127.0.0.1:5113  (CHAT_BATCH event stream + JOB line)
--   python → lua  : 127.0.0.1:5111  (composer "input /p hi" commands)
-- (OmniWatch occupies 5000-5015 / 5054 / 5061; OmniChat stays clear.)
--
-- Module layout mirrors OmniWatch's chat/ folder exactly — the eight
-- submodules under chat/ are byte-identical to OmniWatch's, so fixes
-- can be ported between the two addons by copying files:
--   chat/_loader.lua            module wiring + public API
--   chat/classifier.lua         entity → category classifier
--   chat/ring.lua               bounded ring buffers
--   chat/emit.lua               incoming-text capture
--   chat/chat_packets.lua       0x017 / 0x0CC real-chat packets
--   chat/battle_events.lua      0x028 combat synthesis
--   chat/buff_events.lua        buff/debuff apply + wear synthesis
--   chat/checkparam_events.lua  /checkparam 0x029 capture
--   chat/drain.lua              ring → UDP CHAT_BATCH encoder
--
-- Commands (//omnichat or //oc):
--   //oc dump [N]      show last N captured events (default 20)
--   //oc reset         clear the chat history ring
--   //oc debug [on|off] unified chat diagnostics (probe logs)
--   //oc condense [on|off] condensed multi-hit melee/AoE display
--   //oc class <id>    classify a mob id (diagnostic)

_addon.name     = 'OmniChat'
_addon.author   = 'BalladOfWorms'
_addon.version  = '1.0.3'
_addon.commands = {'omnichat', 'oc'}

local socket = require('socket')

-- Windower libs the chat modules depend on. THIS WAS THE "no chat"
-- BUG: chat/battle_events.lua and chat/buff_events.lua use T{...}
-- table constructors and :contains() from Windower's tables library.
-- In OmniWatch these globals exist as a side effect of its lib
-- requires; without them here, chat/_loader.lua failed on
-- buff_events.lua ("attempt to call global 'T'") and the whole chat
-- module stayed nil — so nothing was ever captured or sent.
require('tables')
require('strings')

-- Registers string:unpack() / string:pack(), used by the 0x076
-- party-buff handler below (same dependency OmniWatch.lua declares).
local packets = require('packets')

-- Global (deliberately not local) — chat/battle_events.lua and
-- chat/buff_events.lua read _G.res for spell/ability/buff name
-- resolution, same as in OmniWatch.lua.
res = require('resources')

-- ── UDP sockets ───────────────────────────────────────────────────────────
-- Event stream to the Python overlay. Same CHAT_BATCH wire format as
-- OmniWatch (see chat/drain.lua header), new port so both addons'
-- overlays can run at once.
local udp_chat = socket.udp()
udp_chat:setpeername('127.0.0.1', 5113)

-- Inbound command listener (composer sends, /pcmd add from the name
-- context menu, etc.). Bare-datagram protocol: the whole payload is a
-- windower console command, e.g. "input /p hello" → send_command.
-- Bind is best-effort; if something else holds 5111 the composer
-- silently no-ops but capture keeps working.
local udp_cmd_in = nil
do
    local s = socket.udp()
    s:settimeout(0)
    local ok_bind, err_bind = s:setsockname('127.0.0.1', 5111)
    if ok_bind then
        udp_cmd_in = s
    else
        windower.add_to_chat(123,
            '[OmniChat] could not bind inbound 5111: ' .. tostring(err_bind))
    end
end

-- ── Crash-safe event registration ───────────────────────────────────────
-- Drop-in replacement for windower.register_event that catches errors
-- thrown from the callback, logs them with traceback to
-- logs/crash_YYYY-MM-DD.log, and keeps the addon running. A single
-- field rename in a Windower update must not kill chat capture.
local function _crash_log(err)
    local msg = '[OmniChat] handler error: ' .. tostring(err)
    windower.add_to_chat(123, msg)
    local ok = pcall(function()
        local base = windower.addon_path or ''
        if base ~= '' and base:sub(-1) ~= '/' and base:sub(-1) ~= '\\' then
            base = base .. '/'
        end
        windower.create_dir(base .. 'logs')
        local f = io.open(base .. 'logs/crash_'
                          .. os.date('%Y-%m-%d') .. '.log', 'a')
        if f then
            f:write(os.date('[%H:%M:%S] ') .. tostring(err) .. '\n')
            if debug and debug.traceback then
                f:write(debug.traceback() .. '\n')
            end
            f:close()
        end
    end)
end

local function oc_safe_register(event_name, fn)
    return windower.register_event(event_name, function(...)
        local args = {...}
        local ok, err = pcall(function() return fn(unpack(args)) end)
        if not ok then _crash_log(err) end
    end)
end

-- ── Chat module load ──────────────────────────────────────────────────────
-- Same loadfile-with-absolute-path pattern OmniWatch uses (require is
-- avoided due to Windows filename casing quirks with package.path).
--
-- Source resolution, in order:
--   1. <OmniChat>/chat/_loader.lua          (a local copy)
--   2. <addons>/OmniWatch/chat/_loader.lua  (borrow OmniWatch's — the
--      modules are byte-identical between the two addons, so when no
--      local copy exists we just use OmniWatch's directly; no manual
--      folder copying needed)
-- The fallback temporarily points windower.addon_path at the OmniWatch
-- addon root while the loader chunk runs, because chat/_loader.lua
-- resolves its submodule paths from windower.addon_path. Restored
-- immediately after, so everything else (logs, data/) stays under
-- OmniChat's own folder.
local _chat = nil
do
    local base = windower.addon_path or ''
    if base ~= '' and base:sub(-1) ~= '/' and base:sub(-1) ~= '\\' then
        base = base .. '/'
    end

    -- Sibling OmniWatch addon root: strip the trailing "OmniChat/"
    -- component (either separator style) and append "OmniWatch/".
    local parent = base:match('^(.*[/\\])[^/\\]+[/\\]$') or base
    local ow_base = parent .. 'OmniWatch/'

    local function try_load(root, label)
        local loader_path = root .. 'chat/_loader.lua'
        local chunk, load_err = loadfile(loader_path)
        if not chunk then
            return nil, load_err
        end
        -- Run with windower.addon_path pointed at `root` so the
        -- loader's submodule paths resolve under the right chat/.
        local saved = windower.addon_path
        windower.addon_path = root
        local ok_run, mod_or_err, mod_err2 = pcall(chunk)
        windower.addon_path = saved
        if ok_run and type(mod_or_err) == 'table' then
            windower.add_to_chat(207, string.format(
                '[OmniChat] chat modules loaded from %s', label))
            return mod_or_err, nil
        end
        return nil, tostring(mod_err2 or mod_or_err)
    end

    local err_local, err_ow
    _chat, err_local = try_load(base, 'OmniChat/chat/')
    if not _chat then
        _chat, err_ow = try_load(ow_base, 'OmniWatch/chat/ (shared)')
    end
    if not _chat then
        windower.add_to_chat(123,
            '[OmniChat] chat module FAILED to load — no chat will be captured.')
        windower.add_to_chat(123,
            '[OmniChat]   tried OmniChat/chat/: ' .. tostring(err_local))
        windower.add_to_chat(123,
            '[OmniChat]   tried OmniWatch/chat/: ' .. tostring(err_ow))
        windower.add_to_chat(123,
            '[OmniChat] Fix: copy the chat/ folder from your OmniWatch '
            .. 'addon into the OmniChat addon folder, then //lua reload omnichat.')
    end
    -- Make sure the data/ dir the probe logs + trace logs write into
    -- exists (chat/*.lua open files under <addon>/data/).
    pcall(function() windower.create_dir(base .. 'data') end)
end

-- ── Shared state ─────────────────────────────────────────────────────────
_ow_chat_debug = false             -- read by chat/emit.lua diagnostics
local _oc_debug_on = false         -- unified //oc debug toggle state

-- Substitution store for outgoing auto-translate phrases. The
-- outgoing-text handler records the resolved AT phrase body keyed by
-- mode; chat/emit.lua's own-echo path (which reads the GLOBAL
-- _ow_own_outgoing_suppress — same name as in OmniWatch so emit.lua
-- stays byte-identical) swaps the mangled echo body for this resolved
-- text. Short TTL inside emit.lua.
_ow_own_outgoing_suppress = _ow_own_outgoing_suppress or {}

-- Recent-action-name cache for the "?" text fix: the FFXI client
-- renders "<Actor> uses ?" when it can't resolve an ability id; the
-- 0x028 handler resolves the id via resources and caches it here so
-- the incoming-text handler can substitute the real name.
--   _oc_recent_action_name[actor_id] = {name=<str>, ts=<os.clock>}
local _oc_recent_action_name = {}
local _OC_ACTION_NAME_TTL = 10.0   -- seconds

-- 0x076 party-buff snapshots, keyed by player id. Used to diff for
-- party-member buff LOSSES (FFXI only sends 0x029 wear-offs for the
-- local player).
local party_buffs = {}

-- 0x063 sub-9 self-buff snapshot (multiset of buff-id counts). nil
-- forces a capture-without-emit baseline on the next packet.
local _oc_self_buff_prev = nil

-- ── 0x028 action-name cache (the "?" fix) ────────────────────────────────
local function _oc_cache_action_name(act)
    if not act or not act.actor_id then return end
    local cat = act.category
    if not cat then return end
    local tgt    = act.targets and act.targets[1]
    local action = tgt and tgt.actions and tgt.actions[1]
    local name

    if cat == 3 then
        local ws = act.param and res.weapon_skills
                   and res.weapon_skills[act.param]
        name = ws and (ws.en or ws.name)
    elseif cat == 4 then
        local sp = act.param and res.spells and res.spells[act.param]
        name = sp and (sp.en or sp.name)
    elseif cat == 8 then
        -- Spell begin: id is in the per-target action param, not
        -- act.param (which is a cast-animation id).
        local sid = (action and action.param) or act.param
        local sp = sid and res.spells and res.spells[sid]
        name = sp and (sp.en or sp.name)
    elseif cat == 6 then
        -- Job ability OR item use (both arrive on cat 6).
        local ja = act.param and res.job_abilities
                   and res.job_abilities[act.param]
        name = ja and (ja.en or ja.name)
        if not name then
            local it = act.param and res.items and res.items[act.param]
            name = it and (it.en or it.english)
        end
    elseif cat == 11 or cat == 13 then
        local ab = act.param and res.monster_abilities
                   and res.monster_abilities[act.param]
        name = ab and (ab.en or ab.name)
    end

    if name and name ~= '' then
        _oc_recent_action_name[act.actor_id] = {name = name, ts = os.clock()}
    end
end

-- ── 0x028 / 0x029 — action packets ───────────────────────────────────────
-- Battle synth FIRST, then buff/debuff synth: a mob TP move emits a
-- "uses <ability>" readies line (battle synth) AND a "gains the effect
-- of <buff>" line (buff synth) from the same packet, and the readies
-- line must land in the ring first so the display reads in causal
-- order. Both pcall'd — a malformed packet can't break the chain.
local function handle_incoming_action(act)
    if not act or not act.targets or not act.targets[1] then return end

    pcall(_oc_cache_action_name, act)

    if _chat and _chat.process_battle_action then
        pcall(_chat.process_battle_action, act)
    end
    if _chat and _chat.process_action then
        pcall(_chat.process_action, act)
    end
end

oc_safe_register('incoming chunk', function(id, data)
    if id == 0x028 then
        local ok, parsed = pcall(windower.packets.parse_action, data)
        if ok and parsed then
            local ok2, err2 = pcall(handle_incoming_action, parsed)
            if not ok2 and _ow_chat_debug then
                windower.add_to_chat(123, string.format(
                    '[OmniChat] action handler error: %s', tostring(err2)))
            end
        end
    elseif id == 0x029 then
        -- Manually unpack bytes to avoid depending on string:unpack
        -- (availability varies with load order / environment).
        -- Windower packet offsets are 1-indexed from the packet header,
        -- matching Lua's 1-indexed string.byte.
        local function u32(s, offset)
            local b1 = s:byte(offset)     or 0
            local b2 = s:byte(offset + 1) or 0
            local b3 = s:byte(offset + 2) or 0
            local b4 = s:byte(offset + 3) or 0
            return b1 + b2*256 + b3*65536 + b4*16777216
        end
        local function u16(s, offset)
            local b1 = s:byte(offset)     or 0
            local b2 = s:byte(offset + 1) or 0
            return b1 + b2*256
        end
        local target_id  = u32(data, 0x09)
        local param_1    = u32(data, 0x0D)
        local param_2    = u32(data, 0x11)
        local message_id = u16(data, 0x19) % 32768

        -- /checkparam capture (msg 712-715, 731, 733) → System tab.
        -- Cheap no-op for every other message id.
        if _chat and _chat.process_action_message then
            pcall(_chat.process_action_message,
                  message_id, target_id, param_1, param_2)
        end

        -- Status wear-off capture (msg 64, 204, 206, 350, 531) →
        -- Buffs/Debuffs/Mob routing. Natural wear-offs never fire
        -- 0x028 action packets; this is the only source for them.
        if _chat and _chat.process_status_message then
            pcall(_chat.process_status_message,
                  message_id, target_id, param_1)
        end
    end
end)

-- ── 0x076 / 0x063 / 0x017 / 0x0CC — buff diffs + real chat ───────────────
oc_safe_register('incoming chunk', function(id, original)
    -- ── 0x076: party buff snapshots → member buff-LOSS detection ────
    -- FFXI only fires a 0x029 wear-off action-message for the LOCAL
    -- player; a party/alliance member dropping a buff is reflected
    -- ONLY in these periodic snapshots. Diff each member's previous
    -- buff multiset against the new one; for each buff whose count
    -- dropped, synthesize a "loses X" chat event.
    if id == 0x076 then
        local me = windower.ffxi.get_player()
        local my_id = me and me.id or 0
        for k = 0, 4 do
            local playerId = original:unpack('I', k*48+5)
            if playerId ~= 0 then
                local buffs = {}
                for i = 1, 32 do
                    local buff = original:byte(k*48+5+16+i-1) + 256*(
                        math.floor(
                            original:byte(k*48+5+8 + math.floor((i-1)/4)) / 4^((i-1)%4)
                        ) % 4
                    )
                    if buff ~= 255 then
                        table.insert(buffs, buff)
                    end
                end

                -- Probe dump (gated inside buff_events on debug_apply).
                if _chat and _chat.debug_party_member_dump then
                    _chat.debug_party_member_dump(playerId, buffs)
                end

                -- Guards:
                --   * Skip the local player — their losses already come
                --     via 0x029. Diffing here too would double-emit.
                --   * Skip when there's no PRIOR snapshot (first sight /
                --     post-zone): absent→present isn't a loss.
                if playerId ~= my_id and _chat
                   and _chat.process_party_buff_loss then
                    local prev = party_buffs[playerId]
                    if type(prev) == 'table' then
                        pcall(function()
                            local old_ct, new_ct = {}, {}
                            for _, b in ipairs(prev) do
                                old_ct[b] = (old_ct[b] or 0) + 1
                            end
                            for _, b in ipairs(buffs) do
                                new_ct[b] = (new_ct[b] or 0) + 1
                            end
                            -- Any buff whose count decreased = net loss.
                            for b, oc in pairs(old_ct) do
                                local nc = new_ct[b] or 0
                                for _ = 1, (oc - nc) do
                                    _chat.process_party_buff_loss(playerId, b)
                                end
                            end
                        end)
                    end
                end

                party_buffs[playerId] = buffs
            end
        end
    end

    -- ── 0x063 sub-9: self buff list → debuff apply/loss detection ───
    -- Mob debuffs (e.g. a TP-move stun) do NOT surface in the 0x028
    -- action stream at all; they arrive only as the buff appearing in
    -- the player's authoritative buff array. Diff against the previous
    -- snapshot; buff_events restricts emission to DEBUFFS (buffs you
    -- receive already emit via recognized 0x028 apply messages).
    -- Layout (confirmed from raw capture): buff ids are plain 2-byte
    -- LE starting such that slot n (1..32) low byte is at 5 + n*2;
    -- 0 and 255 are empty sentinels.
    if id == 0x063 and original:byte(5) == 9
       and _chat and _chat.process_self_debuff_apply then
        pcall(function()
            local cur_ct = {}
            for n = 1, 32 do
                local lo = original:byte(5 + n * 2) or 0
                local hi = original:byte(6 + n * 2) or 0
                local bid = lo + 256 * hi
                if bid ~= 0 and bid ~= 255 and bid >= 1 and bid <= 1023 then
                    cur_ct[bid] = (cur_ct[bid] or 0) + 1
                end
            end

            if _chat.debug_self_buff_dump then
                _chat.debug_self_buff_dump(original, cur_ct)
            end

            if _oc_self_buff_prev then
                -- APPLIES: debuff newly present (count went up).
                for bid, cc in pairs(cur_ct) do
                    local pc = _oc_self_buff_prev[bid] or 0
                    for _ = 1, (cc - pc) do
                        _chat.process_self_debuff_apply(bid)
                    end
                end
                -- REMOVALS: catches removals with no 0x029 wear-off
                -- (e.g. Mix: Vaccine). buff_events dedupes against
                -- recent 0x029 self wear-offs so spell cures aren't
                -- double-reported.
                if _chat.process_self_debuff_loss then
                    for bid, pc in pairs(_oc_self_buff_prev) do
                        local cc = cur_ct[bid] or 0
                        for _ = 1, (pc - cc) do
                            _chat.process_self_debuff_loss(bid)
                        end
                    end
                end
            end
            _oc_self_buff_prev = cur_ct
        end)
    end

    -- ── 0x017 / 0x0CC: real chat packets ─────────────────────────────
    -- 0x017: say/tell/yell/shout/party/LS — replaces the incoming-text
    --        capture for player chat (which BattleMod can mangle).
    -- 0x0CC: Linkshell Message (/lsmes) — NOT carried on incoming text.
    if (id == 0x017 or id == 0x0CC)
       and _chat and _chat.process_chat_packet then
        local ok, err = pcall(_chat.process_chat_packet, id, original)
        if not ok then
            windower.add_to_chat(123,
                '[OmniChat] chat_pkt handler error: ' .. tostring(err))
        end
    end
end)

-- ── Outgoing text — auto-translate phrase resolution ────────────────────
-- FFXI's 'outgoing text' event delivers the command you type with AT
-- phrases as INTACT 6-byte FD sequences; the incoming-text ECHO of the
-- same message arrives pre-mangled by Windower (id_hi byte lost). Tells
-- resolve via 0x0B6 and yells round-trip as inbound 0x017; party/say/
-- linkshell/shout/emote/unity have NO intact source except this event.
-- Resolve the phrase here and stash it for emit.lua's own-echo swap.

-- Resolve all 6-byte FD auto-translate sequences in s to {English}.
local function _oc_resolve_outgoing_at(s)
    if not s or s == '' then return s end
    if not s:find(string.char(0xFD), 1, true) then return s end
    local out, i, n = {}, 1, #s
    while i <= n do
        local b = s:byte(i)
        if b == 0xFD and i + 5 <= n and s:byte(i + 5) == 0xFD then
            local id = (s:byte(i + 3) or 0) * 256 + (s:byte(i + 4) or 0)
            local phrase = res.auto_translates and res.auto_translates[id]
            local txt = phrase and (phrase.en or phrase.ja)
            out[#out + 1] = '{' .. (txt or string.format('?phrase:%d?', id)) .. '}'
            i = i + 6
        else
            out[#out + 1] = string.char(b)
            i = i + 1
        end
    end
    return table.concat(out)
end

-- Map an outgoing chat command to its emit mode. nil = not ours here
-- (tell → 0x0B6; yell → inbound 0x017; non-chat commands).
local _OC_OUT_CHAT_CMDS = {
    ['p']         = 5,  ['party']      = 5,
    ['s']         = 1,  ['say']        = 1,
    ['l']         = 6,  ['linkshell']  = 6,  ['ls']  = 6,
    ['l2']        = 27, ['linkshell2'] = 27, ['ls2'] = 27,
    ['sh']        = 3,  ['shout']      = 3,
    ['em']        = 9,  ['emote']      = 9,
    ['u']         = 211, ['unity']     = 211,
}

oc_safe_register('outgoing text', function(mode, text, blocked)
    if blocked then return end
    if not text or text == '' then return end
    -- Only act on chat phrases that actually contain an AT sequence —
    -- plain "/p hello" needs no resolution and flows through the
    -- normal echo path unharmed.
    if not text:find(string.char(0xFD), 1, true) then return end

    local cmd, rest = text:match('^/(%w+)%s+(.+)$')
    if not cmd then return end
    local emit_mode = _OC_OUT_CHAT_CMDS[cmd:lower()]
    if not emit_mode then return end

    local ok, err = pcall(function()
        local resolved = _oc_resolve_outgoing_at(rest)
        local ok_c, conv = pcall(windower.from_shift_jis, resolved)
        if ok_c and conv then resolved = conv end
        -- Store for in-place substitution by emit.lua's own-echo path.
        _ow_own_outgoing_suppress[emit_mode] = {
            resolved = resolved, ts = os.clock(),
        }
    end)
    if not ok and _ow_chat_debug then
        windower.add_to_chat(123,
            '[OmniChat] outgoing AT resolve failed: ' .. tostring(err))
    end
end)

-- ── Outgoing chunk 0x0B6 — /tell capture ─────────────────────────────────
-- Captured BEFORE FFXI builds the display string, so we have access to
-- structured autotranslate phrase IDs. The text-event path receives
-- only the post-display string where phrases have been substituted
-- with display glyphs we can't decode back.
oc_safe_register('outgoing chunk', function(id, data)
    if id ~= 0x0B6 then return end
    local ok_ot, err_ot = pcall(function()
        -- Target name: bytes 7-21 (1-indexed). Null-trimmed.
        local target_sjis = data:sub(7, 21):gsub('%z+$', '')
        -- Message: byte 22 to end, trailing nulls stripped.
        local msg_sjis = (data:sub(22) or ''):gsub('%z+$', '')
        if msg_sjis == '' and target_sjis == '' then return end

        -- Replace FD ... FD autotranslate sequences with {phrase}.
        local out = {}
        local i, n = 1, #msg_sjis
        while i <= n do
            local b = msg_sjis:byte(i)
            if b == 0xFD and i + 5 <= n
                    and msg_sjis:byte(i + 5) == 0xFD then
                local id_hi = msg_sjis:byte(i + 3) or 0
                local id_lo = msg_sjis:byte(i + 4) or 0
                local phrase_id = id_hi * 256 + id_lo
                local phrase = res.auto_translates
                               and res.auto_translates[phrase_id]
                local text = phrase and (phrase.en or phrase.ja)
                             or string.format('?phrase:%d?', phrase_id)
                out[#out + 1] = '{' .. text .. '}'
                i = i + 6
            else
                out[#out + 1] = string.char(b)
                i = i + 1
            end
        end
        local msg_assembled_sjis = table.concat(out)

        local function to_utf8(s)
            if not s or s == '' then return '' end
            local ok, conv = pcall(windower.from_shift_jis, s)
            if ok and conv then return conv end
            return s
        end
        local target_utf8 = to_utf8(target_sjis)
        local msg_utf8    = to_utf8(msg_assembled_sjis)

        -- FFXI's outgoing-tell wire form: "<Target>>> <message>".
        local line = target_utf8 .. '>> ' .. msg_utf8
        if _chat and _chat.emit_chat then
            pcall(_chat.emit_chat, 12, '', line)
        end
    end)
    if not ok_ot and _ow_chat_debug then
        windower.add_to_chat(123,
            '[OmniChat] outgoing tell capture failed: ' .. tostring(err_ot))
    end
end)

-- ── Incoming text — system lines, "?" fix, other-party gate ─────────────

-- Modes whose lines are already captured by the 0x017/0x0CC packet
-- handler OR the 0x0B6 outgoing chunk handler. The incoming-text
-- handler skips these to avoid double-emitting the same line.
--   mode 4  = /tell received (also captured by 0x017)
--   mode 12 = /tell sent     (captured by 0x0B6 with AT resolution)
local _OC_TEXT_PATH_SKIP_MODES = {
    [4]  = true,
    [12] = true,
}

-- Build the set of "ally" names: you, your party, alliance members,
-- and their pets/trusts. Lowercased. Rebuilt each call (cheap, <=18).
local function _oc_build_ally_name_set()
    local names = {}
    local me = windower.ffxi.get_player()
    if me and me.name then names[me.name:lower()] = true end
    if me and me.pet and me.pet.name then names[me.pet.name:lower()] = true end
    local party = windower.ffxi.get_party()
    if party then
        for _, m in pairs(party) do
            if type(m) == 'table' and m.name and m.name ~= '' then
                names[m.name:lower()] = true
                if m.mob and m.mob.pet_index and m.mob.pet_index ~= 0 then
                    local pet = windower.ffxi.get_mob_by_index
                                and windower.ffxi.get_mob_by_index(m.mob.pet_index)
                    if pet and pet.name and pet.name ~= '' then
                        names[pet.name:lower()] = true
                    end
                end
            end
        end
    end
    return names
end

-- Decide whether a battle-damage TEXT line belongs to ANOTHER party
-- (neither actor nor target is an ally). Used to drop other-party
-- combat noise on shared battle modes (e.g. 40). Fails OPEN — returns
-- false on any parse uncertainty so we never hide the player's own
-- combat.
local function _oc_battle_text_is_other_party(text)
    if not text or text == '' then return false end
    local allies = _oc_build_ally_name_set()
    local low = text:lower()
    for nm in pairs(allies) do
        if nm ~= '' and low:find(nm, 1, true) then
            return false   -- an ally is involved → keep
        end
    end
    -- No ally name found. Require that the line actually looks like a
    -- battle-damage line before deciding it's other-party.
    if low:find('points of damage') or low:find(' takes ')
       or low:find(' uses ') then
        return true
    end
    return false
end

local _battle_text_modes = {[40] = true}

oc_safe_register('incoming text', function(original, modified, original_mode, modified_mode, blocked)
    if not original then return end

    -- Strip FFXI control characters before pattern matching. Color
    -- codes are TWO bytes: 0x1F <index> / 0x1E <index> — strip the
    -- marker AND its index byte together, then any remaining control
    -- bytes < 0x20 except newline and tab.
    local text = original
    text = text:gsub('\31.', ''):gsub('\30.', '')
    text = text:gsub('[%z\1-\8\11-\31]', '')

    -- ── Fix the client's "?" placeholder ─────────────────────────────
    -- The client renders "<Actor> uses ?" when it can't resolve an
    -- ability/item id to a name; the 0x028 handler already resolved
    -- and cached the real name by actor_id. Swap it in if fresh.
    if text:find('%?') then
        local actor_txt = text:match('^(.-) uses %?')
                       or text:match('^(.-) readies %?')
                       or text:match('^(.-) uses an? %?')
        if actor_txt and actor_txt ~= '' then
            local clean = actor_txt:gsub('^The ', '')
            local mob = windower.ffxi.get_mob_by_name(clean)
            local rec = mob and mob.id and _oc_recent_action_name[mob.id]
            if rec and (os.clock() - rec.ts) <= _OC_ACTION_NAME_TTL then
                text = text:gsub('%?', rec.name, 1)
            end
        end
    end

    -- ── Chat panel capture ────────────────────────────────────────────
    -- Push every incoming text line into the chat ring, EXCEPT:
    --   * modes the 0x017/0x0CC/0x0B6 packet handlers already capture
    --     (would duplicate every player-chat line), and
    --   * mode-40 battle text where neither actor nor target is an
    --     ally (another party's fight leaking onto a shared mode).
    if original_mode and _battle_text_modes[original_mode]
       and _oc_battle_text_is_other_party(text) then
        -- other party's battle line; drop.
    elseif _chat and _chat.emit_chat
            and not _OC_TEXT_PATH_SKIP_MODES[original_mode or -1] then
        local ok_emit, err_emit = pcall(_chat.emit_chat,
                                        original_mode or 0, '', text)
        if not ok_emit and _ow_chat_debug then
            windower.add_to_chat(123,
                '[OmniChat] chat emit failed: ' .. tostring(err_emit))
        end
    end
end)

-- ── Zone / login resets ───────────────────────────────────────────────────
oc_safe_register('zone change', function()
    -- Drop the 0x063 self-buff snapshot: after a zone the buff list
    -- repopulates from scratch, and without clearing this the
    -- repopulate would diff as a flurry of "afflicted with" lines for
    -- debuffs already on you. Next 0x063 sub-9 re-baselines silently.
    _oc_self_buff_prev = nil
end)

oc_safe_register('login', function()
    _oc_self_buff_prev = nil
    party_buffs = {}
end)

oc_safe_register('logout', function()
    _oc_self_buff_prev = nil
    party_buffs = {}
end)

-- ── Inbound command drain (python → lua) ─────────────────────────────────
-- Bare-datagram protocol: the whole payload is a windower console
-- command (the composer sends "input /p hello", the name context menu
-- sends "input /pcmd add Name"). Mirrors OmniWatch's legacy bare-
-- command mode on its 5011 listener.
-- ── NPC dialog "continue" arrow support ───────────────────────────────
-- FFXI marks NPC dialog / cutscenes with player status 4 (Event). We
-- watch it (~2 Hz in prerender, throttled) and report changes to the
-- overlay as 'CSSTATE\t1|0' (multibox-tagged), with a periodic refresh
-- feeding the overlay's freshness window. The overlay shows a pulsing
-- continue arrow at the end of the last chat line while the pinned
-- character is in this state.
local _oc_cs_last_sent = nil
local _oc_cs_next_poll = 0
local _oc_cs_next_refresh = 0

local function _oc_in_event()
    local p = windower.ffxi.get_player()
    return p ~= nil and p.status == 4
end

local function _oc_poll_cs_state()
    local now = os.clock()
    if now < _oc_cs_next_poll then return end
    _oc_cs_next_poll = now + 0.5
    local on = _oc_in_event()
    if on ~= _oc_cs_last_sent or now >= _oc_cs_next_refresh then
        _oc_cs_last_sent = on
        _oc_cs_next_refresh = now + 2.0
        local p = windower.ffxi.get_player()
        local name = (p and p.name) or ''
        if udp_chat and name ~= '' then
            pcall(function()
                udp_chat:send('@' .. name .. '@CSSTATE\t'
                              .. (on and '1' or '0'))
            end)
        end
    end
end

local function _oc_drain_inbound()
    if not udp_cmd_in then return end
    local guard = 64
    while guard > 0 do
        guard = guard - 1
        local data = udp_cmd_in:receive()
        if not data then break end
        if data ~= '' then
            if _ow_chat_debug then
                windower.add_to_chat(207,
                    '[OmniChat] cmd recv: ' .. tostring(data))
            end
            -- Composer messages arrive as 'input /p hello {Yes, please.}'.
            -- If the payload contains a resolvable auto-translate
            -- phrase, encode it and deliver via windower.chat.input()
            -- (typed-chat injection passes raw FD bytes through to the
            -- game; send_command's parser is not byte-safe for them).
            -- Plain messages keep the original send_command path
            -- byte-for-byte, so existing behavior is unchanged.
            windower.send_command(data)
        end
    end
end

-- ── Prerender — 10 Hz drain + 1 Hz job line ──────────────────────────────
-- The drain pulls queued chat events out of the ring and sends them as
-- CHAT_BATCH datagrams (see chat/drain.lua). Cheap when empty. The job
-- line tells the Python side which character/job is live so it can
-- apply per-job routing overrides and pin multibox chat to a main.
local _oc_drain_acc = 0
local _oc_job_acc   = 0
local _oc_total_drained = 0     -- lifetime events sent (//oc status)
local _oc_last_drain_ts = 0     -- os.time() of last non-empty drain

oc_safe_register('prerender', function()
    -- Inbound commands every frame (cheap; usually empty).
    pcall(_oc_drain_inbound)
    -- Cutscene-status poll for the dialog pad (self-throttled 2 Hz).
    pcall(_oc_poll_cs_state)

    _oc_drain_acc = _oc_drain_acc + 1
    if _oc_drain_acc >= 6 then        -- ~60 Hz prerender → ~10 Hz drain
        _oc_drain_acc = 0
        if _chat then
            local ok, n = pcall(_chat.drain_text, udp_chat)
            if ok and type(n) == 'number' and n > 0 then
                _oc_total_drained = _oc_total_drained + n
                _oc_last_drain_ts = os.time()
            end
        end
    end

    _oc_job_acc = _oc_job_acc + 1
    if _oc_job_acc >= 60 then         -- ~1 Hz
        _oc_job_acc = 0
        pcall(function()
            local me = windower.ffxi.get_player()
            if not me then return end
            local job = me.main_job or ''
            local name = me.name or ''
            -- Same @name@ multibox prefix drain.lua uses, so the
            -- Python _mb_gate path treats this line uniformly.
            udp_chat:send('@' .. name .. '@JOB\t' .. job .. '\t' .. name)
        end)
    end
end)

-- ── //omnichat commands ───────────────────────────────────────────────────
oc_safe_register('addon command', function(command, ...)
    command = (command or 'help'):lower()
    local args = {...}

    if command == 'dump' then
        if not _chat then
            windower.add_to_chat(123, '[OmniChat] chat module not loaded.')
            return
        end
        local n = tonumber(args[1]) or 20
        windower.add_to_chat(207, string.format(
            '[OmniChat] live ring: text=%d/%d (dropped=%d)',
            _chat.text_ring.size(), _chat.text_ring.capacity(),
            _chat.text_ring.dropped()))
        windower.add_to_chat(207, string.format(
            '[OmniChat] history: text=%d/%d',
            _chat.text_history.size(), _chat.text_history.capacity()))
        local snap = _chat.text_history.peek()
        local start = math.max(1, #snap - n + 1)
        if #snap == 0 then
            windower.add_to_chat(207,
                '[OmniChat]   (history empty -- say something or wait for chat)')
        else
            for i = start, #snap do
                local ev = snap[i]
                windower.add_to_chat(207, string.format(
                    '[OmniChat]   #%d mode=%d %s [%s]: %s',
                    i, ev.mode or 0,
                    (ev.actor_name ~= '' and ev.actor_name) or '?',
                    ev.actor_class or '?',
                    (ev.text or ''):sub(1, 80)))
            end
        end

    elseif command == 'reset' then
        if not _chat then
            windower.add_to_chat(123, '[OmniChat] chat module not loaded.')
            return
        end
        _chat.reset_history()
        windower.add_to_chat(207, '[OmniChat] chat history cleared.')

    elseif command == 'debug' then
        -- Unified chat diagnostics: one switch drives every probe;
        -- output funnels to <addon>/data/ probe logs.
        local target
        if args[1] == 'on' or args[1] == 'off' then
            target = (args[1] == 'on')
        else
            target = not _oc_debug_on
        end
        _oc_debug_on  = target
        _ow_chat_debug = target

        if _chat then
            if _chat.set_debug              then _chat.set_debug(target) end
            if _chat.set_hex_capture        then _chat.set_hex_capture(target) end
            if _chat.set_chat_pkt_debug     then _chat.set_chat_pkt_debug(target) end
            if _chat.set_chat_pkt_trace     then _chat.set_chat_pkt_trace(target) end
            if _chat.set_dropped_mode_log   then _chat.set_dropped_mode_log(target) end
            if _chat.set_buff_wear_probe    then _chat.set_buff_wear_probe(target) end
            if _chat.set_buff_apply_probe   then _chat.set_buff_apply_probe(target) end
            if _chat.set_battle_classify_probe then
                _chat.set_battle_classify_probe(target)
            end
        end
        windower.add_to_chat(207, string.format(
            '[OmniChat] debug = %s%s', tostring(target),
            target and ' (logs under OmniChat/data/)' or ''))

    elseif command == 'condense' then
        if not _chat then
            windower.add_to_chat(123, '[OmniChat] chat module not loaded.')
            return
        end
        local target
        if args[1] == 'on' or args[1] == 'off' then
            target = args[1] == 'on'
        else
            target = not _chat.is_condense_melee()
        end
        _chat.set_condense_melee(target)
        windower.add_to_chat(207, string.format(
            '[OmniChat] condense multi-hit melee/ranged = %s',
            tostring(target)))

    elseif command == 'status' then
        -- Pipeline health at a glance. Use this first when "no chat is
        -- coming through": it tells you whether the chat module loaded,
        -- whether events are being captured, and whether the drain is
        -- actually sending them.
        windower.add_to_chat(207, '[OmniChat] status:')
        if not _chat then
            windower.add_to_chat(123,
                '  chat module: NOT LOADED — capture is dead. Check the')
            windower.add_to_chat(123,
                '  load-time error above //lua load omnichat, and that the')
            windower.add_to_chat(123,
                '  chat/ folder was copied from OmniWatch.')
        else
            windower.add_to_chat(207, '  chat module: loaded')
            windower.add_to_chat(207, string.format(
                '  live ring: %d/%d (dropped=%d) | history: %d/%d',
                _chat.text_ring.size(), _chat.text_ring.capacity(),
                _chat.text_ring.dropped(),
                _chat.text_history.size(), _chat.text_history.capacity()))
            windower.add_to_chat(207, string.format(
                '  drained to overlay (5113): %d events total%s',
                _oc_total_drained,
                _oc_last_drain_ts > 0
                    and string.format(', last %ds ago',
                                      os.time() - _oc_last_drain_ts)
                    or ' (none yet)'))
        end
        windower.add_to_chat(207, string.format(
            '  inbound cmd socket (5111): %s',
            udp_cmd_in and 'bound' or 'NOT BOUND (composer sends ignored)'))

    elseif command == 'ping' then
        -- Push a synthetic event straight into the ring. If it shows
        -- up in the overlay's System tab, the ring → drain → UDP →
        -- Python pipeline is healthy and any missing chat is a CAPTURE
        -- problem (events/packets). If it doesn't, it's TRANSPORT
        -- (overlay not running / port blocked / mb-gate mismatch).
        if not _chat then
            windower.add_to_chat(123, '[OmniChat] chat module not loaded.')
            return
        end
        _chat.text_ring.push({
            ts           = os.time(),
            source       = 'system',
            mode         = -2,
            actor_id     = 0,
            actor_name   = 'OmniChat',
            actor_class  = 'system',
            target_id    = 0,
            target_name  = '',
            target_class = '',
            text         = string.format(
                'ping — pipeline OK (%s)', os.date('%H:%M:%S')),
            segments     = {},
        })
        windower.add_to_chat(207,
            '[OmniChat] ping queued — check the overlay System tab.')

    elseif command == 'class' and #args > 0 then
        if not _chat then
            windower.add_to_chat(123, '[OmniChat] chat module not loaded.')
            return
        end
        local id = tonumber(args[1])
        if not id then
            windower.add_to_chat(123, '[OmniChat] usage: //oc class <numeric id>')
            return
        end
        local cat, name, slot = _chat.classify_entity(id)
        windower.add_to_chat(207, string.format(
            '[OmniChat] id=%d -> category=%s name=%s slot=%s',
            id, tostring(cat), tostring(name), tostring(slot)))

    else
        windower.add_to_chat(207, '[OmniChat] commands:')
        windower.add_to_chat(207, '  //oc status          pipeline health (capture/drain/sockets)')
        windower.add_to_chat(207, '  //oc ping            push a test event to the overlay')
        windower.add_to_chat(207, '  //oc dump [N]        show last N captured events')
        windower.add_to_chat(207, '  //oc reset           clear chat history ring')
        windower.add_to_chat(207, '  //oc debug [on|off]  unified diagnostics')
        windower.add_to_chat(207, '  //oc condense [on|off] condensed multi-hit display')
        windower.add_to_chat(207, '  //oc class <id>      classify a mob id')
    end
end)

windower.add_to_chat(207, '[OmniChat] loaded. Events -> 127.0.0.1:5113, '
    .. 'commands <- 5111. Run OmniChat.py for the overlay.')