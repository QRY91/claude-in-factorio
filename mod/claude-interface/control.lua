-- Claude Interface - In-game chat with Claude AI (multi-agent)
-- Communication: write_file -> bridge daemon -> Claude CLI -> RCON -> remote interface

local GUI_FRAME = "claude_interface_frame"
local MAX_MESSAGES = 100
local INPUT_FILE = "claude-chat/input.jsonl"

-- ============================================================
-- Storage
-- ============================================================

local function init_storage()
    storage.messages = storage.messages or {}
    storage.msg_counter = storage.msg_counter or 0
    storage.agents = storage.agents or {"default"}
    storage.agent_labels = storage.agent_labels or {}
    storage.active_agent = storage.active_agent or {}
    storage._rcon_queue = storage._rcon_queue or {}
    storage.spectator_mode = storage.spectator_mode or false
    -- Agent character entities and walk targets (for deterministic on_tick processing)
    storage.characters = storage.characters or {}
    storage.walk_state = storage.walk_state or {}
end

-- Ensure per-agent message tables exist for a player
local function ensure_agent_messages(player_index)
    if not storage.messages[player_index] then
        storage.messages[player_index] = {}
    end
    for _, agent_name in ipairs(storage.agents) do
        if not storage.messages[player_index][agent_name] then
            storage.messages[player_index][agent_name] = {}
        end
    end
end

-- Get the active agent for a player (defaults to first registered)
local function get_active_agent(player_index)
    local agent = storage.active_agent[player_index]
    if agent then
        -- Verify agent still exists
        for _, a in ipairs(storage.agents) do
            if a == agent then return agent end
        end
    end
    return storage.agents[1] or "default"
end

-- ============================================================
-- Shortcut Bar State
-- ============================================================

local function update_shortcut_state(player)
    local is_open = player.gui.screen[GUI_FRAME] ~= nil
    player.set_shortcut_toggled("claude-interface-toggle", is_open)
end

-- ============================================================
-- GUI Construction
-- ============================================================

local function get_agent_display_name(player)
    return settings.get_player_settings(player)["claude-interface-agent-label"].value or "Claude"
end

local function add_message_label(chat_flow, role, text, player)
    local caption
    if role == "user" then
        caption = "[color=1,0.85,0.4]You:[/color] " .. text
    elseif role == "claude" then
        local name = player and get_agent_display_name(player) or "Claude"
        caption = "[color=0.6,0.8,1]" .. name .. ":[/color] " .. text
    else
        caption = "[color=0.6,0.6,0.6]" .. text .. "[/color]"
    end

    local label = chat_flow.add{
        type = "label",
        caption = caption
    }
    label.style.single_line = false
    label.style.horizontally_stretchable = true
    return label
end

local function restore_chat(player, chat_flow, agent_name)
    ensure_agent_messages(player.index)
    local msgs = storage.messages[player.index][agent_name]
    if not msgs then return end
    for _, msg in ipairs(msgs) do
        add_message_label(chat_flow, msg.role, msg.text, player)
    end
end

-- Get the chat_flow for a specific agent tab
local function get_agent_chat_flow(frame, agent_name)
    if not frame or not frame.valid then return nil end
    local tabbed = frame["ci_agent_tabs"]
    if not tabbed then return nil end
    local scroll = tabbed["ci_scroll_" .. agent_name]
    if not scroll then return nil end
    return scroll["ci_chat_" .. agent_name]
end

-- Get the scroll-pane for a specific agent tab
local function get_agent_scroll(frame, agent_name)
    if not frame or not frame.valid then return nil end
    local tabbed = frame["ci_agent_tabs"]
    if not tabbed then return nil end
    return tabbed["ci_scroll_" .. agent_name]
end

-- Find the tab index for a given agent name
local function find_tab_index(tabbed, agent_name)
    for i, tab_and_content in ipairs(tabbed.tabs) do
        if tab_and_content.tab.name == "ci_tab_" .. agent_name then
            return i
        end
    end
    return nil
end

-- Get display label for a tab (short name or agent_name)
local function get_agent_label(agent_name)
    return storage.agent_labels[agent_name] or agent_name
end

