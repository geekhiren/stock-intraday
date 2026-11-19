'use strict';

const byId = (id) => document.getElementById(id);
const indian = new Intl.NumberFormat('en-IN');
const prices = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' });
const gateNames = {
  closed_candle: 'Closed candle', vwap_crossover: 'VWAP crossover', consolidation: 'Consolidation',
  volume_surge: 'Volume surge', rsi: 'RSI', pivot: 'Pivot proximity', liquidity: 'Option liquidity',
  time_window: 'Entry window', iv: 'IV percentile',
};
const gateStates = { pass: 'Pass', fail: 'Fail', disabled: 'Disabled', not_evaluated: 'Not evaluated' };
const brokerStates = {
  simulated: 'Simulated feed', connected: 'Broker connected', connecting: 'Broker connecting',
  disconnected: 'Broker disconnected', stale: 'Broker feed stale', reconnecting: 'Broker reconnecting',
};
const strategies = { sip: 'Stocks in play', orb: 'Opening range', vwap: 'VWAP crossover', manual: 'Manual', all: 'All trades' };
const setups = { holding_highs: 'Holding highs', holding_lows: 'Holding lows' };
const stop = byId('stop');
let token = null;
const refreshDelay = Number(document.querySelector('meta[name="orderbot-refresh-seconds"]').content) * 1000;
let submitting = false;
let actionError = '';

function money(paise) {
  if (paise === null || paise === undefined) return '—';
  const amount = Math.abs(paise);
  return `${paise < 0 ? '−' : ''}₹${indian.format(Math.floor(amount / 100))}.${String(amount % 100).padStart(2, '0')}`;
}

function price(value) {
  return value === null ? '—' : prices.format(value);
}

function localTime(stamp) {
  // The API has already converted this timestamp to IST.
  return stamp ? stamp.slice(11, 19) : '—';
}

function ukDate(stamp) {
  return stamp ? `${stamp.slice(8, 10)}/${stamp.slice(5, 7)}/${stamp.slice(0, 4)}` : '—';
}

function label(value) {
  return value ? value.replaceAll('_', ' ').replace(/^./, (initial) => initial.toUpperCase()) : '—';
}

function tone(paise) {
  return paise > 0 ? 'positive' : paise < 0 ? 'negative' : '';
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderTable(id, rows, columns, empty) {
  const items = rows.map((cells) => {
    const row = element('tr');
    for (const [value, style, title] of cells) {
      const cell = element('td', undefined, style);
      cell.append(value ?? '—');
      if (title) cell.title = title;
      row.append(cell);
    }
    return row;
  });
  if (!items.length) {
    const row = element('tr');
    const cell = element('td', empty, 'muted');
    cell.colSpan = columns;
    row.append(cell);
    items.push(row);
  }
  byId(id).replaceChildren(...items);
}

function showNotice(message) {
  byId('notice').hidden = !message;
  byId('notice').textContent = message;
}

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'X-OrderBOT-Stop': token, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'The request failed.');
  return data;
}

