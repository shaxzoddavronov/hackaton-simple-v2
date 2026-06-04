/* QueryMind UI kit — Chat view: messages, agent thinking stream, composer */

function pickAnswer(text) {
  const t = text.toLowerCase();
  if (t.includes('region') || t.includes('revenue')) return ANSWERS.bar;
  if (t.includes('compare') || t.includes('search') || t.includes('volume')) return ANSWERS.federated;
  if (t.startsWith('/')) return null;
  return ANSWERS.kpi;
}

function AgentThinking({ nodes, idx }) {
  return (
    <div>
      <div className="thinking">
        <span className="dots"><span /><span /><span /></span>
        <span>Working · <span className="think-node">{nodes[idx] || 'render'}</span></span>
      </div>
      <div className="node-trace">
        {nodes.map((n, i) => <span key={n} className={`node-chip ${i < idx ? 'done' : ''}`}>{i < idx ? '✓ ' : ''}{n}</span>)}
      </div>
    </div>
  );
}

function CitationStrip({ citations }) {
  const iconFor = k => ({ user_doc: 'file-text', api_endpoint: 'braces', qa_history: 'history', harvested_doc: 'file' }[k] || 'file');
  return (
    <div className="cite-strip">
      {citations.map((c, i) => (
        <span className="cite" key={i} title={`${c.kind} · score ${c.score}`}>
          <Icon name={iconFor(c.kind)} size={13} />{c.source_key}<span className="score">{c.score}</span>
        </span>
      ))}
    </div>
  );
}

function AssistantMessage({ m }) {
  return (
    <div className="msg">
      <div className="msg-ava bot"><Icon name="sparkles" size={16} /></div>
      <div className="msg-body">
        <div className="msg-role">QueryMind</div>
        {m.thinking
          ? <AgentThinking nodes={m.nodes} idx={m.nodeIndex} />
          : (
            <div>
              {m.federation && (
                <div className="fed-badge">
                  <Icon name="git-merge" size={13} /> Queried: {m.federation.map((f, i) => (
                    <React.Fragment key={f.name}>{i > 0 && ' · '}<b>{f.name}</b> {f.rows} rows</React.Fragment>
                  ))}
                </div>
              )}
              {m.headline && <div className="msg-text">{m.headline}</div>}
              {m.spec && renderSpec(m.spec)}
              {m.sql && <SqlBlock sql={m.sql} />}
              <div className="answer-tools">
                <button className="btn btn-secondary" style={{ padding: '6px 11px', fontSize: 12 }}><Icon name="star" size={13} /> Star</button>
                <button className="btn btn-ghost" style={{ padding: '6px 11px', fontSize: 12 }}><Icon name="download" size={13} /> Export</button>
                <button className="btn btn-ghost" style={{ padding: '6px 11px', fontSize: 12 }}><Icon name="copy" size={13} /> Copy SQL</button>
              </div>
              {m.citations && m.citations.length > 0 && <CitationStrip citations={m.citations} />}
            </div>
          )}
      </div>
    </div>
  );
}

function UserMessage({ m }) {
  return (
    <div className="msg">
      <div className="msg-ava user">JD</div>
      <div className="msg-body">
        <div className="msg-role">You</div>
        <div className="msg-text user-q">{m.text}</div>
      </div>
    </div>
  );
}

