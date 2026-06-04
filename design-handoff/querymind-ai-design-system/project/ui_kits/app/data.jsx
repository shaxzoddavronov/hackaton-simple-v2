/* QueryMind UI kit — Icon (Lucide via CDN) + shared fake data */

// Icon: renders a Lucide icon by injecting SVG into a React-owned <span>.
// React never diffs the inner <svg>, avoiding reconciliation issues.
function Icon({ name, size = 18, color, style, className = '' }) {
  const ref = React.useRef(null);
  React.useEffect(() => {
    const el = ref.current;
    if (!el || !window.lucide) return;
    el.innerHTML = '';
    const i = document.createElement('i');
    i.setAttribute('data-lucide', name);
    el.appendChild(i);
    try {
      window.lucide.createIcons({ attrs: { width: size, height: size, 'stroke-width': 1.75 } });
    } catch (e) {}
  }, [name, size]);
  return <span ref={ref} className={`ic ${className}`} style={{ color, width: size, height: size, ...style }} />;
}

// ----- status helpers -----
const STATUS = {
  ready:      { color: 'var(--status-ready)',    wash: 'var(--qm-emerald-050)', label: 'ready' },
  profiling:  { color: 'var(--status-activity)', wash: 'var(--qm-blue-050)',    label: 'profiling' },
  harvesting: { color: 'var(--status-activity)', wash: 'var(--qm-blue-050)',    label: 'harvesting' },
  error:      { color: 'var(--status-error)',    wash: 'var(--qm-rose-050)',    label: 'error' },
  auth_error: { color: 'var(--status-auth)',     wash: 'var(--qm-amber-050)',   label: 'auth_error' },
  pending:    { color: 'var(--status-neutral)',  wash: 'var(--qm-slate-050)',   label: 'pending' },
  idle:       { color: 'var(--status-neutral)',  wash: 'var(--qm-slate-050)',   label: 'idle' },
};
const VIZ = ['#34DCCB', '#4F9CF9', '#9B8CFF', '#F5B13F', '#35D38A', '#FB6B7C', '#2BB6A6', '#F08FC0'];

// dialect -> lucide icon
const DIALECT_ICON = {
  postgres: 'database', mysql: 'database', sqlite: 'database', clickhouse: 'database',
  oracle: 'database', duckdb: 'database', mssql: 'database', snowflake: 'snowflake',
  bigquery: 'database', mongodb: 'leaf', elasticsearch: 'search', rest_api: 'braces', graphql: 'share-2',
};
const DIALECTS = ['postgres','mysql','sqlite','clickhouse','snowflake','bigquery','mongodb','elasticsearch','rest_api','graphql','mssql','oracle','duckdb'];

// ----- workspaces / connections -----
const WORKSPACES = [
  { id: 'w1', name: 'Growth Analytics', short: 'GA', status: 'ready', connCount: 3 },
  { id: 'w2', name: 'Support Ops', short: 'SO', status: 'profiling', connCount: 2 },
  { id: 'w3', name: 'Finance', short: 'F', status: 'ready', connCount: 4 },
];

const CONNECTIONS = [
  { id: 'c1', name: 'pg-quiz', dialect: 'postgres', status: 'ready', health: true, latency: 38, tables: 142 },
  { id: 'c2', name: 'es-search', dialect: 'elasticsearch', status: 'profiling', health: true, latency: 61, tables: 24 },
  { id: 'c3', name: 'crm-bitrix24', dialect: 'rest_api', status: 'auth_error', health: false, latency: null, tables: 0 },
];

const DOC_SOURCES = [
  { id: 'd1', kind: 'folder', name: '/data/policies', status: 'ready', docs: 312, when: '4 min ago', icon: 'folder' },
  { id: 'd2', kind: 'url_list', name: 'docs.querymind.io (38 urls)', status: 'harvesting', docs: 21, when: 'now', icon: 'globe' },
  { id: 'd3', kind: 'imap', name: 'support@acme · INBOX', status: 'ready', docs: 1840, when: '1 hr ago', icon: 'mail' },
  { id: 'd4', kind: 'slack', name: 'acme-workspace export', status: 'idle', docs: 0, when: '—', icon: 'message-square' },
];

