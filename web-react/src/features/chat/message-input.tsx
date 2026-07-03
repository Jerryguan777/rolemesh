// MessageInput — gradient-border textarea + the four reference footer
// buttons (spec §6.5): Assistants/Agents (picker), New Chat, Recall
// Conversation, Debug Panel. Enter sends, Shift+Enter newline, empty
// submit is a no-op; disabled (muted placeholder) until an agent is
// chosen.

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
    if (e.key !== 'Enter' || e.shiftKey) return;
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
