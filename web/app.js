/* ============================================================
   外卖平台派单系统 · MVP —— 前端逻辑
   纯原生 JS，无任何依赖。
   ============================================================ */
'use strict';

const $ = (s) => document.querySelector(s);

const STATUS_TEXT = {
  POOLED: '待派单',
  ASSIGNED: '已派单',
  PICKED_UP: '已取餐',
  DELIVERED: '已送达',
};

const MOTION_TEXT = { IDLE: '空闲', MOVING: '赶路中', WAITING: '在商家等出餐' };

/** 每个骑手一个颜色，便于在地图上区分路线。 */
const RIDER_COLORS = [
  '#2563eb', '#db2777', '#0891b2', '#7c3aed',
  '#ea580c', '#0d9488', '#4f46e5', '#b45309',
  '#be123c', '#15803d', '#0369a1', '#9333ea',
];

const S = {
  prev: null, prevAt: 0,
  cur: null, curAt: 0,
  cfgFilled: false,
  roads: null,         // GET /api/roads 的路网几何，只取一次
  networks: null,      // 可用路网清单
};

const ui = {
  tab: 'customer',
  selectedRider: null,
  focusRider: null,    // 只看这个骑手：地图上隐藏其他骑手，并把视野缩到他的路线上
  pick: null,          // 顾客在地图上选的送达点 {x,y}
  showAll: false,
  assignOrder: null,   // 正在做「指派单」的订单号
  reassignOrder: null, // 正在做「调单」的订单号
};

/* ------------------------------------------------------------ 工具函数 */

function clockOf(sec) {
  if (sec == null) return '—';
  const t = Math.max(0, Math.round(sec));
  const p = (n) => String(n).padStart(2, '0');
  return `${p(Math.floor(t / 3600) % 24)}:${p(Math.floor(t / 60) % 60)}:${p(t % 60)}`;
}

function fmtDur(sec) {
  if (sec == null) return '—';
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return s + ' 秒';
  const m = s / 60;
  if (m < 60) return m.toFixed(1) + ' 分';
  return Math.floor(m / 60) + ' 时 ' + Math.round(m % 60) + ' 分';
}

