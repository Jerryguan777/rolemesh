"""Keybindings management for the coding agent.

Python port of packages/coding-agent/src/core/keybindings.ts.
"""

from __future__ import annotations

from typing import Any, Literal

# Application-level actions (coding agent specific)
AppAction = Literal[
    "interrupt",
    "clear",
    "exit",
    "suspend",
    "cycleThinkingLevel",
    "cycleModelForward",
    "cycleModelBackward",
    "selectModel",
    "expandTools",
    "toggleThinking",
    "toggleSessionNamedFilter",
    "externalEditor",
    "followUp",
    "dequeue",
    "pasteImage",
    "newSession",
    "tree",
    "fork",
    "resume",
]

# Key identifier type (matches TS KeyId)
KeyId = str

# Default application keybindings
DEFAULT_APP_KEYBINDINGS: dict[str, KeyId | list[KeyId]] = {
    "interrupt": "escape",
    "clear": "ctrl+c",
    "exit": "ctrl+d",
    "suspend": "ctrl+z",
    "cycleThinkingLevel": "shift+tab",
    "cycleModelForward": "ctrl+p",
    "cycleModelBackward": "shift+ctrl+p",
    "selectModel": "ctrl+l",
    "expandTools": "ctrl+o",
    "toggleThinking": "ctrl+t",
    "toggleSessionNamedFilter": "ctrl+n",
    "externalEditor": "ctrl+g",
    "followUp": "alt+enter",
    "dequeue": "alt+up",
    "pasteImage": "ctrl+v",
    "newSession": [],
    "tree": [],
    "fork": [],
    "resume": [],
}

# Editor actions are not yet ported - use str
EditorAction = str

# All configurable actions (AppAction | EditorAction)
KeyAction = str

# Full keybindings configuration (app + editor actions)
KeybindingsConfig = dict[str, KeyId | list[KeyId]]

# All default keybindings (app + editor).
# In the TS version this merges DEFAULT_EDITOR_KEYBINDINGS from pi-tui with
# DEFAULT_APP_KEYBINDINGS. For the Python port the editor keybindings are not
# yet available, so this is identical to the app keybindings for now.
DEFAULT_KEYBINDINGS: dict[str, KeyId | list[KeyId]] = {
    **DEFAULT_APP_KEYBINDINGS,
}

# snake_case aliases for parity with TS camelCase exports
default_app_keybindings = DEFAULT_APP_KEYBINDINGS
default_keybindings = DEFAULT_KEYBINDINGS


class KeybindingsManager:
    """Manages all keybindings (app + editor).

    Port of the TS KeybindingsManager class.
    """

    def __init__(self, config: KeybindingsConfig | None = None) -> None:
        self._config: KeybindingsConfig = config or {}
        self._app_action_to_keys: dict[str, list[KeyId]] = {}
        self._build_maps()

    @classmethod
    def create(cls, agent_dir: str | None = None) -> KeybindingsManager:
        """Create from config file."""
        # Simplified: no file loading in Python port yet
        return cls()

    @classmethod
    def in_memory(cls, config: KeybindingsConfig | None = None) -> KeybindingsManager:
        """Create in-memory."""
        return cls(config)

    def _build_maps(self) -> None:
        """Build internal action-to-keys maps."""
        self._app_action_to_keys.clear()
        for action, keys in DEFAULT_APP_KEYBINDINGS.items():
            key_list = keys if isinstance(keys, list) else [keys]
            self._app_action_to_keys[action] = list(key_list)
        for action, keys in self._config.items():
            if keys is not None:
                key_list = keys if isinstance(keys, list) else [keys]
                self._app_action_to_keys[action] = list(key_list)

    def matches(self, data: str, action: str) -> bool:
        """Check if input matches an app action."""
        keys = self._app_action_to_keys.get(action)
        if not keys:
            return False
        return data.lower() in [k.lower() for k in keys]

    def get_keys(self, action: str) -> list[KeyId]:
        """Get keys bound to an app action."""
        return self._app_action_to_keys.get(action, [])

    def get_effective_config(self) -> dict[str, Any]:
        """Get the full effective config."""
        result: dict[str, Any] = dict(DEFAULT_KEYBINDINGS)
        for action, keys in self._config.items():
            if keys is not None:
                result[action] = keys
        return result