// -- screens -------------------------------------------------------------------------------------
function showPage() {
  const wanted = location.hash.slice(1);
  const pages = [...document.querySelectorAll('.page')];
  const current = pages.some((page) => page.dataset.page === wanted) ? wanted : 'stocks';
  for (const page of pages) page.hidden = page.dataset.page !== current;
  for (const tab of document.querySelectorAll('.nav-item')) {
    tab.classList.toggle('on', tab.dataset.tab === current);
    if (tab.dataset.tab === current) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
}
window.addEventListener('hashchange', showPage);
showPage();
// A link such as #trade-12 opens that trade's chart directly.
window.addEventListener('load', () => { const linked = /^#trade-(\d+)$/.exec(location.hash); if (linked) showTrade(linked[1]); });

// -- charts ----------------------------------------------------------------------------------------
// Fixed colour per strategy (categorical order validated for colour-blind separation on a white surface).
const seriesColors = { all: '#2a78d6', sip: '#eb6834', orb: '#1baf7a', manual: '#eda100', vwap: '#e87ba4' };
const SVG = 'http://www.w3.org/2000/svg';

function svg(tag, attributes, text) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes || {})) node.setAttribute(name, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

// One y-axis line chart. series: [{name, color, values}], references: [{value, label, color}],
// markers: [{index, value, label}]. Hovering shows a crosshair and one tooltip listing every series.
function lineChart(container, { labels, series, references = [], markers = [], format, zero = false }) {
  const width = Math.max(container.clientWidth, 320);
  const height = 240;
  const pad = { left: 8, right: 96, top: 12, bottom: 22 };
  const all = [...series.flatMap((item) => item.values), ...references.map((item) => item.value), ...(zero ? [0] : [])]
    .filter((value) => value !== null && Number.isFinite(value));
  let low = Math.min(...all);
  let high = Math.max(...all);
  if (low === high) { low -= 1; high += 1; }
  const room = (high - low) * 0.08;
  low -= room; high += room;
  const x = (index) => pad.left + (labels.length > 1 ? index / (labels.length - 1) : 0.5) * (width - pad.left - pad.right);
  const y = (value) => pad.top + (1 - (value - low) / (high - low)) * (height - pad.top - pad.bottom);
  const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img' });
  for (const step of [0, 0.5, 1]) {
    const value = low + room + step * (high - low - 2 * room);
    root.append(svg('line', { class: 'grid', x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value) }),
      svg('text', { x: width - pad.right + 8, y: y(value) + 4 }, format(value)));
  }
  if (zero && low < 0 && high > 0) root.append(svg('line', { class: 'zero', x1: pad.left, x2: width - pad.right, y1: y(0), y2: y(0) }));
  root.append(svg('text', { x: pad.left, y: height - 4 }, labels[0]),
    svg('text', { x: width - pad.right, y: height - 4, 'text-anchor': 'end' }, labels[labels.length - 1]));
  for (const reference of references) {
    root.append(svg('line', { class: 'reference', stroke: reference.color, x1: pad.left, x2: width - pad.right, y1: y(reference.value), y2: y(reference.value) }),
      svg('text', { class: 'label', x: pad.left + 4, y: y(reference.value) - 4 }, reference.label));
  }
  for (const item of series) {
    const points = item.values.map((value, index) => (value === null ? null : `${x(index).toFixed(1)},${y(value).toFixed(1)}`)).filter(Boolean);
    if (points.length > 1) root.append(svg('polyline', { class: 'series', stroke: item.color, points: points.join(' ') }));
    else if (points.length) {
      const [px, py] = points[0].split(',');
      root.append(svg('circle', { class: 'dot', fill: item.color, cx: px, cy: py, r: 5 }));
    }
  }
  for (const marker of markers) {
    root.append(svg('circle', { class: 'dot', fill: '#2A1A12', cx: x(marker.index), cy: y(marker.value), r: 5 }),
      svg('text', { class: 'label', x: x(marker.index) + 8, y: y(marker.value) - 8 }, marker.label));
  }
  const crosshair = svg('line', { class: 'crosshair', y1: pad.top, y2: height - pad.bottom, visibility: 'hidden' });
  const tip = element('div', undefined, 'tip');
  tip.hidden = true;
  root.append(crosshair);
  root.addEventListener('pointermove', (event) => {
    const box = root.getBoundingClientRect();
    const px = (event.clientX - box.left) / box.width * width;
    const index = Math.min(labels.length - 1, Math.max(0, Math.round((px - pad.left) / (width - pad.left - pad.right) * (labels.length - 1))));
    crosshair.setAttribute('x1', x(index));
    crosshair.setAttribute('x2', x(index));
    crosshair.setAttribute('visibility', 'visible');
    const rows = series.filter((item) => item.values[index] !== null).map((item) => {
      const row = element('div');
      const key = element('i');
      key.style.setProperty('--key', item.color);
      row.append(key, item.name, element('span', format(item.values[index])));
      return row;
    });
    tip.replaceChildren(element('b', labels[index]), ...rows);
    tip.hidden = false;
    const left = x(index) / width * box.width;
    tip.style.left = `${left > box.width / 2 ? left - tip.offsetWidth - 12 : left + 12}px`;
  });
  root.addEventListener('pointerleave', () => { crosshair.setAttribute('visibility', 'hidden'); tip.hidden = true; });
  container.replaceChildren(root, tip);
}

let curveKey = '';
function renderCurve(curve) {
  const box = byId('curve');
  const key = JSON.stringify(curve) + box.clientWidth;
  if (key === curveKey) return;  // redraw only when the data or the width changes, so hovering is not interrupted
  curveKey = key;
  const days = [...new Set(curve.map((row) => row.day))];
  if (!days.length) {
    byId('curve-legend').replaceChildren();
    box.replaceChildren(element('p', 'The chart appears after the first closed paper trade.', 'muted'));
    return;
  }
  const names = ['all', ...Object.keys(seriesColors).filter((name) => name !== 'all' && curve.some((row) => row.strategy === name))];
  const series = names.map((name) => {
    let total = 0;
    return {
      name: strategies[name] || label(name), color: seriesColors[name],
      values: days.map((day) => {
        total += curve.filter((row) => row.day === day && (name === 'all' || row.strategy === name)).reduce((sum, row) => sum + row.net_paise, 0);
        return total;
      }),
    };
  });
  byId('curve-legend').replaceChildren(...series.map((item) => {
    const entry = element('span');
    const swatch = element('i');
    swatch.style.setProperty('--key', item.color);
    entry.append(swatch, item.name);
    return entry;
  }));
  lineChart(box, { labels: days.map(ukDate), series, format: (value) => money(Math.round(value)), zero: true });
}

async function showTrade(tradeId) {
  const dialog = byId('trade-dialog');
  byId('trade-title').textContent = 'Loading trade…';
  byId('trade-summary').textContent = '';
  byId('trade-chart').replaceChildren();
  byId('trade-facts').replaceChildren();
  dialog.showModal();
  try {
    const response = await fetch(`/api/stock-chart?trade_id=${encodeURIComponent(tradeId)}`);
    const trade = await response.json();
    if (!response.ok) throw new Error(trade.error || 'The trade could not be loaded.');
    const long = trade.side === 'long';
    byId('trade-title').textContent = `${trade.symbol} · ${ukDate(trade.day)}`;
    byId('trade-summary').textContent = `${strategies[trade.strategy] || label(trade.strategy)} · ${long ? 'Bought' : 'Sold short'} ${trade.quantity} shares at ${trade.entry_at} IST` +
      (trade.exit_at ? ` · closed ${trade.exit_at} (${label(trade.exit_reason).toLowerCase()})` : ' · still open');
    const clocks = trade.points.map((point) => point[0]);
    const nearest = (clock) => Math.max(0, clocks.findIndex((item) => item >= clock));
    const references = [{ value: Number(trade.stop), label: `Stop ${price(trade.stop)}`, color: '#D3343F' }];
    if (trade.target) references.push({ value: Number(trade.target), label: `Target ${price(trade.target)}`, color: '#0F8A57' });
    const markers = [{ index: nearest(trade.entry_at), value: Number(trade.entry), label: 'Entry' }];
    if (trade.exit_at) markers.push({ index: nearest(trade.exit_at), value: Number(trade.exit), label: 'Exit' });
    if (clocks.length) {
      lineChart(byId('trade-chart'), {
        labels: clocks, series: [{ name: trade.symbol, color: seriesColors.all, values: trade.points.map((point) => point[1]) }],
        references, markers, format: (value) => price(value),
      });
    } else {
      byId('trade-chart').replaceChildren(element('p', 'No prices are available for that session yet.', 'muted'));
    }
    const facts = [['Entry', price(trade.entry)], ['Stop', price(trade.stop)], ['Target', trade.target ? price(trade.target) : 'None'],
      ['Exit', trade.exit ? price(trade.exit) : 'Open'], ['Net P&L', trade.exit ? money(trade.net_paise) : '—']];
    byId('trade-facts').replaceChildren(...facts.map(([name, value]) => {
      const item = element('div');
      item.append(element('dt', name), element('dd', value));
      return item;
    }));
  } catch (error) {
    byId('trade-title').textContent = 'Trade';
    byId('trade-summary').textContent = error.message;
  }
}
byId('trade-close').addEventListener('click', () => byId('trade-dialog').close());

function tradeLink(trade) {
  const button = element('button', trade.symbol, 'linklike');
  button.type = 'button';
  button.title = 'Show this trade on a price chart';
  button.addEventListener('click', () => showTrade(trade.trade_id));
  return button;
}

// -- stocks ----------------------------------------------------------------------------------------
function setup(trade) {
  return `${strategies[trade.strategy] || label(trade.strategy)} · ${trade.side === 'long' ? 'Buy' : 'Sell short'}`;
}

function renderStats(id, stats, empty) {
  renderTable(id, stats.map((line) => [
    [strategies[line.strategy] || label(line.strategy)], [line.trades, 'right'], [line.wins, 'right'],
    [`${line.win_rate.toFixed(1)}%`, 'right'], [money(line.average_win_paise), 'right positive'],
    [money(line.average_loss_paise), 'right negative'],
    [line.profit_factor === null ? '—' : line.profit_factor.toFixed(2), 'right'],
    [money(line.net_paise), `right ${tone(line.net_paise)}`],
  ]), 8, empty);
}

function closeButton(trade) {
  const button = element('button', 'Close', 'btn sm');
  button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await post('/api/stock-exit', { trade_id: trade.trade_id });
      byId('manual-message').textContent = `Closed ${trade.symbol}.`;
    } catch (error) {
      byId('manual-message').textContent = error.message;
    }
    refresh();
  });
  return button;
}

