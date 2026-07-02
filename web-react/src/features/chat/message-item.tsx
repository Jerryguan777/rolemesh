// MessageItem — one timeline row: bubble + avatar + `.ts-col` cell
// (prototype .msg-item / .msg-row). User text renders as plain
// pre-wrapped text in the grey bubble; assistant turns render through
// the markdown pipeline with the brand-mark avatar.

import type { ReactNode } from 'react';
import { BrandMark } from '../../components/brand-mark';
import { Markdown } from '../../components/markdown';

export function MessageItem({
  role,
  content,
  tsLabel,
  initials,
}: {
  role: 'user' | 'assistant';
  content: string;
  tsLabel: string;
  initials: string;
}) {
  const row =
    role === 'user' ? (
      <div className="msg-row end">
        <div className="bubble user">
          <div className="md row-line">{content}</div>
        </div>
        <div className="avatar user">
          <div className="profile-icon">
            <span>{initials}</span>
          </div>
        </div>
      </div>
    ) : (
      <div className="msg-row start">
        <div className="avatar assistant">
          <BrandMark size="100%" />
        </div>
        <div className="bubble assistant">
          <div className="row-line">
            <Markdown text={content} />
          </div>
        </div>
      </div>
    );
  return <TimelineItem tsLabel={tsLabel}>{row}</TimelineItem>;
}

/** Shared timeline wrapper so non-message rows (errors, notices) sit
 *  on the same rail. */
export function TimelineItem({
  tsLabel,
  children,
}: {
  tsLabel: string;
  children: ReactNode;
}) {
  return (
    <div className="msg-item">
      <div className="msg-slot">{children}</div>
      <div className="ts-col">
        <span className="ts-dot" />
        {tsLabel}
      </div>
    </div>
  );
}