const SESSIONS = {
  Today: [
    { id: 's1', title: 'Most active users last 30 days', icon: 'message-square' },
    { id: 's2', title: 'Revenue by region Q2', icon: 'message-square' },
  ],
  Yesterday: [
    { id: 's3', title: 'Failed payments breakdown', icon: 'message-square' },
    { id: 's4', title: 'eng faol foydalanuvchilar', icon: 'message-square' },
  ],
  'Last 7 days': [
    { id: 's5', title: 'Schema overview: pg-quiz', icon: 'message-square' },
    { id: 's6', title: 'Search volume vs DB volume', icon: 'message-square' },
  ],
};

const SLASH = [
  { cmd: '/help', desc: 'Show the command list' },
  { cmd: '/sql', desc: 'Echo the SQL of the most recent answer' },
  { cmd: '/lang uz|ru|en', desc: 'Hint the preferred answer language' },
  { cmd: '/clear-cache', desc: 'Drop the query cache for this connection' },
  { cmd: '/refresh-schema', desc: 'Re-profile the current connection' },
  { cmd: '/explain', desc: 'How the agent answers, briefly' },
];

const AGENT_NODES = ['route','plan','validate','execute','federate','render'];

const SIMILAR = [
  'active users last 30 days',
  'eng faol foydalanuvchilar',
];

// ----- canned answers for the chat demo -----
const ANSWERS = {
  kpi: {
    userText: 'How many active users in the last 30 days?',
    sql: `SELECT count(DISTINCT user_id) AS active_users\nFROM events\nWHERE ts > now() - interval '30 days';`,
    nodes: ['route','plan','validate','execute','render'],
    citations: [],
    spec: { type: 'kpi', label: 'Active users · 30d', value: '12,408', unit: null, delta: 12.4, sparkline: [28,24,26,16,18,9,11,4] },
  },
  bar: {
    userText: 'Revenue by region this quarter',
    sql: `SELECT region, sum(amount) AS revenue\nFROM orders\nWHERE created_at >= date_trunc('quarter', now())\nGROUP BY region ORDER BY revenue DESC;`,
    nodes: ['route','plan','validate','execute','render'],
    citations: [{ kind: 'user_doc', source_key: 'q2-targets.xlsx', score: 0.88 }],
    spec: { type: 'bar', title: 'Revenue by region · Q2', x: 'region', y: ['revenue'],
      data: [{region:'EU',revenue:482},{region:'NA',revenue:413},{region:'APAC',revenue:298},{region:'LATAM',revenue:156},{region:'MEA',revenue:92}] },
  },
  federated: {
    userText: 'Compare DB quiz volume with ES search volume',
    sql: `-- FederatedPlan: union(pg_quiz, es_search)\nSELECT source, count(*) AS n FROM ... ;`,
    nodes: ['route','plan','validate','execute','federate','render'],
    federation: [{ name: 'pg-quiz', rows: 12 }, { name: 'es-search', rows: 30 }],
    citations: [{ kind: 'api_endpoint', source_key: 'GET /search/_count', score: 0.83 }],
    spec: { type: 'table',
      columns: [ {key:'source',label:'source',dtype:'string',align:'left'}, {key:'day',label:'day',dtype:'date',align:'left'}, {key:'volume',label:'volume',dtype:'int',align:'right'} ],
      rows: [ ['pg-quiz','2026-06-01',1204], ['pg-quiz','2026-06-02',987], ['es-search','2026-06-01',3310], ['es-search','2026-06-02',2980] ] },
  },
};

Object.assign(window, { Icon, STATUS, VIZ, DIALECT_ICON, DIALECTS, WORKSPACES, CONNECTIONS, DOC_SOURCES, SESSIONS, SLASH, AGENT_NODES, SIMILAR, ANSWERS });