function fmtKm(m) {
  if (m == null) return '—';
  return m >= 1000 ? (m / 1000).toFixed(2) + ' km' : Math.round(m) + ' m';
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function riderColor(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return RIDER_COLORS[h % RIDER_COLORS.length];
}

/** 只在内容真的变了才写 DOM —— 避免每次轮询都重建、把焦点和下拉框打断。 */
function setHTML(node, html, sig) {
  if (!node) return;
  if (node._sig === sig) return;
  node._sig = sig;
  node.innerHTML = html;
}

function toast(msg, kind) {
  const box = $('#toast');
  const d = document.createElement('div');
  d.className = kind || '';
  d.textContent = msg;
  box.appendChild(d);
  setTimeout(() => {
    d.style.transition = 'opacity .25s';
    d.style.opacity = '0';
    setTimeout(() => d.remove(), 260);
  }, 3000);
}

/* ------------------------------------------------------------ 与后端通信 */

/* 认证 token。
   服务端默认只绑 127.0.0.1，并在提供首页时把 token 注入成一个 <meta>，
   所以本机用浏览器访问时这一段是透明的、什么都不用做。
   绑到局域网地址时服务端**不会**注入（否则等于把 token 发给全网段），
   这时用启动日志里打印的 http://…/#token=xxx 链接进入，JS 从 URL 片段里取。
   放在片段里而不是查询串：片段不会进 Referer、也不会进服务端日志。 */
function readToken() {
  const meta = document.querySelector('meta[name="waimai-token"]');
  if (meta && meta.content) return meta.content.trim();
  const m = /(?:^|[#&])token=([A-Za-z0-9_\-]+)/.exec(location.hash || '');
  if (m) {
    try { sessionStorage.setItem('waimai-token', m[1]); } catch (e) { /* 隐私模式 */ }
    return m[1];
  }
  try {
    const saved = sessionStorage.getItem('waimai-token');
    if (saved) return saved;
  } catch (e) { /* 隐私模式 */ }
  return '';
}

const TOKEN = readToken();

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (TOKEN) h['X-Auth-Token'] = TOKEN;
  return h;
}

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

async function poll() {
  const conn = $('#connState');
  try {
    const res = await fetch('/api/state', { cache: 'no-store', headers: authHeaders() });
    if (res.status === 401) {
      // 有服务、但没带对 token：最常见的原因是**直接用浏览器打开了磁盘上的
      // index.html**。页面里的 token 是服务端发页面时注入的，磁盘上那份没有，
      // 所以每个 /api/* 请求都会被拒。这里要说清怎么办，而不是只说"连接断开"。
      conn.textContent = '认证失败';
      conn.className = 'conn bad';
      showHint('打不开接口：缺认证 token。'
               + '请从服务地址打开（例如 http://127.0.0.1:8788/），'
               + '不要直接双击本地的 web/index.html。');
      return;
    }
    const data = await res.json();
    S.prev = S.cur;
    S.prevAt = S.curAt;
    S.cur = data;
    S.curAt = performance.now();
    conn.textContent = '已连接';
    conn.className = 'conn ok';
    hideHint();
    render();
  } catch (e) {
    // 分两种情况说：从本地文件打开时，请求会发到 file:// 上（根本到不了服务端），
    // 这时"连接断开"是误导 —— 用户需要知道的是"换个地址打开"。
    if (location.protocol === 'file:') {
      conn.textContent = '未连接服务';
      conn.className = 'conn bad';
      showHint('当前是本地文件（file://），接口请求发不出去。'
               + '请改用服务地址打开：http://127.0.0.1:8788/'
               + '（端口看你启动时打印的"访问地址"）。');
    } else {
      conn.textContent = '连接断开';
      conn.className = 'conn bad';
      showHint('连不上服务端。确认服务还在跑（服务端控制台有没有报错），'
               + '以及地址里的端口对不对。');
    }
  }
}

/** 顶部一条常驻提示。用于把"为什么用不了"直接写在页面上。
 *
 * 位置选择：插在 `header` 之后、页签之前。之前这里写的是 `header.banner`，
 * 但这个页面根本没有那个类名 —— 于是 fallback 到 append 到 body 之后，
 * 提示渲染在**页面最底部**（实测 y≈730，视口外），等于白写。
 * 教训：提示条的位置要和"用户第一眼看哪里"一致，且选择器必须真的能命中。
 */
function showHint(text) {
  let el = $('#connHint');
  if (!el) {
    el = document.createElement('div');
    el.id = 'connHint';
    el.className = 'connhint';
    const header = document.querySelector('header');
    const nav = document.querySelector('nav.tabs');
    if (header && header.parentNode) {
      header.insertAdjacentElement('afterend', el);
    } else if (nav && nav.parentNode) {
      nav.insertAdjacentElement('beforebegin', el);
    } else {
      document.body.insertBefore(el, document.body.firstChild);
    }
  }
  if (el.textContent !== text) el.textContent = text;
  el.style.display = '';
}

function hideHint() {
  const el = $('#connHint');
  if (el) el.style.display = 'none';
}

/** 路网几何只取一次；切网络或重置之后再取。 */
async function loadRoads() {
  try {
    const res = await fetch('/api/roads', { cache: 'no-store', headers: authHeaders() });
    S.roads = await res.json();
    roadLayer.sig = null;          // 强制重绘离屏路网层
  } catch (e) {
    S.roads = null;
  }
}

async function loadNetworks() {
  try {
    const res = await fetch('/api/networks', { cache: 'no-store', headers: authHeaders() });
    S.networks = await res.json();
  } catch (e) {
    S.networks = null;
  }
}

/* ------------------------------------------------------------ 主渲染 */

function render() {
  const st = S.cur;
  if (!st) return;

  // 顶栏
  $('#simClock').textContent = st.sim.clock;
  const nd = st.sim.nextDispatchIn;
  $('#nextDispatch').textContent = st.sim.paused
    ? '已暂停'
    : `下次派单 ${String(Math.floor(nd / 60)).padStart(2, '0')}:${String(nd % 60).padStart(2, '0')}`;
  $('#roundInfo').textContent = `第 ${st.sim.dispatchRounds} 轮派单`;
  $('#pauseBtn').textContent = st.sim.paused ? '继续' : '暂停';
  $('#speedSel').value = String(st.sim.speed);

  renderKPI(st);
  renderConfig(st);
  renderIntake(st);
  renderFocusChip(st);
  renderNetwork(st);
  renderPool(st);
  renderRiders(st);
  renderActive(st);
  renderLog(st);
  renderOrders(st);
  renderRiderView(st);

  if (!$('#pickMap')._init) initPickMap(st);

  drawMap(st);
  drawPickMap();
}

/* ------------------------------------------------------------ 只看一个骑手 */

/**
 * 地图卡片标题栏里的聚焦提示条：显示当前只看谁、可以直接切换、可以一键退出。
 * 做成一个可切换的下拉框而不只是标签 —— 否则聚焦后地图上只剩一个骑手，
 * 就没办法再点别的骑手切过去了。
 */
function renderFocusChip(st) {
  const el = $('#mapFocus');
  if (!el) return;
  if (!ui.focusRider) { setHTML(el, '', 'none'); return; }

  const r = st.riders.find((x) => x.id === ui.focusRider);
  if (!r) { ui.focusRider = null; setHTML(el, '', 'none'); return; }

  const sig = 'F' + r.id + '|' + r.status + '|' + r.activeOrders + '|' + r.route.length;
  setHTML(el, `
    <span class="fclabel">只看
      <select data-act="focus-pick">
        ${st.riders.map((x) =>
          `<option value="${x.id}" ${x.id === r.id ? 'selected' : ''}>${x.id} ${esc(x.name)}</option>`).join('')}
      </select>
      <button class="fcx" data-act="focus-clear" title="显示全部骑手（Esc）">&times;</button>
    </span>
    <span class="fchint">${r.activeOrders} 单在身 · ${r.route.length} 个站点 · 已隐藏其他骑手</span>`, sig);
}

/* ------------------------------------------------------------ 路网信息与切换 */

function renderNetwork(st) {
  const net = st.network || {};
  const isRoad = net.mode && net.mode !== 'ABSTRACT';

  // 路网变了就重新取一次几何。放在这里而不是只放在切换按钮里 ——
  // 否则从别的途径切换（curl、另一个页面、后端脚本）时前端不会跟着更新。
  const netSig = (net.mode || '') + '|' + (net.name || '');
  if (sb.lastNetSig !== undefined && sb.lastNetSig !== netSig) {
    loadRoads();
  }
  sb.lastNetSig = netSig;

  // 地图右下角的说明：说清楚当前用的是哪套距离模型
  const note = $('#mapNote');
  if (note) {
    note.textContent = isRoad
        ? `距离按真实路网的最短路计算${S.roads && S.roads.truncated
            ? `（路段过多，只画了最长的 ${S.roads.count} / ${S.roads.total} 条）` : ''}`
        : '地图为 10km × 10km 的模拟城市，距离按 1.4 × 直线距离 折算成街面里程';
  }

  const hint = $('#netHint');
  if (hint) hint.textContent = net.label || '';

  // 下拉框只填一次（或清单变了）
  const sel = $('#netSel');
  if (sel && S.networks && S.networks.list) {
    const sig = JSON.stringify(S.networks.list.map((n) => n.id + (n.current ? '*' : '')));
    if (sel._sig !== sig) {
      sel._sig = sig;
      sel.innerHTML = S.networks.list.map((n) =>
        `<option value="${esc(n.id)}" ${n.current ? 'selected' : ''}>${esc(n.label)}</option>`).join('');
    }
  }

  const info = $('#netInfo');
  if (info) {
    const rows = [];
    if (isRoad) {
      rows.push(`<span class="tag road">${esc(net.label || '路网')}</span>`);
      rows.push(`节点 <b>${(net.nodes || 0).toLocaleString()}</b> · 有向边 <b>${(net.edges || 0).toLocaleString()}</b>`);
      if (S.roads) {
        rows.push(`绘制路段 <b>${(S.roads.count || 0).toLocaleString()}</b> 条`);
      }
      rows.push('单行道会带来绕路，所以距离不再等于直线 × 1.4');
      rows.push('路网模式下<b>禁用 2-opt</b>（反转区间在单行道上不合法），只用 Or-opt');
    } else {
      rows.push('<span class="tag">抽象城市</span>');
      rows.push('距离 = <b>1.4 ×</b> 直线距离，2-opt 与 Or-opt 都启用');
    }
    if (S.networks && S.networks.dataDir) {
      rows.push(`把自己的 .osm 放进 <b>${esc(S.networks.dataDir)}</b> 后刷新即可选用`);
    }
    const html = rows.join('<br>');
    if (info._sig !== html) {
      info._sig = html;
      info.innerHTML = html;
    }
  }
}

/* ------------------------------------------------------------ 停单 / 进单 */

function renderIntake(st) {
  const it = st.intake;
  if (!it) return;

  const pill = $('#intakePill');
  if (pill) {
    pill.textContent = it.open ? '进单中' : (it.manualOff ? '已手动停单' : '已自动停单');
    pill.className = 'pill ' + (it.open ? 'ONLINE' : 'BUSY');
  }

  // 顶栏常驻开关：任何页签都能一键切换，不用滚到下面找
  const tog = $('#intakeToggle');
  if (tog) {
    tog.classList.toggle('stopped', !it.open);
    tog.title = it.open ? '当前允许进单 —— 点击停止进单' : `${it.reason} —— 点击恢复进单`;
  }
  const togText = $('#intakeToggleText');
  if (togText) {
    togText.textContent = it.open ? '允许进单'
      : (it.manualOff ? '已停单（手动）' : '已停单（自动）');
  }

  // 卡片里的一键按钮：文字跟着当前状态走，永远说清「点下去会发生什么」
  const cardBtn = $('#intakeCardToggle');
  if (cardBtn) {
    cardBtn.textContent = it.open ? '一键停止进单' : '一键恢复进单';
    cardBtn.className = 'btn small ' + (it.open ? 'ghost' : 'primary');
  }

  // 顾客端顶部横幅：停单时要说清楚为什么，不能只是下不了单
  const banner = $('#intakeBanner');
  if (banner) {
    const sig = it.open ? 'open' : (it.manualOff ? 'manual' : 'auto') + '|' + it.reason;
    setHTML(banner, it.open ? '' :
      `<div class="ib"><b>本站点已停止接单</b><span>${esc(it.reason)}</span></div>`, sig);
  }

  const el = $('#intakeStatus');
  if (el) {
    const pct = Math.min(100, Math.round(it.poolRatio * 100));
    const thr = Math.round(it.thresholdRatio * 100);
    const cls = !it.open ? 'stop' : (pct >= thr * 0.7 ? 'warn' : 'ok');
    const rows = [
      `<div class="row"><span>待派单 <b>${it.pooled}</b> / 在系统 <b>${it.current}</b>`
      + `（在途 <b>${it.inFlight}</b>）</span></div>`,
      `<div class="ratio-bar"><i class="${cls}" style="width:${pct}%"></i></div>`,
      `<div class="row"><span>待派单占比 <b>${pct}%</b> · 停单阈值 <b>${thr}%</b>`
      + ` · 低于 ${it.minOrders} 单不触发</span></div>`,
      `<div class="row"><span>累计订单 <b>${it.totalOrders}</b> / 上限 ${it.maxTotalOrders}</span></div>`,
    ];
    if (!it.open) rows.push(`<div class="why">${esc(it.reason)}</div>`);
    setHTML(el, rows.join(''), JSON.stringify([it.open, it.pooled, it.current, it.poolRatio,
      it.thresholdRatio, it.minOrders, it.totalOrders, it.reason]));
  }

  // 停单时禁用下单按钮 —— 按钮能点但一定失败，体验很差
  const disable = !it.open;
  ['#submitOrder', '#randomOrder'].forEach((s) => {
    const b = $(s);
    if (b) { b.disabled = disable; b.title = disable ? it.reason : ''; }
  });
}

/** 一键切换进单状态。自动停单时顺带把阈值放宽到当前占比之上，否则点了也不生效。 */
async function toggleIntake() {
  const it = S.cur && S.cur.intake;
  if (!it) return;

  if (it.open) {
    await post('/api/control', { acceptOrders: false });
    toast('已停止进单（管理平台手动）', 'err');
  } else if (it.manualOff) {
    await post('/api/control', { acceptOrders: true });
    toast('已恢复进单', 'ok');
  } else {
    // 自动停单：光打开总开关没用，占比还在阈值之上，下一拍又会停。
    // 所以顺手把阈值提到当前占比之上，并把改了什么明说出来。
    const raised = Math.min(1.0, Math.round((it.poolRatio + 0.05) * 100) / 100);
    await post('/api/control', { acceptOrders: true, stopAcceptPoolRatio: raised });
    toast(`已放宽停单阈值到 ${Math.round(raised * 100)}%，恢复进单`, 'ok');
  }
  poll();
}

/* ------------------------------------------------------------ KPI */

function renderKPI(st) {
  const s = st.stats;

  // 推迟开关跟着状态走：重置会把它恢复成默认值，别的地方也可能改它，
  // 只靠点击时同步会留下一个和后端不一致的勾选框。
  if (st.cfg) {
    const on = st.cfg.postponePoorAssignments !== false;
    const k = $('#kpiPostpone');
    if (k && k.checked !== on) k.checked = on;
    const c2 = $('#cfgPostpone');
    if (c2 && c2.checked !== on) c2.checked = on;
  }

  const kpi = (k, v, u, cls, hint, title) =>
    `<div class="kpi ${cls || ''}"${title ? ` title="${esc(title)}"` : ''}>
     <div class="k">${k}</div>
     <div class="v">${v}${u ? `<span class="u">${u}</span>` : ''}</div>
     ${hint ? `<div class="kpi-hint">${esc(hint)}</div>` : ''}</div>`;

  // 「顺路占比」单独看会误导人：运力空闲时，让空车骑手去接单本来就更省钱，
  // 顺路占比低是正确结果；运力吃紧时顺路占比低才是缺运力的信号。
  // 所以这个数必须连负载一起读，判定规则写在下面。
  const share = s.onRouteShare;
  const load = s.riderUtilization;
  const fallback = (s.tier1OnRoute + s.tier2Idle + s.tier3Fallback) > 0
    ? Math.round(s.tier3Fallback * 100 / (s.tier1OnRoute + s.tier2Idle + s.tier3Fallback))
    : null;
  let shareCls = '';
  let shareHint = '还没有派出过订单';
  let shareTitle = '顺路占比 = 顺路派单 ÷（顺路 + 无单 + 兜底）。'
    + '兜底派单就是骑手得为此多绕路的那种。';
  if (share != null && load != null) {
    const tail = fallback == null ? '' : `，兜底 ${fallback}%`;
    if (load < 40) {
      shareHint = `运力空闲（负载 ${load}%）${tail}`;
      shareTitle += '\n当前负载低，空车骑手接单是最优解 —— 顺路占比低是正常的，不要据此加人。';
    } else if (load >= 80) {
      shareCls = 'warn';
      shareHint = `运力吃紧（负载 ${load}%）${tail}`;
      shareTitle += '\n当前负载高，顺路占比低说明运力不足 —— 该加骑手，或者把进单压一压。';
    } else {
      shareCls = 'good';
      shareHint = `负载 ${load}% 正常${tail}`;
      shareTitle += '\n当前负载适中。';
    }
  }

  const html = [
    kpi('订单池待派', s.pooled, '单', s.pooled > 6 ? 'warn' : ''),
    kpi('配送中', s.inFlight, '单'),
    kpi('已送达', s.delivered, '单', 'good'),
    kpi('准时率', s.onTimeRate == null ? '—' : s.onTimeRate + '%', '', s.onTimeRate != null && s.onTimeRate >= 90 ? 'good' : 'warn'),
    kpi('平均总时长', s.avgTotalMin == null ? '—' : s.avgTotalMin, s.avgTotalMin == null ? '' : '分'),
    kpi('顺路派单占比', share == null ? '—' : share + '%', '', shareCls, shareHint, shareTitle),
    kpi('平均等派单', s.avgWaitDispatchMin == null ? '—' : s.avgWaitDispatchMin, '分'),
    kpi('平均配送', s.avgOnRoadMin == null ? '—' : s.avgOnRoadMin, '分'),
    kpi('骑手里程', s.riderTotalKm, 'km'),
    kpi('每单里程', s.avgKmPerOrder == null ? '—' : s.avgKmPerOrder, 'km'),
    kpi('骑手负载', s.riderLoad + ' / ' + s.riderCapacity, '单', s.riderUtilization >= 80 ? 'warn' : ''),
    kpi('派单轮次', s.dispatchRounds, '轮'),
  ].join('');

  const sig = JSON.stringify([s.pooled, s.inFlight, s.delivered, s.onTimeRate, s.avgTotalMin,
    s.onRouteShare, s.tier3Fallback, s.avgWaitDispatchMin, s.avgOnRoadMin, s.riderTotalKm,
    s.avgKmPerOrder, s.riderLoad, s.riderCapacity, s.riderUtilization, s.dispatchRounds]);
  setHTML($('#kpi'), html, sig);
}

/* ------------------------------------------------------------ 参数 */

function renderConfig(st) {
  if (S.cfgFilled) return;
  S.cfgFilled = true;
  const c = st.cfg;
  $('#cfgInterval').value = c.dispatchIntervalSec;
  $('#cfgDetourM').value = c.onRouteMaxDetourM;
  $('#cfgDetourRatio').value = c.onRouteMaxDetourRatio;
  $('#cfgLoad').value = c.loadPenaltyPerOrderM;
  $('#cfgSla').value = c.slaMinutes;
  $('#cfgAutoEvery').value = c.autoOrderEverySec;
  $('#cfgAutoOrder').checked = !!c.autoOrder;
  $('#cfgAvoidWait').checked = !!c.avoidWaiting;
  $('#cfgAcceptOrders').checked = c.acceptOrders !== false;
  $('#cfgStopRatio').value = Math.round((c.stopAcceptPoolRatio ?? 0.5) * 100);
  $('#cfgStopMin').value = c.stopAcceptMinOrders ?? 8;
  $('#cfgMaxTotal').value = c.maxTotalOrders ?? 1500;
  $('#cfgPostpone').checked = c.postponePoorAssignments !== false;
  $('#kpiPostpone').checked = c.postponePoorAssignments !== false;
  $('#cfgPostponeRounds').value = c.postponeMaxRounds ?? 3;
  $('#cfgPostponeWait').value = c.postponeMaxWaitMin ?? 6;
  $('#cfgPostponeFactor').value = c.postponeMaxPoolFactor ?? 1;
  $('#postponeHint').innerHTML =
    '只看「最好的归宿也只是兜底」的单：先留着不派，下一轮如果冒出顺路的骑手再派出去。'
    + '<br>闸门的分母是<b>还有余量的骑手</b>（不是骑手总数）——队列一旦长过能立刻接单的骑手，'
    + '等待就不再免费，系统会立刻放弃等待把单派出去。'
    + '<br>实测（10 骑手、1 单/分钟、90 分钟）：顺路 <b>29% → 42%</b>，兜底 <b>34% → 12%</b>，'
    + '准时率与平均时长都没变差。';

  // 商家下拉只填一次
  const sel = $('#cMerchant');
  sel.innerHTML = st.merchants.map(m =>
    `<option value="${m.id}">${esc(m.name)}（出餐约 ${Math.round(m.prepSec / 60)} 分钟）</option>`).join('');
}

/* ------------------------------------------------------------ 订单池 */

function riderOptionsHTML(st, excludeId) {
  const rs = st.riders.filter(r => r.status === 'ONLINE' && r.activeOrders < r.maxOrders && r.id !== excludeId);
  if (!rs.length) return '<option value="">（暂无可用骑手）</option>';
  return rs.map(r =>
    `<option value="${r.id}">${r.id} ${esc(r.name)}（${r.activeOrders}/${r.maxOrders} 单）</option>`).join('');
}

function renderPool(st) {
  const items = st.orders.filter(o => o.status === 'POOLED');
  // 池子非空不等于积压：开了推迟之后，池子里会有几单是故意在等更顺路的骑手。
  // 不把这件事说出来，「池子里有 5 单」会被读成「处理不过来了」。
  const waiting = (st.stats && st.stats.pooledPostponed) || 0;
  $('#poolHint').textContent = items.length
    ? `${items.length} 单等待派单${waiting ? `（其中 ${waiting} 单在等更顺路的骑手）` : ''}`
    : '暂无待派订单';

  const sig = JSON.stringify(items.map(o => [o.id, o.merchantName, o.t.created, Math.round((st.sim.seconds - o.t.created) / 60)]));
  const html = items.length ? items.map(o => `
    <div class="pitem">
      <div class="info">
        <div class="t1">${o.id} · ${esc(o.customerName)}</div>
        <div class="t2">${esc(o.merchantName)} → ${esc(o.address)}（等了 ${Math.max(0, Math.round((st.sim.seconds - o.t.created) / 60))} 分）</div>
      </div>
      <div class="push">
        <button class="btn small" data-act="assign" data-id="${o.id}">指派单</button>
      </div>
    </div>`).join('') : '<div class="empty">订单池是空的</div>';
  setHTML($('#poolList'), html, sig);

  // 指派单操作条（独立节点，不会因为列表刷新而丢失选择）
  const bar = $('#poolAction') || (() => {
    const d = document.createElement('div');
    d.id = 'poolAction'; d.className = 'actionbar';
    $('#poolList').parentNode.insertBefore(d, $('#poolList'));
    return d;
  })();

  if (ui.assignOrder) {
    const o = st.orders.find(x => x.id === ui.assignOrder);
    if (!o) { ui.assignOrder = null; }
    else {
      const sig2 = 'A' + o.id + '|' + st.riders.map(r => r.id + r.status + r.activeOrders).join();
      setHTML(bar, `
        <div class="actionbar-inner">
          <span>把 <b>${o.id}</b> 指派给</span>
          <select id="assignRider">${riderOptionsHTML(st)}</select>
          <button class="btn small primary" data-act="assign-go" data-id="${o.id}">确认指派</button>
          <button class="btn small ghost" data-act="cancel">取消</button>
        </div>`, sig2);
      return;
    }
  }
  setHTML(bar, '', 'none');
}

/* ------------------------------------------------------------ 骑手 */

function renderRiders(st) {
  const sig = JSON.stringify(st.riders.map(r =>
    [r.id, r.status, r.maxOrders, r.activeOrders, r.motion, r.delivered, r.km]));
  const html = st.riders.map(r => {
    const pct = r.maxOrders ? Math.round(r.activeOrders * 100 / r.maxOrders) : 0;
    const full = r.activeOrders >= r.maxOrders;
    return `
    <div class="ritem ${ui.selectedRider === r.id ? 'sel' : ''}">
      <div class="ritem-hd">
        <span class="nm" style="color:${riderColor(r.id)}">${r.id}</span>
        <span>${esc(r.name)}</span>
        <span class="push pill ${r.status}">${r.status === 'ONLINE' ? '上线' : '忙碌'}</span>
      </div>
      <div class="ritem-sub">
        <span>手持 ${r.activeOrders}/${r.maxOrders} 单</span>
        <span>${MOTION_TEXT[r.motion] || r.motion}</span>
        <span>已送 ${r.delivered} 单</span>
        <span>${r.km} km</span>
      </div>
      <div class="loadbar"><i class="${full ? 'full' : ''}" style="width:${pct}%"></i></div>
      <div class="ritem-ctl">
        <select data-act="rider-status" data-id="${r.id}">
          <option value="ONLINE" ${r.status === 'ONLINE' ? 'selected' : ''}>上线</option>
          <option value="BUSY" ${r.status === 'BUSY' ? 'selected' : ''}>忙碌</option>
        </select>
        <span style="color:var(--muted-2)">接单上限</span>
        <select data-act="rider-cap" data-id="${r.id}">
          ${[1, 2, 3, 4, 5, 6, 8, 10].map(n =>
            `<option value="${n}" ${r.maxOrders === n ? 'selected' : ''}>${n} 单</option>`).join('')}
        </select>
        <button class="btn small ${ui.focusRider === r.id ? 'primary' : 'ghost'}"
          data-act="focus-set" data-id="${r.id}"
          title="地图上只显示这个骑手的路线">只看他</button>
        <button class="btn small danger" data-act="remove-rider" data-id="${r.id}"
          title="把这个骑手从系统里移除">移除</button>
      </div>
    </div>`;
  }).join('');
  setHTML($('#riderList'), html, sig);

  const hint = $('#riderHint');
  if (hint) {
    const online = st.riders.filter((r) => r.status === 'ONLINE').length;
    hint.textContent = `共 ${st.riders.length} 人（在线 ${online}）`;
  }
}

/* ------------------------------------------------------------ 进行中的订单 */

function renderActive(st) {
  const items = st.orders.filter(o => o.status === 'ASSIGNED' || o.status === 'PICKED_UP')
    .sort((a, b) => a.t.dispatched - b.t.dispatched);

  const sig = JSON.stringify(items.map(o => [o.id, o.status, o.riderId, o.mode, o.tier, o.reassignCount, o.t.picked]));
  const html = items.length ? items.map(o => {
    const canReassign = o.t.picked == null;
    const tierTag = o.mode === 'MANUAL_ASSIGN' ? '<span class="pill t3">指派单</span>'
      : o.mode === 'REASSIGN' ? '<span class="pill t3">调单</span>'
      : o.tier ? `<span class="pill t${o.tier}">${o.tier === 1 ? '顺路' : o.tier === 2 ? '无单' : '兜底'}</span>` : '';
    return `
    <div class="aitem">
      <div>
        <div class="t1" style="font-weight:600">${o.id} ${tierTag}</div>
        <div style="font-size:11px;color:var(--muted)">
          ${esc(o.riderId || '')} ${esc(o.riderName || '')} · ${STATUS_TEXT[o.status]}
          ${o.detourM ? '· 多绕 ' + Math.round(o.detourM) + 'm' : ''}
        </div>
      </div>
      <div class="push">
        <button class="btn small" data-act="reassign" data-id="${o.id}"
          ${canReassign ? '' : 'disabled title="已取餐，不能再调单"'}>调单</button>
      </div>
    </div>`;
  }).join('') : '<div class="empty">暂无进行中的订单</div>';
  setHTML($('#activeOrders'), html, sig);

  const bar = $('#activeAction') || (() => {
    const d = document.createElement('div');
    d.id = 'activeAction'; d.className = 'actionbar';
    $('#activeOrders').parentNode.insertBefore(d, $('#activeOrders'));
    return d;
  })();

  if (ui.reassignOrder) {
    const o = st.orders.find(x => x.id === ui.reassignOrder);
    if (!o || o.status === 'DELIVERED' || o.t.picked != null) { ui.reassignOrder = null; }
    else {
      const sig2 = 'R' + o.id + '|' + o.riderId + '|' + st.riders.map(r => r.id + r.status + r.activeOrders).join();
      setHTML(bar, `
        <div class="actionbar-inner">
          <span>把 <b>${o.id}</b> 从 ${esc(o.riderId)} 转给</span>
          <select id="reassignRider">${riderOptionsHTML(st, o.riderId)}</select>
          <button class="btn small primary" data-act="reassign-go" data-id="${o.id}">确认调单</button>
          <button class="btn small ghost" data-act="cancel">取消</button>
        </div>`, sig2);
      return;
    }
  }
  setHTML(bar, '', 'none');
}

/* ------------------------------------------------------------ 日志 */

function renderLog(st) {
  const sig = st.log.length + '|' + (st.log[0] ? st.log[0].t + st.log[0].text : '');
  const html = st.log.length ? st.log.map(e => `
    <div class="litem">
      <span class="tm">${e.clock}</span>
      <span class="kd ${esc(e.kind)}">${esc(e.kind)}</span>
      <span class="tx">${esc(e.text)}</span>
    </div>`).join('') : '<div class="empty">暂无日志</div>';
  setHTML($('#logList'), html, sig);
}

/* ------------------------------------------------------------ 顾客端订单列表 */

function timelineHTML(o) {
  const steps = [
    [1, '下单', o.t.created],
    [2, '派单', o.t.dispatched],
    [3, '到店', o.t.arrivedStore],
    [4, '取餐', o.t.picked],
    [5, '送达', o.t.delivered],
  ];
  return `<div class="tl">` + steps.map(([n, lbl, t]) => `
    <div class="tlstep ${t ? 'done' : ''}">
      <div class="bar"></div>
      <div class="dot">${n}</div>
      <div class="lbl">${lbl}</div>
      <div class="tm">${t ? clockOf(t) : '—'}</div>
    </div>`).join('') + `</div>`;
}

function orderCardHTML(o) {
  const modeTag = o.mode === 'MANUAL_ASSIGN' ? '<span class="pill t3">指派单</span>'
    : o.mode === 'REASSIGN' ? '<span class="pill t3">调单 ×' + o.reassignCount + '</span>'
    : o.tier ? `<span class="pill t${o.tier}">${o.tier === 1 ? '顺路骑手' : o.tier === 2 ? '无订单骑手' : '兜底骑手'}</span>` : '';

  return `
  <div class="ocard">
    <div class="ocard-top">
      <span class="oid">${o.id}</span>
      <span class="pill ${o.status}">${STATUS_TEXT[o.status]}</span>
      ${modeTag}
      <span class="push meta">${o.riderId ? esc(o.riderId) + ' ' + esc(o.riderName || '') : ''}</span>
    </div>
    <div class="oline"><span class="k">姓名</span>${esc(o.customerName)}　<span class="k">电话</span>${esc(o.phone)}</div>
    <div class="oline"><span class="k">地址</span>${esc(o.address)}</div>
    ${o.note ? `<div class="oline"><span class="k">备注</span><span class="note">${esc(o.note)}</span></div>` : ''}
    <div class="oline"><span class="k">商家</span>${esc(o.merchantName)}</div>
    ${timelineHTML(o)}
    <div class="durs">
      <span>等派单 <b>${fmtDur(o.d.waitDispatch)}</b></span>
      <span>赶路 <b>${fmtDur(o.d.toStore)}</b></span>
      <span>等出餐 <b>${fmtDur(o.d.prepWait)}</b></span>
      <span>配送 <b>${fmtDur(o.d.onRoad)}</b></span>
      <span>顾客总等待 <b>${fmtDur(o.d.total)}</b></span>
    </div>
  </div>`;
}

function renderOrders(st) {
  // 顾客最关心的是「还在路上的」—— 先把进行中的排前面，再排最近送达的
  const active = st.orders.filter(o => o.status !== 'DELIVERED');
  const done = st.orders.filter(o => o.status === 'DELIVERED');
  const list = ui.showAll ? active.concat(done) : active.concat(done).slice(0, 12);

  const sig = (ui.showAll ? 'all|' : 'top|') + list.length + '|' +
    JSON.stringify(list.map(o => [o.id, o.status, o.riderId, o.mode, o.tier,
      o.t.created, o.t.dispatched, o.t.arrivedStore, o.t.picked, o.t.delivered]));
  const html = list.length ? list.map(orderCardHTML).join('')
    : '<div class="empty">还没有订单。在左边下单试试，或等待系统自动生成订单。</div>';
  setHTML($('#myOrders'), html, sig);
}

/* ------------------------------------------------------------ 骑手端 */

function renderRiderView(st) {
  // 骑手选择卡片
  const sig = JSON.stringify(st.riders.map(r => [r.id, r.status, r.activeOrders, r.maxOrders, r.delivered]));
  const html = st.riders.map(r => `
    <div class="rpcard ${ui.selectedRider === r.id ? 'sel' : ''}" data-act="pick-rider" data-id="${r.id}">
      <div class="nm" style="color:${riderColor(r.id)}">${r.id} · ${esc(r.name)}</div>
      <div class="sub">${r.status === 'ONLINE' ? '上线' : '忙碌'} · 手持 ${r.activeOrders}/${r.maxOrders} 单 · 已送 ${r.delivered} 单</div>
    </div>`).join('');
  setHTML($('#riderPicker'), html, sig);

  // 详情
  const r = st.riders.find(x => x.id === ui.selectedRider);
  if (!r) {
    $('#riderHead').textContent = '';
    setHTML($('#riderDetail'), '<div class="empty">先在左边选一个骑手</div>', 'none');
    return;
  }
  $('#riderHead').textContent = `${r.id} ${r.name}`;

  const dsig = JSON.stringify([r.id, r.status, r.motion, r.activeOrders, r.maxOrders, r.km, r.route]);
  const stops = r.route.length ? r.route.map((s, i) => `
    <div class="stop ${s.type === 'PICKUP' ? 'pickup' : 'delivery'} ${i === 0 ? 'current' : ''}">
      <div class="idx">${i + 1}</div>
      <div class="body">
        <div class="hd">
          <span class="kind">${s.type === 'PICKUP' ? '到店取餐' : '送达顾客'}</span>
          <span class="oid">${s.orderId}</span>
          ${i === 0 ? '<span class="pill t1">下一站</span>' : ''}
          ${s.picked ? '<span class="pill DELIVERED">餐已在手</span>' : ''}
        </div>
        <div class="cust">
          <span class="k">${s.type === 'PICKUP' ? '商家' : '顾客'}</span>
          ${esc(s.type === 'PICKUP' ? s.merchantName : s.customerName)}
          <span class="k" style="margin-left:10px">电话</span>${esc(s.phone)}
        </div>
        <div class="cust"><span class="k">地址</span>${esc(s.address)}</div>
        ${s.note ? `<div class="note">备注：${esc(s.note)}</div>` : ''}
      </div>
    </div>`).join('') : '<div class="empty">手上没有待办任务</div>';

  const head = `
    <div class="rhead">
      <span class="nm" style="color:${riderColor(r.id)}">${r.id}</span>
      <span>${esc(r.name)}</span>
      <span class="pill ${r.status}">${r.status === 'ONLINE' ? '上线' : '忙碌'}</span>
      <span style="font-size:12px;color:var(--muted)">${MOTION_TEXT[r.motion] || r.motion}
        · 手持 ${r.activeOrders}/${r.maxOrders} 单 · 已送 ${r.delivered} 单 · ${r.km} km</span>
      <span class="push">
        <button class="btn small ${ui.focusRider === r.id ? 'primary' : ''}"
          data-act="focus-set" data-id="${r.id}"
          title="切到派单台，只显示这个骑手的路线">在地图上只看他</button>
        <select data-act="rider-status" data-id="${r.id}">
          <option value="ONLINE" ${r.status === 'ONLINE' ? 'selected' : ''}>上线（正常接单）</option>
          <option value="BUSY" ${r.status === 'BUSY' ? 'selected' : ''}>忙碌（不接单）</option>
        </select>
        <select data-act="rider-cap" data-id="${r.id}">
          ${[1, 2, 3, 4, 5, 6, 8, 10].map(n =>
            `<option value="${n}" ${r.maxOrders === n ? 'selected' : ''}>上限 ${n} 单</option>`).join('')}
        </select>
      </span>
    </div>`;

  setHTML($('#riderDetail'), head + `<div class="routes">${stops}</div>`, dsig);
}

/* ------------------------------------------------------------ 路网底图 */

/**
 * 路网离屏缓存。
 *
 * <p>几万条线段每帧都重画会卡，所以先把路网画到一张离屏画布上，之后每帧只做一次
 * `drawImage` 缩放贴图。离屏分辨率按**当前屏幕比例**推出来（放大看某个骑手时自动
 * 用更高分辨率重画一次），这样既有细节又不会一上来就开一张几万像素的大图。
 */
const roadLayer = { canvas: null, sig: null };

const ROAD_MAX_SIDE = 4096;

function ensureRoadLayer(T) {
  const r = S.roads;
  if (!r || !r.count || !r.seg) return null;

  // 离屏画布上「每米几个像素」：跟当前屏幕比例走，再乘 1.5 倍留点余量
  const wantPpm = Math.max(T.sx, T.sy) * 1.5;
  const ppm = Math.min(wantPpm, ROAD_MAX_SIDE / Math.max(r.w, r.h));
  const cw = Math.max(64, Math.round(r.w * ppm));
  const ch = Math.max(64, Math.round(r.h * ppm));
  const sig = `${cw}x${ch}|${r.count}|${r.name}|${r.w}x${r.h}`;
  if (roadLayer.sig === sig && roadLayer.canvas) return roadLayer.canvas;

  const cv = roadLayer.canvas && roadLayer.canvas.width === cw
      && roadLayer.canvas.height === ch ? roadLayer.canvas : document.createElement('canvas');
  cv.width = cw;
  cv.height = ch;
  const ctx = cv.getContext('2d');
  const k = ppm / 10;                     // 分米 → 像素
  const seg = r.seg;

  ctx.clearRect(0, 0, cw, ch);
  ctx.strokeStyle = '#d7dde5';
  // 离屏画布之后会被 drawImage 缩放贴到屏上，缩放系数是 T.sx/ppm。
  // 想让屏幕上看到约 1.3px 的线，离屏这边就得反着放大回去 ——
  // 否则线会细到不足一个像素，路网看起来像没画出来。
  const blit = (T.sx || 1) / ppm;
  ctx.lineWidth = Math.max(1, 1.3 / Math.max(1e-6, blit));
  ctx.lineCap = 'round';
  ctx.beginPath();
  for (let i = 0; i < seg.length; i += 4) {
    ctx.moveTo(seg[i] * k, seg[i + 1] * k);
    ctx.lineTo(seg[i + 2] * k, seg[i + 3] * k);
  }
  ctx.stroke();

  roadLayer.canvas = cv;
  roadLayer.sig = sig;
  return cv;
}

/** 有没有可画的路网底图。 */
function hasRoads() {
  return !!(S.roads && S.roads.count);
}

/** 把一条折线（平铺的分米整数数组）画到画布上。 */
function strokePolyline(ctx, flat, X, Y) {
  if (!flat || flat.length < 4) return false;
  ctx.beginPath();
  ctx.moveTo(X(flat[0] / 10), Y(flat[1] / 10));
  for (let i = 2; i < flat.length; i += 2) {
    ctx.lineTo(X(flat[i] / 10), Y(flat[i + 1] / 10));
  }
  return true;
}

/* ------------------------------------------------------------ 运营分析侧边栏 */

const sb = {
  open: false,
  zones: null,        // GET /api/zones 的结果
  zonesAt: 0,
  heat: { on: false, mode: 'orders' },
  llm: { status: null, config: null, lastSim: -1 },
  autoTimer: null,
};

function setSidebar(open) {
  sb.open = open;
  $('#sidebar').classList.toggle('open', open);
  // 宽屏下不压暗主界面 —— 用户要一边看地图热力、一边读分析。
  // 窄屏时侧边栏会盖住整屏，那时才需要一层遮罩来提示「点一下关闭」。
  const narrow = window.innerWidth <= 900;
  $('#sbBackdrop').classList.toggle('show', open && narrow);
  if (open) refreshZones();
}

async function refreshZones() {
  const win = $('#heatWindow').value;
  const cells = $('#heatCells').value;
  try {
    const r = await fetch(`/api/zones?window=${win}&cells=${cells}`, { cache: 'no-store', headers: authHeaders() });
    sb.zones = await r.json();
    sb.zonesAt = performance.now();
    renderAdvice();
    renderZoneList();
    renderHeatLegend();
  } catch (e) {
    /* 侧边栏是辅助功能，取不到静默跳过，不要打扰主流程 */
  }
}

async function refreshLlm() {
  try {
    const r = await fetch('/api/llm/status', { cache: 'no-store', headers: authHeaders() });
    const j = await r.json();
    sb.llm.status = j.status;
    sb.llm.config = j.config;
    renderLlm();
    renderLlmConfig();
  } catch (e) { /* ignore */ }
}

/* ---- 运力建议 ---- */

function renderAdvice() {
  const el = $('#advice');
  const z = sb.zones;
  if (!z || !z.ok) { setHTML(el, '<div class="empty">正在统计…</div>', 'none'); return; }

  const tp = z.throughputPerRiderHour;
  const rows = [];
  rows.push(`<div class="big">
      <div class="add"><div class="n">+${z.suggestAdd}</div><div class="l">建议增派（人）</div></div>
      <div class="rest"><div class="n">−${z.suggestRest}</div><div class="l">可安排轮休（人）</div></div>
    </div>`);
  rows.push(`<div class="meta">
      窗口内需求 <b>${z.totalDemandPerHour}</b> 单/时 ·
      单骑手产能 <b>${tp == null ? '—' : tp}</b> 单/时 ·
      全网需要 <b>${z.globalNeedRiders}</b> 人 ·
      在线 <b>${z.ridersOnline}</b> 人
    </div>`);
  if (tp != null) {
    rows.push(`<div class="meta" style="margin-top:2px">产能估法：${esc(z.throughputMethodLabel || '—')}</div>`);
  }

  if (!z.dataSufficient) {
    rows.push(`<div class="warn">样本还太少（窗口内送达不足 3 单），产能估算不可靠 —— 建议先让模拟跑一会儿，或把统计窗口调大。</div>`);
  } else if (z.suggestAdd > 0) {
    rows.push(`<div class="warn">当前运力偏紧：按观测产能算还差约 ${z.suggestAdd} 人，才会基本不排队。</div>`);
  } else if (z.suggestRest > 0) {
    rows.push(`<div class="warn">当前运力有余：需求只需要 ${z.globalNeedRiders} 人，可先安排 ${z.suggestRest} 人轮休压成本。</div>`);
  } else {
    rows.push(`<div class="warn" style="background:var(--green-soft);color:#047857">运力与需求基本平衡，既不用加人，也不用刻意撤人。</div>`);
  }

  const sig = JSON.stringify([z.suggestAdd, z.suggestRest, z.dataSufficient,
    z.totalDemandPerHour, tp, z.globalNeedRiders, z.ridersOnline]);
  setHTML(el, rows.join(''), sig);
}

/* ---- 区域研判列表 ---- */

function renderZoneList() {
  const el = $('#zoneList');
  const z = sb.zones;
  if (!z || !z.ok) { setHTML(el, '<div class="empty">正在统计…</div>', 'none'); return; }

  const interesting = (z.zones || []).filter((c) =>
    c.orders > 0 || c.delivered > 0 || c.ridersHere > 0 || c.servingRiders > 0);

  $('#zoneHint').textContent = `${z.cellsX}×${z.cellsY} 格 · 每格约 ${(z.cellM / 1000).toFixed(2)} km`;

  const sig = JSON.stringify(interesting.map((c) =>
    [c.col, c.row, c.orders, c.ordersActive, c.delivered, c.onTimeRate, c.servingRiders,
     c.delta, c.verdict, c.pressure, c.cause]));
  const html = interesting.length ? interesting.map((c) => `
    <div class="zitem">
      <span class="cell">(${c.col},${c.row})</span>
      <span class="nums">
        单 <b>${c.orders}</b> · 在途 <b>${c.ordersActive}</b> · 送达 <b>${c.delivered}</b>
        ${c.onTimeRate == null ? '' : `· 准时 <b>${c.onTimeRate}%</b>`}
        · 骑手 <b>${c.servingRiders}</b>
        ${c.pressure == null ? '' : `· 压力 <b>${c.pressure}</b>`}
        ${c.delta === 0 ? '' : `· <b style="color:${c.delta > 0 ? '#b91c1c' : '#047857'}">${c.delta > 0 ? '+' : ''}${c.delta} 人</b>`}
        ${c.cause ? `<br><span class="cause">${esc(c.cause)}</span>` : ''}
      </span>
      <span class="v ${c.verdict}">${c.verdictLabel}</span>
    </div>`).join('') : '<div class="empty">这个窗口里还没有订单</div>';
  setHTML(el, html, sig);
}

/* ---- 热力图图例 ---- */

function heatRamp() {
  // 从冷到热的 5 档
  return ['#dbeafe', '#93c5fd', '#fde68a', '#fb923c', '#dc2626'];
}

function renderHeatLegend() {
  const el = $('#heatLegend');
  const on = $('#heatOn').checked;
  if (!on) { setHTML(el, '<span class="hint">勾选后在地图上叠加</span>', 'off'); return; }
  const z = sb.zones;
  const mode = $('#heatMode').value;
  const label = { orders: '订单量', pressure: '运力压力', ontime: '准时率' }[mode];

  let unit = '';
  let maxTxt = '';
  if (z && z.ok) {
    const vals = (z.zones || []).map((c) => heatValue(c, mode)).filter((v) => v != null);
    if (vals.length) {
      const mx = Math.max(...vals);
      const mn = Math.min(...vals);
      unit = mode === 'ontime' ? '%' : '';
      maxTxt = `${Math.round(mn)}${unit} ~ ${Math.round(mx)}${unit}`;
    }
  }

  const html = `<div class="ramp">${heatRamp().map((c) => `<i style="background:${c}"></i>`).join('')}</div>
    <div class="scale"><span>低</span><span>${label}${maxTxt ? '（' + maxTxt + '）' : ''}</span><span>高</span></div>`;
  setHTML(el, html, 'legend|' + mode + '|' + maxTxt);
}

/** 一个格子在当前热力模式下的取值；null 表示不该上色。 */
function heatValue(c, mode) {
  if (mode === 'orders') return c.orders > 0 ? c.orders : null;
  if (mode === 'pressure') return c.pressure == null ? (c.orders > 0 ? 999 : null) : c.pressure;
  if (mode === 'ontime') return c.delivered >= 2 && c.onTimeRate != null ? c.onTimeRate : null;
  return null;
}

/** 在一张画布上画热力网格。 */
function drawHeatmap(ctx, X, Y, T) {
  const z = sb.zones;
  if (!z || !z.ok || !z.zones) return;
  const mode = sb.heat.mode;
  const ramp = heatRamp();

  const vals = z.zones.map((c) => heatValue(c, mode)).filter((v) => v != null && v < 900);
  if (!vals.length) return;
  const mx = Math.max(...vals);
  const mn = mode === 'ontime' ? Math.min(...vals) : 0;
  const span = Math.max(1e-6, mx - mn);

  for (const c of z.zones) {
    const v = heatValue(c, mode);
    if (v == null) continue;
    if (v >= 900) {
      // 压力无穷：有单但完全没人管，用最深的红
      ctx.fillStyle = 'rgba(220,38,38,0.50)';
    } else {
      let f = (v - mn) / span;
      if (mode === 'ontime') f = 1 - f;     // 准时率越低越红
      const idx = Math.max(0, Math.min(ramp.length - 1, Math.round(f * (ramp.length - 1))));
      // 下限别太低，否则热力层在地图上几乎看不见
      const alpha = 0.22 + 0.42 * Math.max(0.2, f);
      ctx.fillStyle = hexA(ramp[idx], alpha);
    }
    const x0 = X(c.x0), y0 = Y(c.y0);
    const w = (c.x1 - c.x0) * T.sx, h = (c.y1 - c.y0) * T.sy;
    ctx.fillRect(x0, y0, w, h);
    // 白描边让格子之间分得开
    ctx.strokeStyle = 'rgba(255,255,255,0.75)';
    ctx.lineWidth = 1;
    ctx.strokeRect(x0 + 0.5, y0 + 0.5, w - 1, h - 1);

    // 格子够大就把单量写上去，够小就不写免得糊成一片
    if (w > 44 && h > 26 && c.orders > 0) {
      ctx.fillStyle = 'rgba(17,24,39,0.78)';
      ctx.font = '700 10px "Segoe UI", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(String(c.orders), x0 + w / 2, y0 + 4);
    }
  }
}

/** #rrggbb + alpha → rgba() */
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a.toFixed(3)})`;
}

/* ---- AI 分析面板 ---- */

function renderLlm() {
  const st = sb.llm.status;
  const cfg = sb.llm.config;
  const badge = $('#llmState');
  if (!st || !cfg) { badge.textContent = '—'; badge.className = 'pill'; return; }

  const state = st.state;
  badge.textContent = state === 'RUNNING' ? '分析中…'
      : state === 'DONE' ? '已完成'
      : state === 'ERROR' ? '出错' : (cfg.configured ? '就绪' : '未配置');
  badge.className = 'pill ' + (state === 'DONE' ? 'DELIVERED'
      : state === 'ERROR' ? 'BUSY'
      : state === 'RUNNING' ? 'ASSIGNED' : (cfg.configured ? 'ONLINE' : 'BUSY'));

  const el = $('#llmOut');
  let html = '';
  if (state === 'RUNNING') {
    html = '<span class="running">正在调用大模型分析…（这一步要几秒到几十秒，模拟不会停下来）</span>';
  } else if (state === 'ERROR') {
    html = `<span class="err">${esc(st.error)}</span>`;
  } else if (state === 'DONE' && st.text) {
    html = miniMarkdown(st.text);
  } else if (!cfg.configured) {
    html = '<span class="running">还没有配置 API Key。展开下面的「模型配置」填一个即可 —— '
         + '不填也不影响热力图和区域研判，那些都是本地算的。</span>';
  } else {
    html = '<span class="running">点「立即分析」，让大模型解读这批数据。</span>';
  }
  setHTML(el, html, state + '|' + (st.text || '').length + '|' + st.error + '|' + (cfg.configured ? 1 : 0));

  $('#llmRun').disabled = state === 'RUNNING';
}

function renderLlmConfig() {
  const cfg = sb.llm.config;
  if (!cfg) return;
  const sel = $('#llmProvider');
  const sig = JSON.stringify((cfg.providers || []).map((p) => p.id));
  if (sel._sig !== sig) {
    sel._sig = sig;
    sel.innerHTML = (cfg.providers || []).map((p) =>
      `<option value="${esc(p.id)}" ${p.id === cfg.provider ? 'selected' : ''}>${esc(p.label)}</option>`).join('');
  }
  if (!$('#llmBaseUrl')._touched) $('#llmBaseUrl').value = cfg.baseUrl || '';
  if (!$('#llmModel')._touched) $('#llmModel').value = cfg.model || '';
  const keyEl = $('#llmKey');
  if (cfg.configured && !keyEl._touched) {
    keyEl.placeholder = `已配置（${cfg.keyHint}，来自 ${cfg.keySource}）—— 留空则不改动`;
  }
  $('#llmAuto').value = String(cfg.autoIntervalSec || 0);

  const note = $('#llmNote');
  const n = `写入 ${cfg.configFile}；环境变量优先级更高。`
      + (cfg.keySource === 'env' ? ' 当前 Key 来自环境变量。' : '');
  if (note.textContent !== n) note.textContent = n;
}

/** 极简 Markdown → HTML：只支持标题、粗体、列表。够用且没有依赖。 */
function miniMarkdown(md) {
  const lines = md.split(/\r?\n/);
  const out = [];
  let inList = false;
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  for (const raw of lines) {
    const line = raw.trimEnd();
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    const li = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
    if (h) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h4>${inline(h[2])}</h4>`);
    } else if (li) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${inline(li[1])}</li>`);
    } else if (!line) {
      if (inList) { out.push('</ul>'); inList = false; }
    } else {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<div>${inline(line)}</div>`);
    }
  }
  if (inList) out.push('</ul>');
  return out.join('');
}

