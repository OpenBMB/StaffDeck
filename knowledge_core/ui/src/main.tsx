import React from 'react';
import { createRoot } from 'react-dom/client';
import MinimalPage, { type Capabilities } from './MinimalPage';
import { KnowledgeClient } from './client';
import './style.css';
import './standalone.css';
import { EmbeddedKnowledge } from './embedded';
const KnowledgePage = React.lazy(() => import('./KnowledgePage'));

function Standalone() {
  const [required, setRequired] = React.useState<boolean>();
  const [token, setToken] = React.useState(() => sessionStorage.getItem('km_bearer') || '');
  const [connected, setConnected] = React.useState(Boolean(token));
  const [error, setError] = React.useState('');
  const [capabilities, setCapabilities] = React.useState<Capabilities>();
  const client = React.useMemo(() => new KnowledgeClient('', required ? token : undefined), [required, token]);
  React.useEffect(() => {
    fetch('/api/km/config').then(response => response.json()).then(value => { setRequired(value.identity_required); setCapabilities(value); })
      .catch(value => setError(String(value)));
  }, []);
  if (required === undefined) return <div role="status">{error || 'Loading'}</div>;
  if (required && !connected) return <main className="identity-entry"><h1>Knowledge</h1>
    <form onSubmit={event => { event.preventDefault(); void new KnowledgeClient('', token).request('GET', '/identity').then(() => {
      sessionStorage.setItem('km_bearer', token); setConnected(true);
    }).catch(value => setError(value.message)); }}>
      <label>访问令牌<input type="password" aria-label="访问令牌" value={token} onChange={event => setToken(event.target.value)} autoComplete="off" /></label>
      <button type="submit">连接</button>{error && <p role="alert">{error}</p>}
    </form></main>;
  if (capabilities?.modules && !Object.entries(capabilities.modules).filter(([name]) => name !== 'authentication').every(([, enabled]) => enabled)) return <MinimalPage client={client} capabilities={capabilities} />;
  return <React.Suspense fallback={<div role="status">Loading</div>}><KnowledgePage host={{ client,
    canWrite: space => space?.can_write !== false }} /></React.Suspense>;
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>{new URLSearchParams(location.hash.slice(1)).has('channel') ? <EmbeddedKnowledge /> : <Standalone />}</React.StrictMode>,
);
