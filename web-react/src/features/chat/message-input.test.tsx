// @vitest-environment happy-dom
//
// The Enter/IME send semantics (Lit parity, message-editor.ts): plain
// Enter sends; an IME-composition Enter (isComposing, or Safari's
// keyCode-229 variant) only commits text and must never send.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MessageInput } from './message-input';

function renderInput() {
  const onSend = vi.fn();
  render(
    <MessageInput
      disabled={false}
      onSend={onSend}
      onOpenPicker={() => {}}
      onNewChat={() => {}}
      onToggleRecall={() => {}}
      onToggleDebug={() => {}}
    />,
  );
  const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
  return { onSend, textarea };
}

afterEach(cleanup);

describe('MessageInput Enter semantics', () => {
  it('plain Enter sends the trimmed text and clears the field', () => {
    const { onSend, textarea } = renderInput();
    fireEvent.change(textarea, { target: { value: '  hello  ' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledWith('hello');
    expect(textarea.value).toBe('');
  });

  it('Shift+Enter does not send (newline)', () => {
    const { onSend, textarea } = renderInput();
    fireEvent.change(textarea, { target: { value: 'hello' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('an IME-composition Enter (isComposing) does not send', () => {
    const { onSend, textarea } = renderInput();
    fireEvent.change(textarea, { target: { value: 'pinyin draft' } });
    fireEvent.keyDown(textarea, { key: 'Enter', isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe('pinyin draft');
  });

  it("Safari's keyCode-229 composition Enter does not send", () => {
    const { onSend, textarea } = renderInput();
    fireEvent.change(textarea, { target: { value: 'pinyin draft' } });
    fireEvent.keyDown(textarea, { key: 'Enter', keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('empty submit is a no-op', () => {
    const { onSend, textarea } = renderInput();
    fireEvent.change(textarea, { target: { value: '   ' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
  });
});