function bindSidebar() {
  $('#sidebarBtn').addEventListener('click', () => setSidebar(!sb.open));
  $('#sidebarClose').addEventListener('click', () => setSidebar(false));
  $('#sbBackdrop').addEventListener('click', () => setSidebar(false));

  const onChange = () => { sb.zonesAt = 0; refreshZones(); };
  $('#heatWindow').addEventListener('change', onChange);
  $('#heatCells').addEventListener('change', onChange);
  $('#heatOn').addEventListener('change', () => { renderHeatLegend(); drawMap(S.cur); });
  $('#heatMode').addEventListener('change', () => { renderHeatLegend(); drawMap(S.cur); });

  $('#llmRun').addEventListener('click', async () => {
    const win = Number($('#heatWindow').value);
    const cells = Number($('#heatCells').value);
    const r = await post('/api/llm/analyze', { windowMin: win, cells });
    if (!r.ok) toast(r.error, 'err');
    refreshLlm();
  });

  $('#llmProvider').addEventListener('change', (e) => {
    // 换供应商时把地址和模型先按预设填上，用户还能接着改
    const p = (sb.llm.config && sb.llm.config.providers || []).find((x) => x.id === e.target.value);
    if (p) {
      const bu = $('#llmBaseUrl'), md = $('#llmModel');
      bu.value = p.baseUrl || '';
      md.value = p.model || '';
      bu._touched = false;
      md._touched = false;
    }
  });
  ['#llmBaseUrl', '#llmModel', '#llmKey'].forEach((s) => {
    $(s).addEventListener('input', (e) => { e.target._touched = true; });
  });

  $('#llmSave').addEventListener('click', async () => {
    const body = {
      provider: $('#llmProvider').value,
      baseUrl: $('#llmBaseUrl').value.trim(),
      model: $('#llmModel').value.trim(),
      autoIntervalSec: Number($('#llmAuto').value),
    };
    const key = $('#llmKey').value.trim();
    if (key) body.apiKey = key;          // 留空 = 不改动已存的 Key
    const r = await post('/api/llm/config', body);
    toast(r.ok ? r.message : (r.error || '保存失败'), r.ok ? 'ok' : 'err');
    $('#llmKey').value = '';
    $('#llmKey')._touched = false;
    ['#llmBaseUrl', '#llmModel'].forEach((s) => { $(s)._touched = false; });
    refreshLlm();
    scheduleAutoLlm();
  });

  $('#llmClear').addEventListener('click', async () => {
    await post('/api/llm/clear', {});
    refreshLlm();
  });

  $('#llmAuto').addEventListener('change', async () => {
    await post('/api/llm/config', { autoIntervalSec: Number($('#llmAuto').value) });
    refreshLlm();
    scheduleAutoLlm();
  });
}