-- Create a single agent tab + scroll-pane + chat_flow inside a tabbed-pane
local function create_agent_tab(tabbed, player, agent_name)
    local tab = tabbed.add{
        type = "tab",
        name = "ci_tab_" .. agent_name,
        caption = get_agent_label(agent_name),
    }

    local scroll = tabbed.add{
        type = "scroll-pane",
        name = "ci_scroll_" .. agent_name,
        direction = "vertical",
    }
    scroll.style.vertically_stretchable = true
    scroll.style.horizontally_stretchable = true

    local chat_flow = scroll.add{
        type = "flow",
        name = "ci_chat_" .. agent_name,
        direction = "vertical",
    }
    chat_flow.style.vertical_spacing = 6
    chat_flow.style.horizontally_stretchable = true

    tabbed.add_tab(tab, scroll)

    -- Restore history for this agent
    restore_chat(player, chat_flow, agent_name)
    scroll.scroll_to_bottom()

    return tab, scroll
end

local function create_gui(player)
    if player.gui.screen[GUI_FRAME] then return end

    ensure_agent_messages(player.index)

    -- Main frame
    local frame = player.gui.screen.add{
        type = "frame",
        name = GUI_FRAME,
        direction = "vertical"
    }
    frame.auto_center = true
    frame.style.width = 700
    frame.style.height = 650

    -- Titlebar: drag + close
    local titlebar = frame.add{
        type = "flow",
        name = "ci_titlebar",
        direction = "horizontal"
    }
    titlebar.drag_target = frame
    titlebar.style.vertical_align = "center"

    local title_text = settings.get_player_settings(player)["claude-interface-title"].value or "Claude AI"
    titlebar.add{
        type = "label",
        name = "ci_title",
        caption = title_text,
        style = "frame_title"
    }

    local spacer = titlebar.add{
        type = "empty-widget",
        name = "ci_spacer",
        style = "draggable_space"
    }
    spacer.style.horizontally_stretchable = true
    spacer.style.height = 24
    spacer.drag_target = frame

    titlebar.add{
        type = "sprite-button",
        name = "ci_close",
        sprite = "utility/close",
        style = "close_button",
        tooltip = "Close [Ctrl+Shift+C]"
    }

    -- Tabbed pane for agents
    local tabbed = frame.add{
        type = "tabbed-pane",
        name = "ci_agent_tabs",
    }
    tabbed.style.vertically_stretchable = true
    tabbed.style.horizontally_stretchable = true

    -- Create a tab per registered agent
    local active_agent = get_active_agent(player.index)
    local active_idx = 1
    for i, agent_name in ipairs(storage.agents) do
        create_agent_tab(tabbed, player, agent_name)
        if agent_name == active_agent then
            active_idx = i
        end
    end

    -- Select the active tab
    tabbed.selected_tab_index = active_idx
    storage.active_agent[player.index] = storage.agents[active_idx]

    -- Status indicator
    frame.add{
        type = "label",
        name = "ci_status",
        caption = "[color=0.4,0.8,0.4]Ready[/color]"
    }

    -- Input area: textfield + send button
    local input_flow = frame.add{
        type = "flow",
        name = "ci_input_flow",
        direction = "horizontal"
    }
    input_flow.style.vertical_align = "center"
    input_flow.style.horizontally_stretchable = true

    local input = input_flow.add{
        type = "textfield",
        name = "ci_input",
        tooltip = "Type a message and press Enter"
    }
    input.style.horizontally_stretchable = true
    input.style.minimal_width = 0
    input.style.maximal_width = 0

    input_flow.add{
        type = "sprite-button",
        name = "ci_send",
        sprite = "utility/enter",
        style = "tool_button",
        tooltip = "Send"
    }

    -- Focus input and register for Escape-close
    input.focus()
    player.opened = frame
end

local function destroy_gui(player)
    local frame = player.gui.screen[GUI_FRAME]
    if frame and frame.valid then
        frame.destroy()
    end
end

local function toggle_gui(player)
    if player.gui.screen[GUI_FRAME] then
        destroy_gui(player)
    else
        create_gui(player)
    end
    update_shortcut_state(player)
end

-- ============================================================
-- Chat Logic
-- ============================================================

local function save_message(player_index, agent_name, role, text)
    ensure_agent_messages(player_index)
    local msgs = storage.messages[player_index][agent_name]
    table.insert(msgs, {
        role = role,
        text = text,
        tick = game.tick,
    })
    while #msgs > MAX_MESSAGES do
        table.remove(msgs, 1)
    end
end

