/* QueryMind UI kit — workspace views: Connections, Documents, Usage, Dashboards, Schema */

function AddConnectionModal({ onClose }) {
  const [dialect, setDialect] = React.useState('postgres');
  const [tested, setTested] = React.useState('idle'); // idle | running | ok
  function test() { setTested('running'); setTimeout(() => setTested('ok'), 1100); }
  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Add connection</span>
          <button className="icon-btn" onClick={onClose}><Icon name="x" size={18} /></button>
        </div>
        <div className="modal-body">
          <div className="field">
            <label>Dialect</label>
            <div className="dialect-grid">
              {['postgres','mysql','snowflake','bigquery','mongodb','elasticsearch','rest_api','graphql','clickhouse'].map(d => (
                <div key={d} className={`dialect-opt ${dialect === d ? 'sel' : ''}`} onClick={() => { setDialect(d); setTested('idle'); }}>
                  <Icon name={DIALECT_ICON[d] || 'database'} size={20} /><span>{d}</span>
                </div>
              ))}
            </div>
          </div>
          {dialect === 'rest_api' ? (
            <React.Fragment>
              <div className="field"><label>Base URL</label><input defaultValue="https://acme.bitrix24.io/rest" /></div>
              <div className="field-row">
                <div className="field"><label>Preset</label><select defaultValue="bitrix24"><option>generic</option><option>bitrix24</option><option>amocrm</option><option>hubspot</option><option>1c_odata</option></select></div>
                <div className="field"><label>Auth</label><select><option>api_key</option><option>bearer</option><option>basic</option><option>oauth2_client</option></select></div>
              </div>
            </React.Fragment>
          ) : (
            <React.Fragment>
              <div className="field-row">
                <div className="field"><label>Host</label><input defaultValue="db.internal.acme.io" /></div>
                <div className="field"><label>Port</label><input defaultValue="5432" /></div>
              </div>
              <div className="field-row">
                <div className="field"><label>Database</label><input defaultValue="analytics" /></div>
                <div className="field"><label>User</label><input defaultValue="querymind_ro" /></div>
              </div>
              <div className="field"><label>Password</label><input type="password" defaultValue="superlongpassword" /></div>
            </React.Fragment>
          )}
          {tested === 'running' && <div className="test-result run"><Icon name="loader" size={15} /> Probing connection…</div>}
          {tested === 'ok' && <div className="test-result ok"><Icon name="check-circle-2" size={15} /> Connection OK · 38 ms · 142 tables found</div>}
        </div>
        <div className="modal-foot">
          <button className="btn btn-secondary" onClick={test}><Icon name="plug-zap" size={15} /> Test connection</button>
          <button className="btn btn-primary" disabled={tested !== 'ok'} style={tested !== 'ok' ? { opacity: .5 } : {}} onClick={onClose}>Save</button>
        </div>
      </div>
    </div>
  );
}

function ConnectionCard({ c, onSchema }) {
  return (
    <div className="conn-card">
      <div className="cc-top">
        <div className="cc-icon"><Icon name={DIALECT_ICON[c.dialect] || 'database'} size={19} /></div>
        <div style={{ minWidth: 0 }}>
          <div className="cc-name">{c.name}</div>
          <div className="cc-dialect">{c.dialect}{c.tables ? ` · ${c.tables} tables` : ''}</div>
        </div>
        <span style={{ marginLeft: 'auto' }}><StatusBadge status={c.status} /></span>
      </div>
      <div className="cc-meta">
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}><HealthDot ok={c.health} />{c.latency != null ? `${c.latency} ms` : 'unreachable'}</span>
        <div className="cc-actions">
          <button className="icon-btn" title="Recheck health" style={{ width: 30, height: 30 }}><Icon name="refresh-cw" size={15} /></button>
          <button className="icon-btn" title="View schema" style={{ width: 30, height: 30 }} onClick={() => onSchema(c)}><Icon name="table-2" size={15} /></button>
          <button className="icon-btn" title="Delete" style={{ width: 30, height: 30 }}><Icon name="trash-2" size={15} /></button>
        </div>
      </div>
    </div>
  );
}

function ConnectionsView({ onSchema }) {
  const [adding, setAdding] = React.useState(false);
  return (
    <div className="view">
      <div className="view-inner">
        <div className="view-head">
          <div><h1 className="view-title">Connections</h1><p className="view-sub">Databases & APIs in this workspace · 13 dialects supported</p></div>
          <button className="btn btn-primary" onClick={() => setAdding(true)}><Icon name="plus" size={16} /> Add connection</button>
        </div>
        <div className="card-grid">
          {CONNECTIONS.map(c => <ConnectionCard key={c.id} c={c} onSchema={onSchema} />)}
        </div>
      </div>
      {adding && <AddConnectionModal onClose={() => setAdding(false)} />}
    </div>
  );
}