/** 按配置的间隔自动触发分析（真实时间，不是模拟时间）。 */
function scheduleAutoLlm() {
  if (sb.autoTimer) { clearInterval(sb.autoTimer); sb.autoTimer = null; }
  const sec = sb.llm.config ? (sb.llm.config.autoIntervalSec || 0) : 0;
  if (sec <= 0) return;
  sb.autoTimer = setInterval(async () => {
    if (!sb.open) return;                       // 侧边栏关着就不烧 token
    if (sb.llm.status && sb.llm.status.state === 'RUNNING') return;
    await post('/api/llm/analyze', {
      windowMin: Number($('#heatWindow').value),
      cells: Number($('#heatCells').value),
    });
    refreshLlm();
  }, sec * 1000);
}

/* ------------------------------------------------------------ 地图 */

const MAP_PAD = 16;

/** 「6.2km × 5.8km」这样的范围标签。 */
function extentLabel(w, h) {
  const f = (v) => (v / 1000).toFixed(1).replace(/\.0$/, '');
  return `${f(w)}km × ${f(h)}km`;
}

/**
 * 只看一个骑手时，把视野缩到「骑手当前位置 + 他所有待办站点」的包围盒上。
 * 没有聚焦就返回 null，表示看整个世界。
 */
function focusBox() {
  if (!ui.focusRider || !S.cur) return null;
  const r = S.cur.riders.find((x) => x.id === ui.focusRider);
  if (!r) return null;
  if (!r.route.length) {
    const pad = 900;
    return { x0: r.x - pad, y0: r.y - pad, x1: r.x + pad, y1: r.y + pad };
  }
  let x0 = r.x, x1 = r.x, y0 = r.y, y1 = r.y;
  for (const s of r.route) {
    if (s.x < x0) x0 = s.x;
    if (s.x > x1) x1 = s.x;
    if (s.y < y0) y0 = s.y;
    if (s.y > y1) y1 = s.y;
  }
  // 留 20% 余量；再兜一个最小视野，免得只有一单时贴得太近
  const padX = Math.max(400, (x1 - x0) * 0.2);
  const padY = Math.max(400, (y1 - y0) * 0.2);
  return { x0: x0 - padX, y0: y0 - padY, x1: x1 + padX, y1: y1 + padY };
}