local function add_chat_message(player, agent_name, role, text)
    save_message(player.index, agent_name, role, text)

    local frame = player.gui.screen[GUI_FRAME]
    if not frame or not frame.valid then return end

    local chat_flow = get_agent_chat_flow(frame, agent_name)
    if not chat_flow then return end
    add_message_label(chat_flow, role, text, player)

    while #chat_flow.children > MAX_MESSAGES do
        chat_flow.children[1].destroy()
    end

    local scroll = get_agent_scroll(frame, agent_name)
    if scroll then scroll.scroll_to_bottom() end

    -- Badge for non-active tabs
    local active = get_active_agent(player.index)
    if agent_name ~= active and role ~= "user" then
        local tabbed = frame["ci_agent_tabs"]
        if tabbed then
            local tab_idx = find_tab_index(tabbed, agent_name)
            if tab_idx then
                local tab_obj = tabbed.tabs[tab_idx].tab
                local current = tab_obj.badge_text
                local count = 0
                if current and current ~= "" then
                    count = tonumber(current) or 0
                end
                tab_obj.badge_text = tostring(count + 1)
            end
        end
    end
end

local function set_status(player, status_text)
    local frame = player.gui.screen[GUI_FRAME]
    if not frame or not frame.valid then return end
    frame["ci_status"].caption = status_text
end

local function send_to_bridge(player, message)
    storage.msg_counter = storage.msg_counter + 1
    local target = get_active_agent(player.index)
    local payload = {
        id = storage.msg_counter,
        player_index = player.index,
        player_name = player.name,
        message = message,
        target_agent = target,
        tick = game.tick,
    }
    helpers.write_file(INPUT_FILE, helpers.table_to_json(payload) .. "\n", true, 0)
end

local function handle_send(player)
    local frame = player.gui.screen[GUI_FRAME]
    if not frame or not frame.valid then return end

    local input = frame["ci_input_flow"]["ci_input"]
    local text = input.text
    if text == "" or text == nil then return end

    input.text = ""
    input.focus()

    local agent_name = get_active_agent(player.index)
    add_chat_message(player, agent_name, "user", text)
    set_status(player, "[color=1,0.8,0.2]Thinking...[/color]")
    send_to_bridge(player, text)
end

-- ============================================================
-- Agent Management
-- ============================================================

local function agent_exists(name)
    for _, a in ipairs(storage.agents) do
        if a == name then return true end
    end
    return false
end

local function register_agent(agent_name, label)
    if label then
        storage.agent_labels[agent_name] = label
    end
    if agent_exists(agent_name) then return end
    table.insert(storage.agents, agent_name)

    -- Create message tables for all players
    for _, player in pairs(game.players) do
        ensure_agent_messages(player.index)
    end

    -- Add tab to all open GUIs
    for _, player in pairs(game.players) do
        local frame = player.gui.screen[GUI_FRAME]
        if frame and frame.valid then
            local tabbed = frame["ci_agent_tabs"]
            if tabbed then
                create_agent_tab(tabbed, player, agent_name)
            end
        end
    end
end

local function unregister_agent(agent_name)
    -- Allow removing "default" only if other agents exist
    if agent_name == "default" and #storage.agents <= 1 then return end
    local idx = nil
    for i, a in ipairs(storage.agents) do
        if a == agent_name then idx = i; break end
    end
    if not idx then return end

    table.remove(storage.agents, idx)

    -- Remove tab from all open GUIs
    for _, player in pairs(game.players) do
        local frame = player.gui.screen[GUI_FRAME]
        if frame and frame.valid then
            local tabbed = frame["ci_agent_tabs"]
            if tabbed then
                local tab_idx = find_tab_index(tabbed, agent_name)
                if tab_idx then
                    tabbed.remove_tab(tabbed.tabs[tab_idx].tab)
                    -- Clean up the scroll pane element
                    local scroll = tabbed["ci_scroll_" .. agent_name]
                    if scroll then scroll.destroy() end
                    local tab_el = tabbed["ci_tab_" .. agent_name]
                    if tab_el then tab_el.destroy() end
                end
            end
        end

        -- Reset active agent if it was the removed one
        if storage.active_agent[player.index] == agent_name then
            storage.active_agent[player.index] = storage.agents[1] or "default"
        end
    end
end

-- ============================================================
-- Queue Processing (on_tick)
-- ============================================================

