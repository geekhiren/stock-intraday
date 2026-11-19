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
const stop = byId('stop');
let token = null;
const refreshDelay = Number(document.querySelector('meta[name="orderbot-refresh-seconds"]').content) * 1000;
let submitting = false;
let actionError = '';

function money(paise) {
  if (paise === null) return '—';
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

function label(value) {
  return value ? value.replaceAll('_', ' ').replace(/^./, (initial) => initial.toUpperCase()) : '—';
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

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

function renderTable(id, rows, columns, empty) {
  const items = rows.map((cells) => {
    const row = element('tr');
    for (const [value, style, title] of cells) {
      const cell = element('td', value, style);
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

function render(state) {
  token = state.stop_token;
  byId('mode').textContent = state.mode;
  byId('mode').dataset.mode = state.mode;
  byId('clock').dateTime = state.now;
  byId('clock').textContent = `${localTime(state.now)} IST`;
  byId('broker').textContent = brokerStates[state.broker.state] || label(state.broker.state);
  byId('broker').dataset.state = state.broker.state;
  byId('broker').title = state.broker.observed_at ? `Last update ${localTime(state.broker.observed_at)} IST` : '';
  byId('session').textContent = `NIFTY · ${state.now.slice(0, 10)}${state.fixture ? ' · Fixture replay' : ''}`;
  byId('net').textContent = money(state.net_paise);
  byId('net').className = `big-value ${state.net_paise > 0 ? 'positive' : state.net_paise < 0 ? 'negative' : ''}`;
  const reportState = state.reconciliation.status;
  byId('reconciliation').textContent = state.trades.length === 0 ? 'No closed trades' :
    reportState === 'reconciled' ? 'Reconciled to the paisa' : `Reconciliation ${reportState}`;
  byId('reconciliation').className = state.trades.length === 0 ? '' : reportState === 'reconciled' ? 'positive' : 'negative';
  const limit = state.daily_loss_limit;
  const crossed = state.daily_loss_limit_reached;
  byId('limit').textContent = limit === null ? 'Daily loss limit unset' : `Daily loss limit ${money(limit)}${crossed ? ' · Reached' : ''}`;
  byId('limit').className = crossed ? 'negative' : '';
  renderPositions(state.positions);
  byId('candle').textContent = state.candle_at ? `1 min candle · ${localTime(state.candle_at)} IST` : 'Awaiting Strategy A candle';
  byId('gates').replaceChildren(...state.gates.map(({ name, status }) => {
    const row = element('li', gateNames[name]);
    const badge = element('span', gateStates[status] || gateStates.not_evaluated, 'gate-state');
    badge.dataset.status = status;
    row.append(badge);
    return row;
  }));
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
  submitting = true;
  stop.disabled = true;
  actionError = '';
  try {
    const response = await fetch('/api/stop', { method: 'POST', headers: { 'X-OrderBOT-Stop': token } });
    if (!response.ok || !(await response.json()).halted) throw new Error('Stop unconfirmed');
  } catch {
    actionError = 'Stop could not be confirmed. Try the stop control again.';
    showNotice(actionError);
  } finally {
    submitting = false;
    await refresh();
  }
});

refresh();
