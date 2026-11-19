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
      [trade.symbol, '', `Entered ${localTime(trade.entry_at)} IST`], [setup(trade)], [trade.quantity, 'right'],
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
