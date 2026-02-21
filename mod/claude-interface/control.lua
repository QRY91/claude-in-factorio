-- Claude Interface - In-game chat with Claude AI
-- Communication: write_file -> bridge daemon -> Claude API -> RCON -> remote interface

local GUI_FRAME = "claude_interface_frame"
local TOP_BUTTON = "claude_interface_toggle_btn"
local MAX_MESSAGES = 100
local INPUT_FILE = "claude-chat/input.jsonl"

local SIZES = {
    {name = "S", w = 380, h = 350},
    {name = "M", w = 520, h = 500},
    {name = "L", w = 700, h = 650},
}

-- ============================================================
-- Storage
-- ============================================================

local function init_storage()
    storage.messages = storage.messages or {}
    storage.msg_counter = storage.msg_counter or 0
    storage.gui_size = storage.gui_size or {}
end

local function get_size(player_index)
    if not storage.gui_size then storage.gui_size = {} end
    local idx = storage.gui_size[player_index] or 2
    return SIZES[idx], idx
end

-- ============================================================
-- Top Bar Button
-- ============================================================

local function ensure_top_button(player)
    if player.gui.top[TOP_BUTTON] then return end
    player.gui.top.add{
        type = "sprite-button",
        name = TOP_BUTTON,
        caption = "AI",
        tooltip = "Toggle Claude AI [Ctrl+Shift+C]",
        style = "slot_button"
    }
end

local function destroy_top_button(player)
    local btn = player.gui.top[TOP_BUTTON]
    if btn then btn.destroy() end
end

-- ============================================================
-- GUI Construction
-- ============================================================

local function add_message_label(chat_flow, role, text)
    local caption
    if role == "user" then
        caption = "[color=1,0.85,0.4]You:[/color] " .. text
    elseif role == "claude" then
        caption = "[color=0.6,0.8,1]Claude:[/color] " .. text
    elseif role == "tool" then
        caption = "[color=0.5,0.7,0.5]> " .. text .. "[/color]"
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

local function restore_chat(player, chat_flow)
    local msgs = storage.messages[player.index]
    if not msgs then return end
    for _, msg in ipairs(msgs) do
        add_message_label(chat_flow, msg.role, msg.text)
    end
end

local function apply_size(frame, size)
    frame.style.width = size.w
    frame.style.height = size.h
end

