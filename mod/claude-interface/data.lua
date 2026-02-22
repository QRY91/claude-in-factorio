data:extend({
    {
        type = "custom-input",
        name = "claude-interface-toggle",
        key_sequence = "CONTROL + SHIFT + C",
        action = "lua",
        consuming = "none"
    },
    {
        type = "shortcut",
        name = "claude-interface-toggle",
        order = "z[claude]",
        action = "lua",
        associated_control_input = "claude-interface-toggle",
        toggleable = true,
        icon = "__claude-interface__/graphics/q-shortcut.png",
        icon_size = 32,
        small_icon = "__claude-interface__/graphics/q-shortcut.png",
        small_icon_size = 32
    }
})