/** 当前世界的范围（米）。抽象模式是正方形，路网模式是路网 bbox 的实际宽高。 */
function worldExtent() {
  const c = S.cur;
  return {
    w: (c && c.worldW) || 10000,
    h: (c && c.worldH) || 10000,
    fallback: (c && c.city) || 10000,
  };
}

/**
 * 世界坐标 → 画布坐标。
 * stretch = true 时两个方向各自缩放，把地图铺满画布（用于扁平的小地图）；
 * 聚焦某个骑手时按他的包围盒缩放（等价于放大地图）；
 * 否则等比缩放并居中显示整个世界。
 */
function mapTransform(canvas, stretch) {
  const W = canvas.clientWidth;
  const H = canvas.clientHeight;
  const E = worldExtent();

  if (stretch) {
    const sx = (W - MAP_PAD) / E.w;
    const sy = (H - MAP_PAD) / E.h;
    return { sx, sy, ox: MAP_PAD / 2, oy: MAP_PAD / 2, w: E.w, h: E.h, W, H, box: null };
  }

  const box = focusBox();
  if (box) {
    const bw = Math.max(1, box.x1 - box.x0);
    const bh = Math.max(1, box.y1 - box.y0);
    const s = Math.min((W - MAP_PAD * 2) / bw, (H - MAP_PAD * 2) / bh);
    return {
      sx: s, sy: s,
      ox: (W - bw * s) / 2 - box.x0 * s,
      oy: (H - bh * s) / 2 - box.y0 * s,
      w: E.w, h: E.h, W, H, box,
    };
  }

  const s = Math.min((W - MAP_PAD * 2) / E.w, (H - MAP_PAD * 2) / E.h);
  return {
    sx: s, sy: s,
    ox: (W - E.w * s) / 2,
    oy: (H - E.h * s) / 2,
    w: E.w, h: E.h, W, H, box: null,
  };
}