function DocumentsView() {
  return (
    <div className="view">
      <div className="view-inner">
        <div className="view-head">
          <div><h1 className="view-title">Documents</h1><p className="view-sub">Sources crawled into the RAG index · 9 source kinds</p></div>
          <button className="btn btn-primary"><Icon name="plus" size={16} /> Add source</button>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {DOC_SOURCES.map(d => (
            <div className="conn-card" key={d.id} style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
              <div className="cc-icon"><Icon name={d.icon} size={19} /></div>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="cc-name" style={{ fontFamily: 'var(--qm-font-body)', fontWeight: 600 }}>{d.name}</div>
                <div className="cc-dialect">{d.kind} · {d.docs.toLocaleString()} docs · crawled {d.when}</div>
              </div>
              <StatusBadge status={d.status} />
              <button className="btn btn-secondary" style={{ padding: '7px 12px', fontSize: 12.5 }}><Icon name="refresh-cw" size={14} /> Crawl now</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function UsageView() {
  const stats = [
    { l: 'LLM calls', v: '8,412' }, { l: 'Tokens (in/out)', v: '3.1M' },
    { l: 'Queries ok', v: '4,188' }, { l: 'RAG + cache hits', v: '1,902' },
  ];
  const bars = [42,55,38,61,72,49,80,66,58,74,90,63,70];
  const max = Math.max(...bars);
  return (
    <div className="view">
      <div className="view-inner">
        <div className="view-head"><div><h1 className="view-title">Usage</h1><p className="view-sub">Last 30 days · per-workspace rollup</p></div>
          <select className="conn-pill" style={{ border: '1px solid var(--border)' }} defaultValue="30"><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option></select>
        </div>
        <div className="stat-grid">{stats.map(s => <div className="stat" key={s.l}><div className="l">{s.l}</div><div className="v">{s.v}</div></div>)}</div>
        <div className="spec-card" style={{ marginTop: 0 }}>
          <p className="spec-title">LLM calls per day</p>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 6, height: 120 }}>
            {bars.map((b, i) => <div key={i} style={{ flex: 1, height: `${(b / max) * 100}%`, background: i === bars.length - 1 ? 'var(--accent)' : 'var(--qm-blue-400)', opacity: i === bars.length - 1 ? 1 : .55, borderRadius: '4px 4px 0 0' }} />)}
          </div>
        </div>
      </div>
    </div>
  );
}

function DashboardsView() {
  const dash = { type: 'dashboard', title: '', children: [
    { span: 4, spec: ANSWERS.kpi.spec },
    { span: 8, spec: ANSWERS.bar.spec },
    { span: 12, spec: ANSWERS.federated.spec },
  ] };
  return (
    <div className="view">
      <div className="view-inner">
        <div className="view-head">
          <div><h1 className="view-title">Q2 Growth</h1><p className="view-sub">6 saved questions · re-run live on render</p></div>
          <div style={{ display: 'flex', gap: 10 }}>
            <button className="btn btn-secondary"><Icon name="calendar-clock" size={15} /> Schedule report</button>
            <button className="btn btn-primary"><Icon name="rotate-cw" size={15} /> Run all</button>
          </div>
        </div>
        {renderSpec(dash, false)}
      </div>
    </div>
  );
}

function SchemaView({ conn, onBack }) {
  const tables = [
    { name: 'users', cols: [['id','int','pk'],['username','string',''],['email','string',''],['created_at','datetime','']] },
    { name: 'events', cols: [['id','int','pk'],['user_id','int','fk'],['kind','string',''],['ts','datetime','']] },
    { name: 'orders', cols: [['id','int','pk'],['user_id','int','fk'],['amount','float',''],['region','string','']] },
  ];
  const [active, setActive] = React.useState('events');
  const t = tables.find(x => x.name === active);
  return (
    <div className="view" style={{ overflow: 'hidden' }}>
      <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12 }}>
        <button className="btn btn-ghost" style={{ padding: '6px 10px' }} onClick={onBack}><Icon name="arrow-left" size={15} /> Connections</button>
        <span style={{ fontFamily: 'var(--qm-font-mono)', fontSize: 14, color: 'var(--fg-0)' }}>{conn?.name || 'pg-quiz'}</span>
        <StatusBadge status="ready" />
      </div>
      <div className="schema" style={{ height: 'calc(100% - 56px)' }}>
        <div className="schema-tree">
          {tables.map(tb => (
            <div className="tree-table" key={tb.name}>
              <div className={`tree-row ${active === tb.name ? 'active' : ''}`} onClick={() => setActive(tb.name)}>
                <Icon name="table-2" size={15} />{tb.name}<span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--fg-3)' }}>{tb.cols.length}</span>
              </div>
            </div>
          ))}
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>
          <h1 className="view-title" style={{ fontSize: 20, marginBottom: 4 }}>{t.name}</h1>
          <p className="view-sub" style={{ marginBottom: 18 }}>{t.cols.length} columns · sampled distinct values + FKs</p>
          <table className="dtable" style={{ background: 'var(--bg-2)', border: '1px solid var(--border-subtle)', borderRadius: 14 }}>
            <thead><tr><th>column</th><th>type</th><th>key</th><th>sample</th></tr></thead>
            <tbody>
              {t.cols.map(c => (
                <tr key={c[0]}><td style={{ color: 'var(--accent)' }}>{c[0]}</td><td style={{ color: 'var(--fg-2)' }}>{c[1]}</td>
                  <td>{c[2] ? <span style={{ color: c[2] === 'pk' ? 'var(--status-auth)' : 'var(--status-activity)', fontSize: 11 }}>{c[2].toUpperCase()}</span> : ''}</td>
                  <td style={{ color: 'var(--fg-3)' }}>{c[1] === 'int' ? '1, 2, 3…' : c[1] === 'datetime' ? '2026-06-01…' : c[1] === 'float' ? '12.40…' : 'sample…'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { ConnectionsView, DocumentsView, UsageView, DashboardsView, SchemaView, AddConnectionModal, ConnectionCard });
