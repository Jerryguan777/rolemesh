// MessageInput — gradient-border textarea + the four reference footer
// buttons (spec §6.5): Assistants/Agents (picker), New Chat, Recall
// Conversation, Debug Panel. Enter sends, Shift+Enter newline, empty
// submit is a no-op; disabled (muted placeholder) until an agent is
// chosen.
//
// IME guard (Lit parity, message-editor.ts handleKeyDown): the Enter
// that confirms an IME composition (e.g. committing Latin text from a
// Chinese input method) fires a keydown with `isComposing: true` — it
// must NOT send. keyCode 229 additionally catches Safari, which
// delivers that keydown after compositionend with isComposing already
// false (a variant the Lit fix predates).

import { useState, type KeyboardEvent } from 'react';
import { Clock, History, PanelRight, User } from 'lucide-react';
import { COPY } from '../../app/copy';

export function MessageInput({
  disabled,
  onSend,
  onOpenPicker,
  onNewChat,
  onToggleRecall,
  onToggleDebug,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
  onOpenPicker: () => void;
  onNewChat: () => void;
  onToggleRecall: () => void;
  onToggleDebug: () => void;
}) {
  const [value, setValue] = useState('');

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      e.key !== 'Enter' ||
      e.shiftKey ||
      e.nativeEvent.isComposing ||
      e.keyCode === 229
    )
      return;
    e.preventDefault();
    const text = value.trim();
    if (!text) return;
    onSend(text);
    setValue('');
  }

  return (
    <>
      <div className={`gradient-wrap${disabled ? ' disabled' : ''}`}>
        <textarea
          value={value}
          disabled={disabled}
          placeholder={disabled ? COPY.inputPlaceholderDisabled : COPY.inputPlaceholder}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
        />
      </div>
      <div className="input-footer">
        <div className="fstart">
          <button className="label-btn" onClick={onOpenPicker}>
            <User aria-hidden="true" />
            {COPY.footerAgents}
          </button>
          <button className="label-btn" onClick={onNewChat}>
            <Clock aria-hidden="true" />
            {COPY.footerNewChat}
          </button>
          <button className="label-btn" onClick={onToggleRecall}>
            <History aria-hidden="true" />
            {COPY.footerRecall}
          </button>
        </div>
        <div className="fend">
          <button className="label-btn" onClick={onToggleDebug}>
            <PanelRight aria-hidden="true" />
            {COPY.footerDebug}
          </button>
        </div>
      </div>
    </>
  );
}