function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

/** 在两份快照之间插值，让骑手移动看起来是连续的，而不是每 500ms 跳一下。 */
function interpolatedRiders() {
  if (!S.cur) return [];
  const R = S.cur.riders;
  if (!S.prev) return R;
  const span = Math.max(120, S.curAt - S.prevAt);
  const t = Math.min(1, (performance.now() - S.curAt) / span);
  const old = new Map(S.prev.riders.map(r => [r.id, r]));
  return R.map(r => {
    const p = old.get(r.id);
    if (!p) return r;
    return Object.assign({}, r, {
      x: p.x + (r.x - p.x) * t,
      y: p.y + (r.y - p.y) * t,
    });
  });
}

function drawMap(st) {
  const canvas = $('#map');
  if (!canvas || !canvas.clientWidth) return;
  const ctx = fitCanvas(canvas);
  const T = mapTransform(canvas);
  const X = (x) => T.ox + x * T.sx;
  const Y = (y) => T.oy + y * T.sy;
  const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 420);

  ctx.clearRect(0, 0, T.W, T.H);

  // 城外底色
  ctx.fillStyle = '#f7f8fa';
  ctx.fillRect(0, 0, T.W, T.H);

  // 城区底
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(X(0), Y(0), T.w * T.sx, T.h * T.sy);

  if (hasRoads()) {
    // 路网模式：直接贴路网底图，不画方格纸网格（真实路网 + 方格网会很乱）
    const layer = ensureRoadLayer(T);
    if (layer) {
      ctx.drawImage(layer, X(0), Y(0), T.w * T.sx, T.h * T.sy);
    }
  } else {
    // 网格
    ctx.strokeStyle = '#eef0f3';
    ctx.lineWidth = 1;
    for (let g = 1000; g < T.w; g += 1000) {
      ctx.beginPath(); ctx.moveTo(X(g), Y(0)); ctx.lineTo(X(g), Y(T.h)); ctx.stroke();
    }
    for (let g = 1000; g < T.h; g += 1000) {
      ctx.beginPath(); ctx.moveTo(X(0), Y(g)); ctx.lineTo(X(T.w), Y(g)); ctx.stroke();
    }
  }

  ctx.strokeStyle = '#e0e3e8';
  ctx.strokeRect(X(0), Y(0), T.w * T.sx, T.h * T.sy);

  // 城区标注（只在没放大时画，免得被裁得只剩半行字）
  if (!T.box) {
    ctx.fillStyle = '#c8ccd4';
    ctx.font = '600 10px "Segoe UI", "Microsoft YaHei", sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillText(extentLabel(T.w, T.h), X(0), Y(0) - 5);
  }

  // 热力层：画在底图之上、骑手之下，这样路线和骑手仍然压在最上面看得清。
  // 开关和模式都直接读 DOM —— 单一事实来源，不用再维护一份影子状态。
  const heatOn = $('#heatOn');
  if (heatOn && heatOn.checked) {
    sb.heat.mode = $('#heatMode') ? $('#heatMode').value : 'orders';
    drawHeatmap(ctx, X, Y, T);
  }

  // 商家
  for (const m of st.merchants) {
    const x = X(m.x), y = Y(m.y);
    ctx.fillStyle = '#f97316';
    ctx.beginPath();
    ctx.roundRect(x - 5, y - 5, 10, 10, 3);
    ctx.fill();
    ctx.fillStyle = '#9a3412';
    ctx.font = '600 10px "Segoe UI", "Microsoft YaHei", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(m.name, x, y + 8);
  }

  // 未派单订单：商家处脉冲提示
  const poolByMerchant = new Map();
  for (const o of st.orders) {
    if (o.status !== 'POOLED') continue;
    const k = o.merchantId;
    poolByMerchant.set(k, (poolByMerchant.get(k) || 0) + 1);
  }
  for (const m of st.merchants) {
    const n = poolByMerchant.get(m.id);
    if (!n) continue;
    const x = X(m.x), y = Y(m.y);
    ctx.strokeStyle = `rgba(245, 158, 11, ${0.25 + 0.5 * pulse})`;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x, y, 12 + 6 * pulse, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = '#b45309';
    ctx.font = '700 10px "Segoe UI", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(n), x, y);
  }

  // 只看某个骑手时，其他骑手直接不画（路线和圆点都不画）
  const riders = interpolatedRiders()
    .filter((r) => !ui.focusRider || r.id === ui.focusRider);

  // 骑手路线
  for (const r of riders) {
    if (!r.route.length) continue;
    const c = riderColor(r.id);
    const sel = ui.focusRider === r.id || ui.selectedRider === r.id;

    ctx.strokeStyle = c;
    ctx.globalAlpha = sel ? 0.95 : 0.45;
    ctx.lineWidth = sel ? 2.5 : 1.6;

    // 拼出「当前位置 → 首站 → … → 末站」的完整折线。后端把它拆成两段：
    // leg（当前位置到首站，每一拍都在变）和 tail（首站往后，只在路线变化时才变），
    // 两段各有自己的缓存，这里拼起来画。
    const pts = [];
    if (r.leg && r.leg.length >= 4) {
      for (let i = 0; i < r.leg.length; i += 2) pts.push([r.leg[i] / 10, r.leg[i + 1] / 10]);
    }
    if (r.tail && r.tail.length >= 4) {
      for (let i = 0; i < r.tail.length; i += 2) {
        const x = r.tail[i] / 10, y = r.tail[i + 1] / 10;
        if (i === 0 && pts.length
            && Math.abs(pts[pts.length - 1][0] - x) < 0.05
            && Math.abs(pts[pts.length - 1][1] - y) < 0.05) {
          continue;                    // 接缝处重复的点，跳掉
        }
        pts.push([x, y]);
      }
    }

    ctx.beginPath();
    if (pts.length >= 2) {
      ctx.moveTo(X(pts[0][0]), Y(pts[0][1]));
      for (let i = 1; i < pts.length; i++) ctx.lineTo(X(pts[i][0]), Y(pts[i][1]));
    } else {
      ctx.moveTo(X(r.x), Y(r.y));      // 兜底：直连站点
      for (const s of r.route) ctx.lineTo(X(s.x), Y(s.y));
    }
    ctx.stroke();
    ctx.globalAlpha = 1;

    // 站点
    r.route.forEach((s, i) => {
      const x = X(s.x), y = Y(s.y);
      if (s.type === 'PICKUP') {
        ctx.fillStyle = '#fff';
        ctx.strokeStyle = c;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.roundRect(x - 4, y - 4, 8, 8, 2);
        ctx.fill(); ctx.stroke();
      } else {
        ctx.fillStyle = '#fff';
        ctx.strokeStyle = '#10b981';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x, y, 4.5, 0, Math.PI * 2);
        ctx.fill(); ctx.stroke();
      }
      if (sel) {
        ctx.fillStyle = '#111827';
        ctx.font = '700 9px "Segoe UI", sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(i + 1), x, y - 9);
      }
    });
  }

  // 骑手
  for (const r of riders) {
    const x = X(r.x), y = Y(r.y);
    const c = riderColor(r.id);
    const sel = ui.focusRider === r.id || ui.selectedRider === r.id;

    if (sel) {
      ctx.strokeStyle = c;
      ctx.globalAlpha = 0.3;
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 13, 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = 1;
    }
    if (r.motion === 'WAITING') {
      ctx.strokeStyle = '#f59e0b';
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 10 + 3 * pulse, 0, Math.PI * 2); ctx.stroke();
    }

    ctx.fillStyle = r.status === 'ONLINE' ? c : '#9ca3af';
    ctx.beginPath(); ctx.arc(x, y, sel ? 7 : 5.5, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.fillStyle = '#374151';
    ctx.font = '600 9.5px "Segoe UI", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(r.id, x, y + 8);
  }
}