-- Apply walk states to agent characters each tick.
-- Processed in on_tick for deterministic multiplayer behavior.
local function process_walk_states()
    if not storage.walk_state then return end
    local to_remove = {}
    for agent_id, ws in pairs(storage.walk_state) do
        local c = storage.characters[agent_id]
        if c and c.valid then
            c.walking_state = ws
        end
        -- Clean up stopped entries (applied once, then removed)
        if not ws.walking then
            table.insert(to_remove, agent_id)
        end
    end
    for _, agent_id in ipairs(to_remove) do
        storage.walk_state[agent_id] = nil
    end
end

-- Process queued RCON commands deterministically in on_tick.
-- This prevents desync in multiplayer: RCON pushes to queue,
-- on_tick processes it identically on server and all clients.
local function process_rcon_queue()
    if not storage._rcon_queue or #storage._rcon_queue == 0 then return end
    local queue = storage._rcon_queue
    storage._rcon_queue = {}
    for _, item in ipairs(queue) do
        if item.type == "response" then
            local player = game.get_player(item.pi)
            if player then
                add_chat_message(player, item.agent, "claude", item.text)
                set_status(player, "[color=0.4,0.8,0.4]Ready[/color]")
            end
        elseif item.type == "tool" then
            -- Tool calls only shown in status bar, not in chat log
            local player = game.get_player(item.pi)
            if player then
                set_status(player, "[color=0.6,0.7,1]Using " .. item.tool .. "...[/color]")
            end
        elseif item.type == "status" then
            local player = game.get_player(item.pi)
            if player then
                set_status(player, item.text)
            end
        elseif item.type == "register" then
            register_agent(item.agent, item.label)
        elseif item.type == "unregister" then
            unregister_agent(item.agent)
        elseif item.type == "clear" then
            local player = game.get_player(item.pi)
            if player then
                if item.agent then
                    if storage.messages[item.pi] then
                        storage.messages[item.pi][item.agent] = {}
                    end
                    local frame = player.gui.screen[GUI_FRAME]
                    if frame and frame.valid then
                        local chat_flow = get_agent_chat_flow(frame, item.agent)
                        if chat_flow then chat_flow.clear() end
                    end
                else
                    storage.messages[item.pi] = {}
                    ensure_agent_messages(item.pi)
                    local frame = player.gui.screen[GUI_FRAME]
                    if frame and frame.valid then
                        for _, a in ipairs(storage.agents) do
                            local chat_flow = get_agent_chat_flow(frame, a)
                            if chat_flow then chat_flow.clear() end
                        end
                    end
                end
            end
        end
    end
end

-- ============================================================
-- Remote Interface (called by bridge via RCON)
-- All state-modifying operations push to _rcon_queue for
-- deterministic processing in on_tick (prevents MP desync).
-- ============================================================

remote.add_interface("claude_interface", {
    receive_response = function(player_index, agent_name, text)
        table.insert(storage._rcon_queue, {
            type = "response", pi = player_index,
            agent = agent_name or "default", text = text,
        })
    end,

    tool_status = function(player_index, agent_name, tool_name)
        table.insert(storage._rcon_queue, {
            type = "tool", pi = player_index,
            agent = agent_name or "default", tool = tool_name,
        })
    end,

    set_status = function(player_index, status_text)
        table.insert(storage._rcon_queue, {
            type = "status", pi = player_index, text = status_text,
        })
    end,

    clear_chat = function(player_index, agent_name)
        table.insert(storage._rcon_queue, {
            type = "clear", pi = player_index, agent = agent_name,
        })
    end,

    register_agent = function(agent_name, label)
        table.insert(storage._rcon_queue, {
            type = "register", agent = agent_name, label = label,
        })
    end,

    unregister_agent = function(agent_name)
        table.insert(storage._rcon_queue, {
            type = "unregister", agent = agent_name,
        })
    end,

    -- Register an agent character entity for on_tick walk processing
    register_character = function(agent_id, entity)
        if not storage.characters then storage.characters = {} end
        storage.characters[agent_id] = entity
    end,

    -- Set walking direction for an agent (processed in on_tick)
    set_walk = function(agent_id, direction)
        if not storage.walk_state then storage.walk_state = {} end
        storage.walk_state[agent_id] = {walking = true, direction = direction}
    end,

    -- Stop walking for an agent (processed in on_tick)
    stop_walk = function(agent_id)
        if not storage.walk_state then storage.walk_state = {} end
        storage.walk_state[agent_id] = {walking = false}
    end,

    -- Get character position (read-only, safe from any context)
    get_character_pos = function(agent_id)
        if not storage.characters then return nil end
        local c = storage.characters[agent_id]
        if c and c.valid then
            return c.position.x .. "," .. c.position.y
        end
        return nil
    end,

    set_spectator_mode = function(enabled)
        storage.spectator_mode = enabled
        -- Apply to all currently connected players
        if enabled then
            for _, player in pairs(game.players) do
                if player.connected and player.controller_type ~= defines.controllers.spectator then
                    player.set_controller{type = defines.controllers.spectator}
                end
            end
        end
    end,

    ping = function()
        rcon.print("pong")
    end,
})