local function create_gui(player)
    if player.gui.screen[GUI_FRAME] then return end

    local size, _ = get_size(player.index)

    -- Main frame
    local frame = player.gui.screen.add{
        type = "frame",
        name = GUI_FRAME,
        direction = "vertical"
    }
    frame.auto_center = true
    frame.style.width = size.w
    frame.style.height = size.h

    -- Titlebar: drag + size buttons + close
    local titlebar = frame.add{
        type = "flow",
        name = "ci_titlebar",
        direction = "horizontal"
    }
    titlebar.drag_target = frame
    titlebar.style.vertical_align = "center"

    titlebar.add{
        type = "label",
        name = "ci_title",
        caption = "Claude AI",
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

    -- Size toggle buttons
    for i, s in ipairs(SIZES) do
        local btn = titlebar.add{
            type = "button",
            name = "ci_size_" .. i,
            caption = s.name,
            tooltip = s.w .. "x" .. s.h,
            style = "tool_button"
        }
        btn.style.width = 28
        btn.style.height = 28
        btn.style.padding = 0
        btn.style.font = "default-small"
    end

    titlebar.add{
        type = "sprite-button",
        name = "ci_close",
        sprite = "utility/close",
        style = "close_button",
        tooltip = "Close [Ctrl+Shift+C]"
    }

    -- Chat scroll area
    local scroll = frame.add{
        type = "scroll-pane",
        name = "ci_chat_scroll",
        direction = "vertical"
    }
    scroll.style.vertically_stretchable = true
    scroll.style.horizontally_stretchable = true

    local chat_flow = scroll.add{
        type = "flow",
        name = "ci_chat_flow",
        direction = "vertical"
    }
    chat_flow.style.vertical_spacing = 6
    chat_flow.style.horizontally_stretchable = true

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

    input_flow.add{
        type = "sprite-button",
        name = "ci_send",
        sprite = "utility/enter",
        style = "tool_button",
        tooltip = "Send"
    }

    -- Restore chat history
    restore_chat(player, chat_flow)
    scroll.scroll_to_bottom()

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
end

-- ============================================================
-- Chat Logic
-- ============================================================

local function save_message(player_index, role, text)
    if not storage.messages[player_index] then
        storage.messages[player_index] = {}
    end
    table.insert(storage.messages[player_index], {
        role = role,
        text = text,
        tick = game.tick
    })
    local msgs = storage.messages[player_index]
    while #msgs > MAX_MESSAGES do
        table.remove(msgs, 1)
    end
end

local function add_chat_message(player, role, text)
    save_message(player.index, role, text)

    local frame = player.gui.screen[GUI_FRAME]
    if not frame or not frame.valid then return end

    local chat_flow = frame["ci_chat_scroll"]["ci_chat_flow"]
    add_message_label(chat_flow, role, text)

    while #chat_flow.children > MAX_MESSAGES do
        chat_flow.children[1].destroy()
    end

    frame["ci_chat_scroll"].scroll_to_bottom()
end

local function set_status(player, status_text)
    local frame = player.gui.screen[GUI_FRAME]
    if not frame or not frame.valid then return end
    frame["ci_status"].caption = status_text
end

local function send_to_bridge(player, message)
    storage.msg_counter = storage.msg_counter + 1
    local payload = {
        id = storage.msg_counter,
        player_index = player.index,
        player_name = player.name,
        message = message,
        tick = game.tick
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

    add_chat_message(player, "user", text)
    set_status(player, "[color=1,0.8,0.2]Thinking...[/color]")
    send_to_bridge(player, text)
end

-- ============================================================
-- Remote Interface (called by bridge via RCON)
-- ============================================================

remote.add_interface("claude_interface", {
    receive_response = function(player_index, text)
        local player = game.get_player(player_index)
        if not player then return end
        add_chat_message(player, "claude", text)
        set_status(player, "[color=0.4,0.8,0.4]Ready[/color]")
    end,

    -- Show tool use in chat
    tool_status = function(player_index, tool_name)
        local player = game.get_player(player_index)
        if not player then return end
        add_chat_message(player, "tool", tool_name)
        set_status(player, "[color=0.6,0.7,1]Using " .. tool_name .. "...[/color]")
    end,

    set_status = function(player_index, status_text)
        local player = game.get_player(player_index)
        if not player then return end
        set_status(player, status_text)
    end,

    clear_chat = function(player_index)
        local player = game.get_player(player_index)
        if not player then return end
        storage.messages[player_index] = {}
        local frame = player.gui.screen[GUI_FRAME]
        if frame and frame.valid then
            frame["ci_chat_scroll"]["ci_chat_flow"].clear()
        end
    end,

    ping = function()
        rcon.print("pong")
    end
})

-- ============================================================
-- Event Handlers
-- ============================================================

local function on_player_created(event)
    local player = game.get_player(event.player_index)
    if player then ensure_top_button(player) end
end

local function setup_all_players()
    init_storage()
    for _, player in pairs(game.players) do
        ensure_top_button(player)
    end
end

script.on_init(setup_all_players)
script.on_configuration_changed(setup_all_players)

script.on_event(defines.events.on_player_created, on_player_created)

script.on_event(defines.events.on_player_joined_game, function(event)
    local player = game.get_player(event.player_index)
    if player then ensure_top_button(player) end
end)

-- Hotkey toggle
script.on_event("claude-interface-toggle", function(event)
    local player = game.get_player(event.player_index)
    if player then toggle_gui(player) end
end)

-- Click handler
script.on_event(defines.events.on_gui_click, function(event)
    if not event.element or not event.element.valid then return end
    local name = event.element.name

    if name == "ci_send" then
        handle_send(game.get_player(event.player_index))
    elseif name == "ci_close" then
        destroy_gui(game.get_player(event.player_index))
    elseif name == TOP_BUTTON then
        toggle_gui(game.get_player(event.player_index))
    elseif name:match("^ci_size_%d$") then
        local idx = tonumber(name:sub(-1))
        local player = game.get_player(event.player_index)
        storage.gui_size[player.index] = idx
        local frame = player.gui.screen[GUI_FRAME]
        if frame and frame.valid then
            apply_size(frame, SIZES[idx])
        end
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
        destroy_gui(game.get_player(event.player_index))
    end
end)
