// AppShell — flex shell: sidebar + main (prototype `.app`). Owns the
// router outlet; every routed page renders inside `.main`.

import type { ReactNode } from 'react';
import { AppSidebar } from './app-sidebar';
import './shell.css';

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app">
      <AppSidebar />
      <main className="main">{children}</main>
    </div>
  );
}
