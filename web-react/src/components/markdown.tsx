// Markdown — react-markdown + remark-gfm + rehype-sanitize +
// highlight.js (spec §2). rehype-sanitize is the safety layer for
// streaming model output; render parity is governed by the prototype's
// `.md` CSS in md.css, not by matching web/'s marked output.
//
// Highlighting happens INSIDE the code renderer (after sanitize), so
// sanitize never sees or strips hljs markup. hljs.highlight escapes
// the code text itself, so the injected HTML is hljs's own.

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';
// The `common` build registers ~40 mainstream languages (~10% of the
// full bundle); unknown languages fall back to unhighlighted code.
import hljs from 'highlight.js/lib/common';
import 'highlight.js/styles/github.css';
import './md.css';

function CodeRenderer({
  className,
  children,
}: {
  className?: string;
  children?: React.ReactNode;
}) {
  const lang = /language-(\w+)/.exec(className ?? '')?.[1];
  const text = String(children ?? '');
  if (lang && hljs.getLanguage(lang)) {
    return (
      <code
        className={className}
        // hljs.highlight HTML-escapes the source text; this is hljs
        // markup only, never raw model output.
        dangerouslySetInnerHTML={{
          __html: hljs.highlight(text, { language: lang }).value,
        }}
      />
    );
  }
  return <code className={className}>{children}</code>;
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{ code: CodeRenderer }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