/* ------------------------------------------------------------ 顾客选点小地图 */

function initPickMap(st) {
  const canvas = $('#pickMap');
  canvas._init = true;
  canvas.addEventListener('click', (ev) => {
    const rect = canvas.getBoundingClientRect();
    const T = mapTransform(canvas, true);
    const x = (ev.clientX - rect.left - T.ox) / T.sx;
    const y = (ev.clientY - rect.top - T.oy) / T.sy;
    if (x < 0 || y < 0 || x > T.w || y > T.h) return;
    ui.pick = { x: Math.round(x), y: Math.round(y) };
    const m = currentMerchant();
    if (m) {
      const d = Math.hypot(ui.pick.x - m.x, ui.pick.y - m.y) * 1.4;
      $('#pickInfo').textContent = `已选 (${ui.pick.x}, ${ui.pick.y})，离商家约 ${(d / 1000).toFixed(2)} km`;
    }
    drawPickMap();
  });
}

function currentMerchant() {
  if (!S.cur) return null;
  const id = $('#cMerchant').value;
  return S.cur.merchants.find(m => m.id === id) || S.cur.merchants[0];
}

function drawPickMap() {
  const canvas = $('#pickMap');
  if (!canvas || !canvas.clientWidth || !S.cur) return;
  const ctx = fitCanvas(canvas);
  const T = mapTransform(canvas, true);
  const X = (x) => T.ox + x * T.sx;
  const Y = (y) => T.oy + y * T.sy;

  ctx.clearRect(0, 0, T.W, T.H);
  ctx.fillStyle = '#fff';
  ctx.fillRect(X(0), Y(0), T.w * T.sx, T.h * T.sy);
  ctx.strokeStyle = '#eef0f3';
  ctx.lineWidth = 1;
  for (let g = 2000; g < T.w; g += 2000) {
    ctx.beginPath(); ctx.moveTo(X(g), Y(0)); ctx.lineTo(X(g), Y(T.h)); ctx.stroke();
  }
  for (let g = 2000; g < T.h; g += 2000) {
    ctx.beginPath(); ctx.moveTo(X(0), Y(g)); ctx.lineTo(X(T.w), Y(g)); ctx.stroke();
  }
  ctx.strokeStyle = '#e0e3e8';
  ctx.strokeRect(X(0), Y(0), T.w * T.sx, T.h * T.sy);

  // 所有商家，选中的高亮
  const sel = currentMerchant();
  for (const m of S.cur.merchants) {
    const x = X(m.x), y = Y(m.y);
    const on = sel && m.id === sel.id;
    ctx.fillStyle = on ? '#ea580c' : '#fdba74';
    ctx.beginPath(); ctx.arc(x, y, on ? 5 : 3, 0, Math.PI * 2); ctx.fill();
    if (on) {
      ctx.strokeStyle = '#f97316';
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = '#9a3412';
      ctx.font = '600 9px "Segoe UI", "Microsoft YaHei", sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      ctx.fillText(m.name, x, y + 11);
    }
  }

  if (ui.pick) {
    const x = X(ui.pick.x), y = Y(ui.pick.y);
    if (sel) {
      ctx.strokeStyle = '#94a3b8';
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(X(sel.x), Y(sel.y)); ctx.lineTo(x, y); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.fillStyle = '#10b981';
    ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
  }
}

/* ------------------------------------------------------------ 事件绑定 */

function bind() {
  // 页签
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => {
      ui.tab = t.dataset.view;
      document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === t));
      document.querySelectorAll('.view').forEach(v =>
        v.classList.toggle('active', v.id === 'view-' + ui.tab));
      if (ui.tab === 'console') { drawMap(S.cur); }
      if (ui.tab === 'customer') { drawPickMap(); }
    });
  });

  // 顶栏
  $('#pauseBtn').addEventListener('click', async () => {
    await post('/api/control', { paused: !S.cur.sim.paused });
    poll();
  });
  $('#dispatchBtn').addEventListener('click', async () => {
    const r = await post('/api/dispatch', {});
    toast(r.message || '已触发派单', r.ok ? 'ok' : 'err');
    poll();
  });
  // 批量新增骑手
  $('#addRiderBtn').addEventListener('click', async () => {
    const n = Number($('#addRiderCount').value) || 1;
    const btn = $('#addRiderBtn');
    btn.disabled = true;
    const added = [];
    try {
      for (let i = 0; i < n; i++) {
        const r = await post('/api/rider/add', {});
        if (r.ok) added.push(r.riderId);
        else { toast(r.error, 'err'); break; }
      }
      if (added.length) toast(`已新增骑手 ${added.join('、')}`, 'ok');
      poll();
    } finally {
      btn.disabled = false;
    }
  });

  $('#resetBtn').addEventListener('click', async () => {
    if (!confirm('确定重置整个模拟吗？所有订单和骑手状态都会清空。')) return;
    await post('/api/reset', {});
    S.cfgFilled = false;
    ui.selectedRider = null; ui.pick = null; ui.assignOrder = null; ui.reassignOrder = null;
    ui.focusRider = null;
    await loadRoads();
    toast('模拟已重置', 'ok');
    poll();
  });
  $('#speedSel').addEventListener('change', async (e) => {
    await post('/api/control', { speed: Number(e.target.value) });
  });

  // 切换路网（会重置整个模拟）
  $('#netApply').addEventListener('click', async () => {
    const id = $('#netSel').value;
    if (!id) return;
    if (!confirm('切换路网会重置整个模拟（商家、骑手、订单全部重建）。继续吗？')) return;
    const btn = $('#netApply');
    btn.disabled = true;
    btn.textContent = '加载中…';
    try {
      const r = await post('/api/network', { id });
      toast(r.ok ? r.message : r.error, r.ok ? 'ok' : 'err');
      if (r.ok) {
        ui.focusRider = null;
        ui.selectedRider = null;
        S.cfgFilled = false;
        S.networks = null;
        await loadRoads();
        await loadNetworks();
        await poll();
      }
    } finally {
      btn.disabled = false;
      btn.textContent = '切换（会重置模拟）';
    }
  });

  // 参数（失焦或改变时提交）
  const cfgMap = {
    cfgInterval: 'dispatchIntervalSec',
    cfgDetourM: 'onRouteMaxDetourM',
    cfgDetourRatio: 'onRouteMaxDetourRatio',
    cfgLoad: 'loadPenaltyPerOrderM',
    cfgSla: 'slaMinutes',
    cfgAutoEvery: 'autoOrderEverySec',
  };
  Object.entries(cfgMap).forEach(([id, key]) => {
    $('#' + id).addEventListener('change', async (e) => {
      const v = Number(e.target.value);
      if (Number.isFinite(v)) await post('/api/control', { [key]: v });
    });
  });
  $('#cfgAutoOrder').addEventListener('change', async (e) => {
    await post('/api/control', { autoOrder: e.target.checked });
  });
  $('#cfgAvoidWait').addEventListener('change', async (e) => {
    await post('/api/control', { avoidWaiting: e.target.checked });
  });

  // 停单：一键开关（顶栏 + 卡片里各一个入口）+ 阈值
  $('#intakeToggle').addEventListener('click', toggleIntake);
  $('#intakeCardToggle').addEventListener('click', toggleIntake);

  // 阈值快捷预设：一键设成常用档位
  document.querySelectorAll('.preset').forEach((b) => {
    b.addEventListener('click', async () => {
      const pct = Number(b.dataset.ratio);
      await post('/api/control', { stopAcceptPoolRatio: pct / 100 });
      toast(`停单阈值已设为 ${pct}%`, 'ok');
      poll();
    });
  });

  $('#cfgAcceptOrders').addEventListener('change', async (e) => {
    const r = await post('/api/control', { acceptOrders: e.target.checked });
    toast(e.target.checked ? '已允许进单' : '已停止进单（管理平台手动）',
          e.target.checked ? 'ok' : 'err');
    poll();
  });
  $('#cfgStopRatio').addEventListener('change', async (e) => {
    const v = Number(e.target.value);
    if (Number.isFinite(v)) {
      await post('/api/control', { stopAcceptPoolRatio: Math.max(0, Math.min(100, v)) / 100 });
      poll();
    }
  });
  $('#cfgStopMin').addEventListener('change', async (e) => {
    const v = Number(e.target.value);
    if (Number.isFinite(v)) { await post('/api/control', { stopAcceptMinOrders: v }); poll(); }
  });
  $('#cfgMaxTotal').addEventListener('change', async (e) => {
    const v = Number(e.target.value);
    if (Number.isFinite(v)) { await post('/api/control', { maxTotalOrders: v }); poll(); }
  });

  // 顺路占比调节（ACA 式推迟）
  // 两个开关（指标卡里的一键开关 + 派单参数卡里的复选框）必须手动同步：
  // 派单参数卡只在首次拿到状态时填一次，不会跟着刷新。
  const setPostpone = async (on) => {
    await post('/api/control', { postponePoorAssignments: on });
    $('#kpiPostpone').checked = on;
    $('#cfgPostpone').checked = on;
    toast(on ? '已开启：兜底单先留一轮' : '已关闭：兜底单立即派出', on ? 'ok' : '');
    poll();
  };
  $('#kpiPostpone').addEventListener('change', (e) => setPostpone(e.target.checked));
  $('#cfgPostpone').addEventListener('change', (e) => setPostpone(e.target.checked));
  const postMap = {
    cfgPostponeRounds: 'postponeMaxRounds',
    cfgPostponeWait: 'postponeMaxWaitMin',
    cfgPostponeFactor: 'postponeMaxPoolFactor',
  };
  Object.entries(postMap).forEach(([id, key]) => {
    $('#' + id).addEventListener('change', async (e) => {
      const v = Number(e.target.value);
      if (Number.isFinite(v)) { await post('/api/control', { [key]: v }); poll(); }
    });
  });

  // 顾客端
  $('#cMerchant').addEventListener('change', drawPickMap);
  // 随机填写：只把表单填上，不提交 —— 方便看着改
  $('#randomFill').addEventListener('click', async () => {
    try {
      const r = await fetch('/api/order/random', { cache: 'no-store', headers: authHeaders() });
      const j = await r.json();
      if (!j.ok) { toast(j.error || '生成失败', 'err'); return; }
      $('#cName').value = j.customer.name;
      $('#cPhone').value = j.customer.phone;
      $('#cAddress').value = j.customer.address;
      $('#cNote').value = j.customer.note;
      $('#cMerchant').value = j.merchantId;
      ui.pick = { x: Math.round(j.dest.x), y: Math.round(j.dest.y) };
      $('#pickInfo').textContent =
        `随机到 ${j.merchantName}，(x=${Math.round(j.dest.x)}, y=${Math.round(j.dest.y)})`;
      drawPickMap();
      toast('已随机填写，可以直接提交或先改', 'ok');
    } catch (e) {
      toast('生成失败：' + e, 'err');
    }
  });

  // 一键随机下单：服务端直接下单，可以一次下多笔
  $('#randomOrder').addEventListener('click', async () => {
    const count = Number($('#randomCount').value) || 1;
    const btn = $('#randomOrder');
    btn.disabled = true;
    try {
      const r = await post('/api/order/auto', { count });
      if (r.ok) {
        toast(r.message || `已生成 ${r.placed} 笔订单`, 'ok');
      } else {
        toast(r.error || '没有生成订单', 'err');
      }
      poll();
    } finally {
      btn.disabled = false;
    }
  });
  $('#showAllOrders').addEventListener('change', (e) => {
    ui.showAll = e.target.checked;
    renderOrders(S.cur);
  });
  $('#submitOrder').addEventListener('click', async () => {
    const name = $('#cName').value.trim();
    const phone = $('#cPhone').value.trim();
    const address = $('#cAddress').value.trim();
    const note = $('#cNote').value.trim();
    const msg = $('#orderMsg');
    if (!name || !phone || !address) {
      msg.className = 'msg err';
      msg.textContent = '姓名、电话、地址都要填。';
      return;
    }
    const body = { name, phone, address, note, merchantId: $('#cMerchant').value };
    if (ui.pick) { body.dx = ui.pick.x; body.dy = ui.pick.y; }
    const r = await post('/api/order', body);
    msg.className = 'msg ' + (r.ok ? 'ok' : 'err');
    msg.textContent = r.ok ? r.message : r.error;
    if (r.ok) {
      $('#cName').value = ''; $('#cPhone').value = ''; $('#cAddress').value = ''; $('#cNote').value = '';
      ui.pick = null;
      $('#pickInfo').textContent = '在地图上点一下选位置；不选就随机生成';
      toast('下单成功：' + r.orderId, 'ok');
      poll();
    }
  });

  // 列表内的通用操作（事件委托）
  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-act]');
    if (btn) {
      const act = btn.dataset.act;
      const id = btn.dataset.id;

      if (act === 'assign') { ui.assignOrder = id; ui.reassignOrder = null; render(); return; }
      if (act === 'reassign') { ui.reassignOrder = id; ui.assignOrder = null; render(); return; }
      if (act === 'cancel') { ui.assignOrder = null; ui.reassignOrder = null; render(); return; }
      if (act === 'pick-rider') { ui.selectedRider = id; render(); return; }
      if (act === 'focus-set') {
        // 同一个骑手再点一次 = 取消聚焦
        ui.focusRider = ui.focusRider === id ? null : id;
        ui.selectedRider = id;
        if (ui.focusRider) toast(`地图上只看 ${id}，已隐藏其他骑手`, 'ok');
        render();
        return;
      }
      if (act === 'focus-clear') { ui.focusRider = null; render(); return; }

      if (act === 'remove-rider') {
        const r = (S.cur.riders || []).find((x) => x.id === id);
        const busy = r ? r.activeOrders : 0;
        const extra = busy
          ? `\n他手上有 ${busy} 单未完成：还没取餐的会退回订单池重新派单；`
            + `如果其中有已取餐的，这个操作会被拒绝。`
          : '';
        if (!confirm(`确定移除骑手 ${id}${r ? ' ' + r.name : ''}？${extra}`)) return;
        const res = await post('/api/rider/remove', { riderId: id });
        toast(res.ok ? res.message : res.error, res.ok ? 'ok' : 'err');
        if (res.ok && ui.focusRider === id) ui.focusRider = null;
        if (res.ok && ui.selectedRider === id) ui.selectedRider = null;
        poll();
        return;
      }

      if (act === 'assign-go') {
        const riderId = $('#assignRider') && $('#assignRider').value;
        if (!riderId) { toast('没有可用的骑手', 'err'); return; }
        const r = await post('/api/assign', { orderId: id, riderId });
        toast(r.ok ? r.message : r.error, r.ok ? 'ok' : 'err');
        ui.assignOrder = null;
        poll();
        return;
      }
      if (act === 'reassign-go') {
        const riderId = $('#reassignRider') && $('#reassignRider').value;
        if (!riderId) { toast('没有可用的骑手', 'err'); return; }
        const r = await post('/api/reassign', { orderId: id, riderId });
        toast(r.ok ? r.message : r.error, r.ok ? 'ok' : 'err');
        ui.reassignOrder = null;
        poll();
        return;
      }
    }

    // 下拉框直接生效
    const sel = ev.target.closest('select[data-act]');
    if (sel) {
      const act = sel.dataset.act;
      const id = sel.dataset.id;
      if (act === 'focus-pick') {
        ui.focusRider = sel.value;
        ui.selectedRider = sel.value;
        render();
        return;
      }
      if (act === 'rider-status') {
        const r = await post('/api/rider', { riderId: id, status: sel.value });
        toast(r.ok ? `骑手 ${id} 已切换为 ${sel.value === 'ONLINE' ? '上线' : '忙碌'}` : r.error,
              r.ok ? 'ok' : 'err');
        poll();
      }
      if (act === 'rider-cap') {
        const r = await post('/api/rider', { riderId: id, maxOrders: Number(sel.value) });
        toast(r.ok ? `骑手 ${id} 接单上限设为 ${sel.value} 单` : r.error, r.ok ? 'ok' : 'err');
        poll();
      }
    }
  });

  // 地图点击选中骑手
  $('#map').addEventListener('click', (ev) => {
    const canvas = $('#map');
    const rect = canvas.getBoundingClientRect();
    const T = mapTransform(canvas);
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    let best = null, bestD = 22 * 22;
    for (const r of (S.cur ? S.cur.riders : [])) {
      const dx = (T.ox + r.x * T.sx) - mx;
      const dy = (T.oy + r.y * T.sy) - my;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = r.id; }
    }
    if (best) {
      // 在地图上点骑手 = 只看他（再点一次取消）
      const wasFocused = ui.focusRider === best;
      ui.selectedRider = best;
      ui.focusRider = wasFocused ? null : best;
      toast(ui.focusRider ? `地图上只看 ${best}，已隐藏其他骑手` : '已显示全部骑手', 'ok');
      render();
    }
  });

  // Esc 退出聚焦
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && ui.focusRider) {
      ui.focusRider = null;
      render();
    }
  });

  window.addEventListener('resize', () => { drawMap(S.cur); drawPickMap(); });
}

/* ------------------------------------------------------------ 启动 */

bind();
bindSidebar();
loadNetworks();
loadRoads().then(poll);
refreshLlm().then(scheduleAutoLlm);
setInterval(poll, 500);
// 侧边栏开着时：本地统计每 2 秒刷新一次（纯本地计算，很便宜），
// 大模型的状态轮询得慢一些（它一次要几十秒）
setInterval(() => { if (sb.open) refreshZones(); }, 2000);
setInterval(() => { if (sb.open) refreshLlm(); }, 3000);
// 让骑手在两份快照之间平滑移动
(function tick() {
  if (S.cur) {
    if (ui.tab === 'console') drawMap(S.cur);
    else if (ui.tab === 'customer') drawPickMap();
  }
  requestAnimationFrame(tick);
})();