-- ============================================================
-- Event Handlers
-- ============================================================

script.on_init(init_storage)

-- Process RCON queue and walk states every tick
script.on_event(defines.events.on_tick, function(event)
    process_rcon_queue()
    process_walk_states()
end)

script.on_configuration_changed(function(data)
    -- Migrate old flat messages to per-agent structure
    if storage.messages then
        for player_index, msgs in pairs(storage.messages) do
            -- Detect old format: flat array of {role, text, tick}
            if msgs[1] and msgs[1].role then
                storage.messages[player_index] = {default = msgs}
            end
        end
    end

    init_storage()

    -- Rebuild GUI for existing players after mod update
    for _, player in pairs(game.players) do
        local frame = player.gui.screen[GUI_FRAME]
        if frame and frame.valid then
            frame.destroy()
            create_gui(player)
        end
        update_shortcut_state(player)
    end
end)

-- Settings changed — rebuild GUI to pick up new title/label
script.on_event(defines.events.on_runtime_mod_setting_changed, function(event)
    if event.setting == "claude-interface-title" or event.setting == "claude-interface-agent-label" then
        local player = game.get_player(event.player_index)
        if player then
            local frame = player.gui.screen[GUI_FRAME]
            if frame and frame.valid then
                frame.destroy()
                create_gui(player)
            end
        end
    end
end)

-- Auto-spectator: when spectator_mode is enabled, new players join as spectators
script.on_event(defines.events.on_player_joined_game, function(event)
    if storage.spectator_mode then
        local player = game.get_player(event.player_index)
        if player and player.controller_type ~= defines.controllers.spectator then
            player.set_controller{type = defines.controllers.spectator}
        end
    end
end)

-- Hotkey toggle
script.on_event("claude-interface-toggle", function(event)
    local player = game.get_player(event.player_index)
    if player then toggle_gui(player) end
end)

-- Shortcut bar toggle
script.on_event(defines.events.on_lua_shortcut, function(event)
    if event.prototype_name ~= "claude-interface-toggle" then return end
    local player = game.get_player(event.player_index)
    if player then toggle_gui(player) end
end)

-- Tab switching
script.on_event(defines.events.on_gui_selected_tab_changed, function(event)
    if not event.element or not event.element.valid then return end
    if event.element.name ~= "ci_agent_tabs" then return end

    local player = game.get_player(event.player_index)
    if not player then return end

    local tabbed = event.element
    local idx = tabbed.selected_tab_index
    if not idx or not tabbed.tabs[idx] then return end

    local tab_obj = tabbed.tabs[idx].tab
    local tab_name = tab_obj.name  -- "ci_tab_<agent_name>"
    local agent_name = tab_name:sub(8)  -- strip "ci_tab_"

    storage.active_agent[player.index] = agent_name

    -- Clear badge on newly selected tab
    tab_obj.badge_text = ""
end)

-- Click handler
script.on_event(defines.events.on_gui_click, function(event)
    if not event.element or not event.element.valid then return end
    local name = event.element.name

    if name == "ci_send" then
        handle_send(game.get_player(event.player_index))
    elseif name == "ci_close" then
        local player = game.get_player(event.player_index)
        destroy_gui(player)
        update_shortcut_state(player)
    end
end)

-- Enter key submits
script.on_event(defines.events.on_gui_confirmed, function(event)
    if not event.element or not event.element.valid then return end
    if event.element.name == "ci_input" then
        handle_send(game.get_player(event.player_index))
    end
end)

-- Escape closes
script.on_event(defines.events.on_gui_closed, function(event)
    if event.element and event.element.valid and event.element.name == GUI_FRAME then
        local player = game.get_player(event.player_index)
        destroy_gui(player)
        update_shortcut_state(player)
    end
end)