function renderStocks(stocks) {
  byId('stocks-status').textContent = stocks ? stocks.status : 'Stock paper trading is off in this session';
  const net = stocks ? stocks.net_paise : null;
  byId('stocks-net').textContent = money(net);
  byId('stocks-net').className = `big-value ${tone(net)}`;
  byId('stocks-capital').textContent = `Capital ${money(stocks ? stocks.capital_paise : null)}`;
  byId('stocks-risk').textContent = `Risk per trade ${money(stocks ? stocks.risk_paise : null)}`;
  byId('stocks-limit').textContent = `Daily loss limit ${money(stocks ? stocks.loss_limit_paise : null)}`;
  const stats = stocks ? stocks.stats : [];
  const all = stats.find((line) => line.strategy === 'all');
  byId('stocks-accuracy').textContent = all ? `${all.win_rate.toFixed(0)}%` : '—';
  byId('stocks-accuracy-note').textContent = all ? `${all.wins} won of ${all.trades} closed trades` : 'No closed paper trades yet';
  byId('stocks-total').textContent = all ? money(all.net_paise) : '—';
  byId('stocks-total').className = `tile-value ${all ? tone(all.net_paise) : ''}`;
  byId('stocks-total-note').textContent = all ? `Net of charges over ${all.trades} trades` : 'Forward test starts with the first trade';
  renderCurve(stocks ? stocks.curve : []);
  renderStats('forward-stats', stats, 'No closed paper trades yet. Results build up as the bot trades each day.');
  const percent = (value) => (value === null || value === undefined ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(2)}%`);
  renderTable('watchlist', (stocks ? stocks.watchlist : []).map((row) => [
    [row.symbol], [row.score.toFixed(0), 'right'], [`${row.atr_pct.toFixed(2)}%`, 'right'],
    [`${row.relative_volume.toFixed(1)}× usual`, 'right'], [percent(row.change_5d), `right ${tone(row.change_5d)}`],
    [percent(row.today_pct), `right ${tone(row.today_pct)}`],
  ]), 6, stocks && stocks.status.startsWith('Preparing') ? stocks.status : 'The watchlist appears once the 14-session history is ready. See Background jobs.');
  const rows = stocks ? [...stocks.open, ...stocks.trades.slice().reverse()] : [];
  renderTable('stock-trades', rows.map((trade) => {
    const open = trade.exit_at === null;
    return [
      [tradeLink(trade), '', `Entered ${localTime(trade.entry_at)} IST`], [setup(trade)], [trade.quantity, 'right'],
      [price(trade.entry), 'right'], [price(trade.stop), 'right'], [price(trade.target), 'right'],
      [price(open ? trade.mark : trade.exit), 'right'],
      [open ? closeButton(trade) : `${label(trade.exit_reason)} ${localTime(trade.exit_at)}`],
      [open ? '—' : money(trade.net_paise), `right ${open ? '' : tone(trade.net_paise) || 'positive'}`],
    ];
  }), 9, stocks ? 'No stock trades today.' : 'Stock paper trading is off in this session.');
}

byId('manual').addEventListener('submit', async (event) => {
  event.preventDefault();
  const side = event.submitter ? event.submitter.value : 'long';
  const symbol = byId('manual-symbol').value.trim().toUpperCase();
  const message = byId('manual-message');
  message.textContent = `Placing paper ${side === 'long' ? 'buy' : 'short sell'} for ${symbol}…`;
  try {
    await post('/api/stock-entry', { symbol, side });
    message.textContent = `Paper ${side === 'long' ? 'buy' : 'short sell'} of ${symbol} is open. It exits at the stop, target or 15:10, or use Close.`;
    byId('manual-symbol').value = '';
  } catch (error) {
    message.textContent = error.message;
  }
  refresh();
});

function moverRow(mover) {
  const side = mover.pct < 0 ? 'negative' : 'positive';
  const row = element('li');
  const name = element('div', undefined, 'mover-name');
  name.append(element('span', mover.symbol));
  if (mover.setup) {
    const chip = element('span', setups[mover.setup], 'pill chip');
    chip.dataset.setup = mover.setup;
    name.append(chip);
  }
  const meta = `${price(mover.ltp)} · VWAP ${price(mover.vwap)} · ₹${indian.format(Math.round(mover.turnover_cr))} cr`;
  const range = element('div', undefined, `range ${side}`);
  range.style.setProperty('--position', mover.range_position);
  range.title = `Day range ${price(mover.low)} to ${price(mover.high)}`;
  const change = `${mover.pct > 0 ? '+' : '−'}${Math.abs(mover.pct).toFixed(2)}%`;
  row.append(name, element('div', meta, 'mover-meta'), range, element('div', change, `mover-change ${side}`));
  return row;
}

function renderMovers(movers) {
  const scanned = movers && movers.at;
  for (const side of ['gainers', 'losers']) {
    const rows = scanned ? movers[side].map(moverRow) : [];
    byId(side).replaceChildren(...(rows.length ? rows : [element('li', scanned ? 'None right now.' : 'No scan yet.', 'muted')]));
  }
  const status = !movers ? 'Scanner off' : movers.state === 'ok' ? `${movers.quoted} stocks · scanned ${localTime(movers.at)} IST` :
    movers.state === 'starting' ? 'Scanner starting' : `${movers.message}${scanned ? ` · Showing ${localTime(movers.at)} IST` : ''}`;
  byId('movers-status').textContent = status;
  byId('movers-status').className = movers && movers.message ? 'negative' : '';
  const total = scanned ? movers.advancers + movers.decliners : 0;
  byId('breadth').textContent = scanned ? `${movers.advancers} ▲ · ${movers.decliners} ▼` : '—';
  byId('breadth-bar').hidden = !total;
  if (total) byId('breadth-bar').style.setProperty('--share', `${(100 * movers.advancers / total).toFixed(1)}%`);
  byId('breadth-note').textContent = scanned ? `Advancers and decliners of ${movers.quoted}` : 'Awaiting stock scan';
}

// -- backtest --------------------------------------------------------------------------------------
let backtestDatesSet = false;
function defaultBacktestDates(now) {
  if (backtestDatesSet) return;
  backtestDatesSet = true;
  const end = new Date(`${now.slice(0, 10)}T00:00:00Z`);
  const start = new Date(end.getTime() - 7 * 86400000);
  byId('backtest-to').value = end.toISOString().slice(0, 10);
  byId('backtest-from').value = start.toISOString().slice(0, 10);
}

byId('backtest-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const run = event.target.querySelector('button');
  const message = byId('backtest-message');
  run.disabled = true;
  message.textContent = 'Fetching candles and replaying. This can take up to a minute…';
  try {
    const result = await post('/api/backtest', {
      symbols: byId('backtest-symbols').value, from: byId('backtest-from').value, to: byId('backtest-to').value,
    });
    byId('backtest-range').textContent = result.sessions ?
      `${result.sessions} sessions · ${ukDate(result.from)} to ${ukDate(result.to)} · ${result.symbols.join(', ')}` : '';
    renderStats('backtest-stats', result.stats, 'No trades were triggered in this range.');
    renderTable('backtest-trades', result.trades.map((trade) => [
      [`${ukDate(trade.entry_at)} ${localTime(trade.entry_at)}`], [trade.symbol], [setup(trade)], [trade.quantity, 'right'],
      [price(trade.entry), 'right'], [price(trade.exit), 'right'], [`${label(trade.exit_reason)} ${localTime(trade.exit_at)}`],
      [money(trade.net_paise), `right ${tone(trade.net_paise) || 'positive'}`],
    ]), 8, 'No trades were triggered in this range.');
    message.textContent = result.sessions ? `Done. ${result.trades.length} trades over ${result.sessions} sessions.` :
      'No market sessions with data in that range.';
  } catch (error) {
    message.textContent = error.message;
  } finally {
    run.disabled = false;
  }
});
renderStats('backtest-stats', [], 'Run a backtest to see accuracy here.');
renderTable('backtest-trades', [], 8, 'Run a backtest to see its trades here.');

// -- background jobs ----------------------------------------------------------------------------------
function renderJobs(events) {
  const latest = new Map();
  for (const event of events) if (!latest.has(event.job)) latest.set(event.job, event);
  const row = (event, first) => [[first], [localTime(event.at)], [event.message, event.error ? 'negative wrap' : 'wrap']];
  renderTable('jobs', [...latest.values()].map((event) => row(event, event.job)), 3, 'No job has reported yet.');
  byId('log-count').textContent = `${events.length} recent entries`;
  renderTable('log', events.map((event) => [[localTime(event.at)], [event.job], [event.message, event.error ? 'negative wrap' : 'wrap']]),
    3, 'Nothing logged yet.');
}

// -- options ---------------------------------------------------------------------------------------
function renderPositions(positions) {
  const items = positions.map((position) => {
    const item = element('div', undefined, 'position-item');
    const row = element('div', undefined, 'contract-row');
    row.append(element('span', position.symbol), element('span', `${position.quantity} units`, 'muted'));
    const stats = element('dl', undefined, 'position-values');
    for (const [name, value] of [['Last premium', position.premium], ['Premium stop', position.premium_stop],
      ['Futures stop', position.underlying_stop]]) {
      const stat = element('div');
      stat.append(element('dt', name), element('dd', price(value)));
      stats.append(stat);
    }
    let description = position.premium_at ? `Mark ${localTime(position.premium_at)} IST` : 'Waiting for an option mark';
    if (position.premium_at && position.premium_stale) description += ' · Stale';
    if (position.exit_reason) description += ` · Exit pending · ${label(position.exit_reason)}`;
    item.append(row, stats, element('p', description, 'muted'));
    return item;
  });
  byId('positions').replaceChildren(...(items.length ? items : [element('p', 'No open position.', 'muted')]));
}

function render(state) {
  token = state.stop_token;
  byId('mode').textContent = state.mode;
  byId('mode').dataset.mode = state.mode;
  byId('clock').dateTime = state.now;
  byId('clock').textContent = `${localTime(state.now)} IST`;
  byId('broker').textContent = brokerStates[state.broker.state] || label(state.broker.state);
  byId('broker').dataset.state = state.broker.state;
  byId('broker').title = state.broker.observed_at ? `Last update ${localTime(state.broker.observed_at)} IST` : '';
  byId('session').textContent = `NIFTY · ${ukDate(state.now)}${state.fixture ? ' · Fixture replay' : ''}`;
  defaultBacktestDates(state.now);
  renderMovers(state.movers);
  renderStocks(state.stocks);
  renderJobs(state.activity || []);

  byId('net').textContent = money(state.net_paise);
  byId('net').className = `big-value ${tone(state.net_paise)}`;
  const reportState = state.reconciliation.status;
  byId('reconciliation').textContent = state.trades.length === 0 ? 'No closed trades' :
    reportState === 'reconciled' ? 'Reconciled to the paisa' : `Reconciliation ${reportState}`;
  byId('reconciliation').className = state.trades.length === 0 ? '' : reportState === 'reconciled' ? 'positive' : 'negative';
  const limit = state.daily_loss_limit;
  const crossed = state.daily_loss_limit_reached;
  byId('limit').textContent = limit === null ? 'Daily loss limit unset' : `Daily loss limit ${money(limit)}${crossed ? ' · Reached' : ''}`;
  byId('limit').className = crossed ? 'negative' : '';
  byId('option-engine').textContent = state.risk_configured ? 'Funded' : 'Unfunded';
  renderPositions(state.positions);
  byId('candle').textContent = state.candle_at ? `1 min candle · ${localTime(state.candle_at)} IST` : 'Awaiting Strategy A candle';
  byId('gates').replaceChildren(...state.gates.map(({ name, status }) => {
    const row = element('li', gateNames[name]);
    const badge = element('span', gateStates[status] || gateStates.not_evaluated, 'pill gate-state');
    badge.dataset.status = status;
    row.append(badge);
    return row;
  }));
  const wins = state.trades.filter((trade) => trade.net_paise > 0).length;
  byId('trade-total').textContent = state.trades.length;
  byId('trade-split').textContent = state.trades.length ? `${wins} won · ${state.trades.length - wins} lost` : 'No closed trades';
  byId('win-rate').textContent = state.trades.length ? `${Math.round(100 * wins / state.trades.length)}%` : '—';
  byId('skip-total').textContent = `${state.skips.length} skipped signal${state.skips.length === 1 ? '' : 's'}`;
  byId('trade-count').textContent = state.trades.length;
  byId('skip-count').textContent = state.skips.length;
  renderTable('trades', state.trades.map((trade) => [
    [localTime(trade.exit_at), '', trade.exit_at], [trade.symbol || '—', '', label(trade.exit_reason)],
    [trade.quantity, 'right'], [money(trade.net_paise), `right ${trade.net_paise < 0 ? 'negative' : 'positive'}`],
  ]), 4, 'No closed trades today.');
  renderTable('skips', state.skips.map((skip) => [[localTime(skip.at), '', skip.at], [label(skip.reason)]]), 2, 'No skipped signals today.');
  byId('delivery').textContent = state.telegram_enabled ? 'Telegram delivery enabled' : 'Local outbox';
  renderTable('alerts', state.alerts.map((alert) => [
    [localTime(alert.at), '', alert.at], [label(alert.kind)],
    [alert.fields.symbol || label(alert.fields.reason)], [label(alert.status), 'right'],
  ]), 4, 'No alerts yet.');
  const halt = state.halt ? `Trading halted · ${label(state.halt)}${state.stop_latched && state.positions.length ? ' · Waiting for the paper exit loop' : ''}` : '';
  showNotice(actionError || halt);
  stop.disabled = submitting || state.stop_latched;
  byId('resume').hidden = !state.resumable;
}

let refreshTimer;
let refreshVersion = 0;
async function refresh() {
  clearTimeout(refreshTimer);
  const version = ++refreshVersion;
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error('Journal unavailable');
    const state = await response.json();
    if (version === refreshVersion) render(state);
  } catch {
    if (version === refreshVersion) {
      showNotice(actionError || 'Journal update unavailable. The figures shown are from the last successful refresh.');
    }
  } finally {
    if (version === refreshVersion) {
      refreshTimer = setTimeout(refresh, refreshDelay);
    }
  }
}

byId('resume').addEventListener('click', async () => {
  if (!token || !window.confirm('Resume paper trading for today? New entries will be allowed again.')) return;
  actionError = '';
  try {
    if (!(await post('/api/resume')).resumed) throw new Error('Nothing to resume');
  } catch {
    actionError = 'Trading could not be resumed. Only a stop pressed on this dashboard can be undone.';
    showNotice(actionError);
  }
  await refresh();
});

stop.addEventListener('click', async () => {
  if (!token || submitting) return;
  if (!window.confirm('Stop all paper trading for the rest of today? This cannot be undone from the dashboard.')) return;
  submitting = true;
  stop.disabled = true;
  actionError = '';
  try {
    if (!(await post('/api/stop')).halted) throw new Error('Stop unconfirmed');
  } catch {
    actionError = 'Stop could not be confirmed. Try the stop control again.';
    showNotice(actionError);
  } finally {
    submitting = false;
    await refresh();
  }
});

refresh();
