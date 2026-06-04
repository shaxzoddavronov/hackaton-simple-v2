/* QueryMind UI kit — shared primitives + UISpec renderers */

function StatusBadge({ status }) {
  const s = STATUS[status] || STATUS.pending;
  return (
    <span className="badge" style={{ background: s.wash, color: s.color }}>
      <span className="hdot" style={{ background: s.color }} />{s.label}
    </span>
  );
}

function HealthDot({ ok }) {
  const c = ok === null ? 'var(--status-neutral)' : ok ? 'var(--status-ready)' : 'var(--status-error)';
  const glow = ok ? '0 0 8px rgba(53,211,138,.55)' : 'none';
  return <span className="hdot" style={{ background: c, boxShadow: glow }} />;
}

function highlightSql(sql) {
  const kw = /\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|LIMIT|JOIN|ON|AS|count|sum|avg|DISTINCT|interval|now|date_trunc|union|DESC|ASC|HAVING|AND|OR)\b/g;
  const parts = [];
  let html = sql
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/'[^']*'/g, m => `<span class="s">${m}</span>`)
    .replace(/\b(\d+)\b/g, '<span class="n">$1</span>')
    .replace(kw, m => `<span class="k">${m}</span>`)
    .replace(/(--[^\n]*)/g, '<span style="color:var(--fg-3)">$1</span>');
  return { __html: html };
}

function SqlBlock({ sql }) {
  return (
    <div className="sql-block">
      <div className="sql-head">
        <Icon name="terminal" size={13} /> generated SQL
        <span className="ro">read-only</span>
      </div>
      <div className="sql-code" dangerouslySetInnerHTML={highlightSql(sql)} />
    </div>
  );
}

// ---- KPI ----
function KpiCard({ spec, compact }) {
  const up = (spec.delta ?? 0) >= 0;
  const pts = spec.sparkline || [];
  const max = Math.max(...pts, 1), min = Math.min(...pts, 0);
  const W = 200, H = 38;
  const path = pts.map((p, i) => `${(i / (pts.length - 1)) * W},${H - ((p - min) / (max - min || 1)) * H}`).join(' ');
  return (
    <div className={compact ? '' : 'spec-card'}>
      <div className="kpi-label">{spec.label}</div>
      <div className="kpi-val">{spec.value}{spec.unit && <span className="kpi-unit">{spec.unit}</span>}</div>
      {spec.delta != null && (
        <div className={`kpi-delta ${up ? 'up' : 'down'}`}>
          <Icon name={up ? 'trending-up' : 'trending-down'} size={14} />{up ? '+' : ''}{spec.delta}%
        </div>
      )}
      {pts.length > 1 && (
        <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ marginTop: 14, display: 'block' }}>
          <polyline points={path} fill="none" stroke={up ? 'var(--status-ready)' : 'var(--status-error)'} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
    </div>
  );
}

// ---- Bar ----
function BarChart({ spec, compact }) {
  const yk = spec.y[0];
  const max = Math.max(...spec.data.map(d => d[yk]), 1);
  return (
    <div className={compact ? '' : 'spec-card'}>
      {spec.title && <p className="spec-title">{spec.title}</p>}
      <div className="chart-wrap" style={{ display: 'flex', alignItems: 'flex-end', gap: 14, height: 180, padding: '0 4px' }}>
        {spec.data.map((d, i) => (
          <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, height: '100%', justifyContent: 'flex-end' }}>
            <span style={{ fontFamily: 'var(--qm-font-mono)', fontSize: 11, color: 'var(--fg-1)' }}>{d[yk]}</span>
            <div style={{ width: '100%', maxWidth: 44, height: `${(d[yk] / max) * 100}%`, background: VIZ[i % VIZ.length], borderRadius: '5px 5px 0 0', transition: 'height .5s var(--qm-ease-out)' }} />
            <span style={{ fontSize: 11, color: 'var(--fg-2)' }}>{d[spec.x]}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---- Pie ----
function PieChart({ spec, compact }) {
  const total = spec.data.reduce((a, d) => a + d[spec.value], 0);
  let acc = 0; const R = 70, C = 88, sw = 28;
  const circ = 2 * Math.PI * R;
  return (
    <div className={compact ? '' : 'spec-card'}>
      {spec.title && <p className="spec-title">{spec.title}</p>}
      <div style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
        <svg width={C * 2} height={C * 2} viewBox={`0 0 ${C * 2} ${C * 2}`}>
          {spec.data.map((d, i) => {
            const frac = d[spec.value] / total;
            const dash = `${frac * circ} ${circ}`;
            const off = -acc * circ; acc += frac;
            return <circle key={i} cx={C} cy={C} r={R} fill="none" stroke={VIZ[i % VIZ.length]} strokeWidth={sw}
              strokeDasharray={dash} strokeDashoffset={off} transform={`rotate(-90 ${C} ${C})`} />;
          })}
        </svg>
        <div className="legend" style={{ flexDirection: 'column', gap: 9, marginTop: 0 }}>
          {spec.data.map((d, i) => (
            <div className="legend-item" key={i}>
              <span className="legend-dot" style={{ background: VIZ[i % VIZ.length] }} />
              {d[spec.label]} <span style={{ fontFamily: 'var(--qm-font-mono)', color: 'var(--fg-3)' }}>{Math.round(d[spec.value] / total * 100)}%</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---- Table ----
function DataTable({ spec, compact }) {
  return (
    <div className={compact ? '' : 'spec-card'} style={{ padding: compact ? 0 : '6px', overflowX: 'auto' }}>
      <table className="dtable">
        <thead><tr>{spec.columns.map(c => <th key={c.key} className={c.align === 'right' ? 'num' : ''}>{c.label}</th>)}</tr></thead>
        <tbody>
          {spec.rows.map((r, i) => (
            <tr key={i}>{r.map((cell, j) => {
              const c = spec.columns[j];
              const num = c.align === 'right';
              return <td key={j} className={num ? 'num' : ''}>{num && typeof cell === 'number' ? cell.toLocaleString() : cell}</td>;
            })}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderSpec(spec, compact) {
  switch (spec.type) {
    case 'kpi': return <KpiCard spec={spec} compact={compact} />;
    case 'bar': return <BarChart spec={spec} compact={compact} />;
    case 'pie': return <PieChart spec={spec} compact={compact} />;
    case 'table': return <DataTable spec={spec} compact={compact} />;
    case 'text_only': return <div className="msg-text" style={{ marginTop: 6 }}>{spec.body_md}</div>;
    case 'dashboard':
      return (
        <div className={compact ? '' : 'spec-card'}>
          {spec.title && <p className="spec-title">{spec.title}</p>}
          <div className="dash-grid">
            {spec.children.map((ch, i) => (
              <div key={i} style={{ gridColumn: `span ${ch.span}` }}>{renderSpec(ch.spec, true)}</div>
            ))}
          </div>
        </div>
      );
    default: return null;
  }
}

Object.assign(window, { StatusBadge, HealthDot, SqlBlock, KpiCard, BarChart, PieChart, DataTable, renderSpec });
