import { createRoot } from 'react-dom/client';
import { App } from './app/App';
import './styles/base.css';
import './components/ui.css';

// No <StrictMode>: the chat bootstrap is imperative (it may POST a
// conversation as part of resolving a default chat_id) and dev-mode
// double-invoked effects would double-create. Same policy the Lit
// app's imperative bootstrap follows.
createRoot(document.getElementById('root')!).render(<App />);
