// <App/> — auth state machine + outermost host (port of web/'s
// <rm-app>). Nothing behind the shell mounts until auth resolves and
// the Me cache is populated (atomic bootstrap).

import { HashRouter } from 'react-router-dom';
import { AppShell } from '../features/shell/app-shell';
import { LoginPage } from './login-page';
import { Providers } from './providers';
import { AppRoutes } from './routes';
import { useAuth } from './use-auth';

export function App() {
  const auth = useAuth();

  if (auth === 'loading') {
    return (
      <div
        style={{
          height: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--rm-text-muted)',
        }}
      >
        Loading...
      </div>
    );
  }
  if (auth === 'login') {
    return <LoginPage />;
  }
  return (
    <Providers>
      <HashRouter>
        <AppShell>
          <AppRoutes />
        </AppShell>
      </HashRouter>
    </Providers>
  );
}