function Composer({ onSend }) {
  const [val, setVal] = React.useState('');
  const [focus, setFocus] = React.useState(false);
  const taRef = React.useRef(null);
  const showSlash = val.startsWith('/');
  const filtered = SLASH.filter(s => s.cmd.startsWith(val.split(' ')[0]));

  function send() {
    const v = val.trim();
    if (!v) return;
    onSend(v);
    setVal('');
    if (taRef.current) taRef.current.style.height = 'auto';
  }
  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  }
  function grow(e) {
    setVal(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 140) + 'px';
  }
  return (
    <div className="composer-wrap">
      <div className="composer-inner">
        {showSlash && filtered.length > 0 && (
          <div className="slash-menu">
            {filtered.map((s, i) => (
              <div className={`slash-item ${i === 0 ? 'active' : ''}`} key={s.cmd} onClick={() => setVal(s.cmd + ' ')}>
                <span className="slash-cmd">{s.cmd}</span><span className="slash-desc">{s.desc}</span>
              </div>
            ))}
          </div>
        )}
        {!showSlash && (
          <div className="sim-rail">
            {SIMILAR.map(s => <span className="sim-chip" key={s} onClick={() => onSend(s)}><Icon name="rotate-cw" size={12} />{s}</span>)}
          </div>
        )}
        <div className={`composer ${focus ? 'focus' : ''}`}>
          <textarea ref={taRef} rows={1} placeholder="Ask in Uzbek, Russian, or English…  Type / for commands"
            value={val} onChange={grow} onKeyDown={onKey} onFocus={() => setFocus(true)} onBlur={() => setFocus(false)} />
          <div className="composer-actions">
            <button className="icon-btn" title="Attach document"><Icon name="paperclip" size={18} /></button>
            <button className="send-btn" disabled={!val.trim()} onClick={send} title="Send"><Icon name="arrow-up" size={18} /></button>
          </div>
        </div>
        <div className="composer-hint">
          <span><kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline</span>
          <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6 }}><Icon name="globe" size={13} /> Answers mirror your language</span>
        </div>
      </div>
    </div>
  );
}

function EmptyChat({ onSend }) {
  const starters = ['How many active users in the last 30 days?', 'Revenue by region this quarter', 'Compare DB quiz volume with ES search volume'];
  return (
    <div className="empty" style={{ paddingTop: 90 }}>
      <Icon name="sparkles" size={34} />
      <h3>Ask your data anything</h3>
      <p>QueryMind plans the query, runs it against your connection, and returns a typed answer with citations.</p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, width: 380, maxWidth: '100%' }}>
        {starters.map(s => (
          <button key={s} className="btn btn-secondary" style={{ justifyContent: 'flex-start', width: '100%' }} onClick={() => onSend(s)}>
            <Icon name="corner-down-right" size={15} />{s}
          </button>
        ))}
      </div>
    </div>
  );
}

function ChatView() {
  const [messages, setMessages] = React.useState([]);
  const scrollRef = React.useRef(null);
  const timers = React.useRef([]);

  React.useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [messages]);
  React.useEffect(() => () => timers.current.forEach(clearTimeout), []);

  function handleSend(text) {
    const ans = pickAnswer(text);
    const userMsg = { id: Date.now() + 'u', role: 'user', text };

    if (!ans) { // slash command — short stream, text_only
      setMessages(m => [...m, userMsg, { id: Date.now() + 'a', role: 'assistant', thinking: false,
        spec: { type: 'text_only', body_md: 'Command list: /help · /sql · /lang · /clear-cache · /refresh-schema · /explain' }, citations: [] }]);
      return;
    }
    const aId = Date.now() + 'a';
    setMessages(m => [...m, userMsg, { id: aId, role: 'assistant', thinking: true, nodes: ans.nodes, nodeIndex: 0 }]);

    ans.nodes.forEach((_, i) => {
      timers.current.push(setTimeout(() => {
        setMessages(m => m.map(x => x.id === aId ? { ...x, nodeIndex: i + 1 } : x));
      }, 480 * (i + 1)));
    });
    timers.current.push(setTimeout(() => {
      setMessages(m => m.map(x => x.id === aId ? {
        ...x, thinking: false, spec: ans.spec, sql: ans.sql, citations: ans.citations || [], federation: ans.federation,
        headline: ans.spec.type === 'kpi' ? null : 'Here\u2019s what I found:',
      } : x));
    }, 480 * (ans.nodes.length + 1)));
  }

  return (
    <div className="chat">
      <div className="chat-scroll" ref={scrollRef}>
        <div className="chat-inner">
          {messages.length === 0 ? <EmptyChat onSend={handleSend} /> :
            messages.map(m => m.role === 'user' ? <UserMessage key={m.id} m={m} /> : <AssistantMessage key={m.id} m={m} />)}
        </div>
      </div>
      <Composer onSend={handleSend} />
    </div>
  );
}

Object.assign(window, { ChatView, UserMessage, AssistantMessage, Composer });
