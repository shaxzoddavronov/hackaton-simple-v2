/* QueryMind UI kit — login + app shell + top-level App */

function Login({ onLogin }) {
  const [mode, setMode] = React.useState('login');
  return (
    <div className="login">
      <div className="login-card">
        <div className="login-brand">
          <img src="../../assets/qm-mark.svg" alt="" />
          <span className="login-word">QueryMind<span className="ai">AI</span></span>
        </div>
        <p className="login-tag">Ask your databases, documents & APIs in your own language.</p>
        <div className="login-box">
          {mode === 'register' && (
            <div className="field"><label>Username</label><input defaultValue="" placeholder="jdoe" /></div>
          )}
          <div className="field"><label>{mode === 'login' ? 'Username or email' : 'Email'}</label><input defaultValue="jdoe@acme.io" /></div>
          <div className="field" style={{ marginBottom: 20 }}><label>Password</label><input type="password" defaultValue="superlongpassword" /></div>
          <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center', padding: 11 }} onClick={onLogin}>
            {mode === 'login' ? 'Sign in' : 'Create account'}<Icon name="arrow-right" size={16} />
          </button>
          <p className="login-sub">
            {mode === 'login'
              ? <React.Fragment>No account? <a onClick={() => setMode('register')}>Register</a></React.Fragment>
              : <React.Fragment>Have an account? <a onClick={() => setMode('login')}>Sign in</a></React.Fragment>}
          </p>
        </div>
      </div>
    </div>
  );
}

function WorkspaceRail({ activeWs, setActiveWs }) {
  return (
    <div className="rail">
      <img className="rail-logo" src="../../assets/qm-mark.svg" alt="QueryMind" />
      <div className="rail-sep" />
      {WORKSPACES.map(w => (
        <div key={w.id} className={`ws-avatar ${activeWs === w.id ? 'active' : ''}`} title={w.name} onClick={() => setActiveWs(w.id)}>{w.short}</div>
      ))}
      <button className="rail-btn" title="New workspace"><Icon name="plus" size={18} /></button>
      <div className="rail-spacer" />
      <button className="rail-btn" style={{ border: 'none' }} title="Settings"><Icon name="settings" size={19} /></button>
      <div className="avatar" title="John Doe">JD</div>
    </div>
  );
}

function Sidebar({ ws, activeSession, setActiveSession, onNewChat }) {
  return (
    <div className="sidebar">
      <div className="sb-head">
        <div className="sb-ws">
          <span className="hdot" style={{ background: 'var(--status-ready)', width: 8, height: 8 }} />
          <div style={{ flex: 1 }}>
            <div className="sb-ws-name">{ws.name}</div>
            <div className="sb-ws-meta">{ws.connCount} connections · ready</div>
          </div>
          <Icon name="chevron-down" size={16} color="var(--fg-3)" />
        </div>
      </div>
      <button className="new-chat" onClick={onNewChat}><Icon name="plus" size={16} /> New chat</button>
      <div className="sb-scroll">
        {Object.entries(SESSIONS).map(([group, items]) => (
          <div key={group}>
            <div className="sb-group">{group}</div>
            {items.map(s => (
              <div key={s.id} className={`sb-item ${activeSession === s.id ? 'active' : ''}`} onClick={() => setActiveSession(s.id)}>
                <Icon name="message-square" size={15} /><span className="t">{s.title}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

const TABS = [
  { id: 'chat', label: 'Chat', icon: 'messages-square' },
  { id: 'connections', label: 'Connections', icon: 'database', count: 3 },
  { id: 'documents', label: 'Documents', icon: 'file-text', count: 4 },
  { id: 'dashboards', label: 'Dashboards', icon: 'layout-dashboard', count: 4 },
  { id: 'usage', label: 'Usage', icon: 'activity' },
];

function TopBar({ tab, setTab }) {
  return (
    <div className="topbar">
      {TABS.map(t => (
        <button key={t.id} className={`tab ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
          <Icon name={t.icon} size={16} />{t.label}{t.count != null && <span className="count">{t.count}</span>}
        </button>
      ))}
      <div className="topbar-spacer" />
      {tab === 'chat' && (
        <button className="conn-pill"><Icon name="database" size={14} color="var(--accent)" /> pg-quiz <Icon name="chevron-down" size={14} color="var(--fg-3)" /></button>
      )}
    </div>
  );
}

function App() {
  const [authed, setAuthed] = React.useState(false);
  const [activeWs, setActiveWs] = React.useState('w1');
  const [tab, setTab] = React.useState('chat');
  const [activeSession, setActiveSession] = React.useState('s1');
  const [schemaConn, setSchemaConn] = React.useState(null);
  const [chatKey, setChatKey] = React.useState(0);

  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  const ws = WORKSPACES.find(w => w.id === activeWs);

  let main;
  if (schemaConn) main = <SchemaView conn={schemaConn} onBack={() => setSchemaConn(null)} />;
  else if (tab === 'chat') main = <ChatView key={chatKey} />;
  else if (tab === 'connections') main = <ConnectionsView onSchema={c => setSchemaConn(c)} />;
  else if (tab === 'documents') main = <DocumentsView />;
  else if (tab === 'dashboards') main = <DashboardsView />;
  else if (tab === 'usage') main = <UsageView />;

  return (
    <div className="app">
      <WorkspaceRail activeWs={activeWs} setActiveWs={id => { setActiveWs(id); setSchemaConn(null); }} />
      <Sidebar ws={ws} activeSession={activeSession} setActiveSession={setActiveSession}
        onNewChat={() => { setTab('chat'); setSchemaConn(null); setChatKey(k => k + 1); }} />
      <div className="main">
        <TopBar tab={schemaConn ? 'connections' : tab} setTab={t => { setTab(t); setSchemaConn(null); }} />
        {main}
      </div>
    </div>
  );
}

Object.assign(window, { App, Login, WorkspaceRail, Sidebar, TopBar, TABS });
