#!/usr/bin/env node
// Read-only meme-desk watcher. Fetches public prices and posts alerts.
// NEVER signs or sends transactions. The only outbound POST is an alert
// {type, details} to DESK_WEBHOOK_URL, and only when a new incident fires.
// If the webhook secret is missing, alerts are logged and the process exits 0.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const WATCH_PATH = process.env.WATCH_JSON || path.join(ROOT, 'watch.json');
const STATE_PATH = process.env.ALERT_STATE || path.join(ROOT, '.alert-state.json');
const STALE_MS = Number(process.env.STALE_MS || 30 * 60 * 1000);
const UA = 'AkinwoleE-meme-desk-watch/1.0 (read-only; github.com/AkinwoleE/meme-desk-watch)';

function log(...a) { console.log(...a); }
function warn(...a) { console.warn(...a); }

async function getJson(url, { attempts = 3, timeoutMs = 15000 } = {}) {
  let last;
  for (let i = 1; i <= attempts; i++) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const res = await fetch(url, {
        signal: ac.signal,
        headers: { 'User-Agent': UA, Accept: 'application/json' },
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
      return await res.json();
    } catch (e) {
      last = e;
      warn(`fetch attempt ${i}/${attempts} failed: ${e.message || e}`);
      if (i < attempts) await new Promise((r) => setTimeout(r, 500 * i));
    } finally {
      clearTimeout(t);
    }
  }
  throw last;
}

function pickDexPair(pairs, { mint, pairAddress, quoteSymbol, chain } = {}) {
  let list = Array.isArray(pairs) ? pairs.slice() : [];
  if (chain) {
    const c = list.filter((p) => (p.chainId || '').toLowerCase() === chain.toLowerCase());
    if (c.length) list = c;
  }
  if (pairAddress) {
    const hit = list.find((p) => (p.pairAddress || '').toLowerCase() === pairAddress.toLowerCase());
    if (hit) return hit;
  }
  if (mint) {
    const m = mint.toLowerCase();
    const asBase = list.filter((p) => (p.baseToken?.address || '').toLowerCase() === m);
    if (asBase.length) list = asBase;
  }
  if (quoteSymbol) {
    const q = list.filter((p) => (p.quoteToken?.symbol || '').toUpperCase() === quoteSymbol.toUpperCase());
    if (q.length) list = q;
  }
  list.sort((a, b) => ((b.liquidity?.usd || 0) - (a.liquidity?.usd || 0)));
  return list[0] || null;
}

async function fetchDexToken(address) {
  const data = await getJson(`https://api.dexscreener.com/latest/dex/tokens/${address}`);
  return data.pairs || data || [];
}

async function fetchGeckoSolanaPrice(mint) {
  const data = await getJson(`https://api.geckoterminal.com/api/v2/networks/solana/tokens/${encodeURIComponent(mint)}`);
  const a = data?.data?.attributes || {};
  const price = a.price_usd != null ? Number(a.price_usd) : null;
  const mcap = a.market_cap_usd != null ? Number(a.market_cap_usd) : (a.fdv_usd != null ? Number(a.fdv_usd) : null);
  if (price == null && mcap == null) return null;
  return { priceUsd: price, marketCap: mcap, source: 'geckoterminal' };
}

async function gcMcap(gc) {
  const pairs = await fetchDexToken(gc.contract);
  const src = gc.priceSource || {};
  const pair = pickDexPair(pairs, {
    mint: gc.contract,
    pairAddress: src.pairAddress,
    quoteSymbol: src.quoteSymbol || 'META',
    chain: gc.chain || 'robinhood',
  });
  if (!pair) throw new Error('GC/META pool not found on DexScreener');
  const usedPair = (pair.pairAddress || '').toLowerCase();
  const expected = (src.pairAddress || '').toLowerCase();
  if (expected && usedPair !== expected) {
    throw new Error(`GC price pair ${pair.pairAddress} is not the configured GC/META pool ${src.pairAddress}`);
  }
  const mcap = Number(pair.marketCap ?? pair.fdv);
  if (!Number.isFinite(mcap)) throw new Error('GC mcap missing on DexScreener');
  return {
    marketCap: mcap,
    priceUsd: pair.priceUsd != null ? Number(pair.priceUsd) : null,
    pairAddress: pair.pairAddress,
    pool: `${pair.baseToken?.symbol}/${pair.quoteToken?.symbol}`,
    liquidityUsd: pair.liquidity?.usd ?? null,
    url: pair.url,
    source: 'dexscreener',
  };
}

async function positionPrice(pos) {
  try {
    const pairs = await fetchDexToken(pos.mint);
    const pair = pickDexPair(pairs, { mint: pos.mint, chain: pos.chain || 'solana' });
    if (pair?.priceUsd) {
      return {
        priceUsd: Number(pair.priceUsd),
        marketCap: pair.marketCap != null ? Number(pair.marketCap) : null,
        source: 'dexscreener',
        url: pair.url,
        quote: pair.quoteToken?.symbol,
      };
    }
  } catch (e) {
    warn(`DexScreener failed for ${pos.symbol || pos.mint}: ${e.message || e}`);
  }
  const fb = await fetchGeckoSolanaPrice(pos.mint);
  if (fb) return fb;
  throw new Error(`no public price for ${pos.symbol || pos.mint}`);
}

function ageMs(iso) {
  if (!iso) return Infinity;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return Infinity;
  return Date.now() - t;
}

function fmtAge(ms) {
  if (!Number.isFinite(ms)) return 'unknown';
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 120) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 120) return `${m}m`;
  return `${(m / 60).toFixed(1)}h`;
}

function loadWatch() {
  if (!fs.existsSync(WATCH_PATH)) {
    throw new Error(`watch.json missing at ${WATCH_PATH}`);
  }
  return JSON.parse(fs.readFileSync(WATCH_PATH, 'utf8'));
}

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
  } catch {
    return { version: 1, incidents: {} };
  }
}

function saveState(state) {
  const tmp = `${STATE_PATH}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`);
  fs.renameSync(tmp, STATE_PATH);
}

function parseAuthHeader(spec, key) {
  const raw = (spec || 'Authorization: Bearer').trim();
  const idx = raw.indexOf(':');
  if (idx === -1) return { name: raw, value: key };
  const name = raw.slice(0, idx).trim();
  const prefix = raw.slice(idx + 1).trim();
  return { name, value: prefix ? `${prefix} ${key}` : key };
}

async function postAlert(alert) {
  const url = process.env.DESK_WEBHOOK_URL || '';
  const key = process.env.DESK_WEBHOOK_KEY || '';
  const body = JSON.stringify({ type: alert.type, details: alert.details });
  if (!url || !key) {
    log(`webhook secrets missing; logging alert only: ${body}`);
    return { sent: false, reason: 'missing_secret' };
  }
  const { name, value } = parseAuthHeader(process.env.DESK_WEBHOOK_HEADER, key);
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 15000);
  try {
    const res = await fetch(url, {
      method: 'POST',
      signal: ac.signal,
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': UA,
        [name]: value,
      },
      body,
    });
    if (!res.ok) throw new Error(`webhook HTTP ${res.status}`);
    log(`posted alert ${alert.type} (${res.status})`);
    return { sent: true };
  } finally {
    clearTimeout(t);
  }
}

async function evaluate(watch) {
  const alerts = [];
  const notes = [];

  // --- GC dump trigger ---
  const gc = watch.gc || {};
  const ladderUnderCap =
    (Number.isFinite(gc.remaining) && gc.remaining > 0) ||
    (Number.isFinite(gc.totalSpent) && gc.totalSpent < (gc.ladderCap ?? 100));
  let gcLive = null;
  try {
    gcLive = await gcMcap(gc);
    const triggered = gcLive.marketCap <= gc.nextTriggerMcap && ladderUnderCap;
    notes.push(
      `GC: mcap=$${Math.round(gcLive.marketCap).toLocaleString('en-US')} ` +
      `(${gcLive.pool} ${gcLive.source}) trigger=$${Number(gc.nextTriggerMcap).toLocaleString('en-US')} ` +
      `spent=$${gc.totalSpent} remaining=$${gc.remaining} => ${triggered ? 'TRIGGER' : 'no trigger'}`
    );
    if (triggered) {
      alerts.push({
        type: 'gc_trigger',
        key: `gc:${gc.nextTriggerMcap}:${Math.round(gcLive.marketCap)}`,
        // Key includes trigger level so a later buy (new NEXT_TRIGGER) can fire again.
        // Mcap is bucketed only in the incident identity via the trigger, not the live print:
        // use trigger + "below" so we don't re-alert on every tick as mcap wiggles under the line.
        stableKey: `gc:${gc.nextTriggerMcap}:below`,
        details: {
          contract: gc.contract,
          chain: gc.chain,
          pool: gcLive.pool,
          pairAddress: gcLive.pairAddress,
          marketCap: gcLive.marketCap,
          nextTriggerMcap: gc.nextTriggerMcap,
          lastBuyMcap: gc.lastBuyMcap,
          totalSpent: gc.totalSpent,
          remaining: gc.remaining,
          ladderCap: gc.ladderCap ?? 100,
          url: gcLive.url,
        },
      });
    }
  } catch (e) {
    warn(`GC price fetch failed (no gc_trigger this run): ${e.message || e}`);
    notes.push(`GC: price unavailable (${e.message || e})`);
  }

  // --- Missed stops ---
  for (const pos of watch.openPositions || []) {
    try {
      const live = await positionPrice(pos);
      const entry = Number(pos.entryPrice);
      const stop = Number(pos.stopMultiple);
      if (!Number.isFinite(entry) || entry <= 0 || !Number.isFinite(stop)) {
        notes.push(`#${pos.id} ${pos.symbol}: bad entry/stop in watch.json; skipped`);
        continue;
      }
      const multiple = live.priceUsd / entry;
      const below = multiple <= stop;
      notes.push(
        `#${pos.id} ${pos.symbol}: live=${live.priceUsd} entry=${entry} ` +
        `multiple=${multiple.toFixed(4)}x stop=${stop}x (${live.source}) => ${below ? 'BELOW STOP' : 'ok'}`
      );
      if (below) {
        alerts.push({
          type: 'missed_stop',
          stableKey: `missed_stop:${pos.id}:${stop}`,
          details: {
            id: pos.id,
            symbol: pos.symbol,
            mint: pos.mint,
            profile: pos.profile,
            entryPrice: pos.entryPrice,
            entryMcap: pos.entryMcap,
            stopMultiple: stop,
            livePrice: live.priceUsd,
            liveMcap: live.marketCap ?? null,
            multiple,
            source: live.source,
            url: live.url || null,
          },
        });
      }
    } catch (e) {
      warn(`price failed for #${pos.id} ${pos.symbol || pos.mint} (no missed_stop this run): ${e.message || e}`);
      notes.push(`#${pos.id} ${pos.symbol || pos.mint}: price unavailable (${e.message || e})`);
    }
  }

  // --- Unreported sell ---
  const sellAt = watch.latestJournalSellAt;
  const marker = watch.lastReportedExitAt;
  const sellTs = sellAt ? Date.parse(sellAt) : NaN;
  const markerTs = marker ? Date.parse(marker) : NaN;
  const unreported = Number.isFinite(sellTs) && (!Number.isFinite(markerTs) || sellTs > markerTs);
  notes.push(
    `sells: latestJournalSellAt=${sellAt || 'none'} lastReportedExitAt=${marker || 'none'} ` +
    `=> ${unreported ? 'UNREPORTED SELL' : 'no unreported sell'}`
  );
  if (unreported) {
    alerts.push({
      type: 'unreported_sell',
      stableKey: `unreported_sell:${sellAt}`,
      details: {
        latestJournalSellAt: sellAt,
        lastReportedExitAt: marker,
      },
    });
  }

  // --- Stale heartbeat ---
  const hbAge = ageMs(watch.monitorHeartbeatAt);
  const stale = hbAge > STALE_MS;
  notes.push(
    `heartbeat: monitorHeartbeatAt=${watch.monitorHeartbeatAt || 'none'} ` +
    `exportedAt=${watch.exportedAt || 'none'} age=${fmtAge(hbAge)} threshold=${fmtAge(STALE_MS)} ` +
    `=> ${stale ? 'STALE' : 'ok'}`
  );
  if (stale) {
    alerts.push({
      type: 'stale_heartbeat',
      // Identity is the frozen heartbeat timestamp: one alert per incident.
      // When the box resumes and the heartbeat advances, the incident clears and a later freeze can fire again.
      stableKey: `stale_heartbeat:${watch.monitorHeartbeatAt || 'missing'}`,
      details: {
        monitorHeartbeatAt: watch.monitorHeartbeatAt || null,
        exportedAt: watch.exportedAt || null,
        ageMs: Number.isFinite(hbAge) ? hbAge : null,
        thresholdMs: STALE_MS,
      },
    });
  }

  return { alerts, notes };
}

function incidentsEqual(a, b) {
  return JSON.stringify(a || {}) === JSON.stringify(b || {});
}

function reconcile(state, alerts) {
  const incidents = { ...(state.incidents || {}) };
  const now = new Date().toISOString();
  const activeKeys = new Set();
  const fresh = [];
  for (const a of alerts) {
    // One slot per type, except missed_stop which is per position.
    const slot = a.type === 'missed_stop' ? `missed_stop:${a.details.id}` : a.type;
    activeKeys.add(slot);
    const prev = incidents[slot];
    if (prev && prev.key === a.stableKey) {
      // Same incident already alerted — do not flap.
      continue;
    }
    incidents[slot] = { key: a.stableKey, alertedAt: now, type: a.type };
    fresh.push(a);
  }
  // Clear incidents that have recovered so a future occurrence can alert again.
  for (const slot of Object.keys(incidents)) {
    if (!activeKeys.has(slot)) delete incidents[slot];
  }
  const changed = !incidentsEqual(incidents, state.incidents);
  const next = changed
    ? { version: 1, incidents, updatedAt: now }
    : { version: state.version || 1, incidents: state.incidents || {}, updatedAt: state.updatedAt };
  return { state: next, fresh, changed };
}

async function main() {
  log('desk-watch starting (read-only; no trading)');
  let watch;
  try {
    watch = loadWatch();
  } catch (e) {
    warn(`cannot load watch.json: ${e.message || e}`);
    // Treat a missing/broken snapshot as a stale-heartbeat style incident so we get woken once.
    const alerts = [{
      type: 'stale_heartbeat',
      stableKey: `stale_heartbeat:missing-watch:${e.message || e}`,
      details: { error: String(e.message || e), watchPath: WATCH_PATH },
    }];
    const prev = loadState();
    const { state, fresh, changed } = reconcile(prev, alerts);
    for (const n of [`watch.json: FAILED (${e.message || e})`]) log(n);
    for (const a of fresh) {
      try { await postAlert(a); } catch (err) { warn(`webhook post failed: ${err.message || err}`); }
    }
    if (changed) saveState(state);
    log('done (watch.json unreadable; exited 0)');
    return;
  }

  log(`watch.json exportedAt=${watch.exportedAt} open=${(watch.openPositions || []).length}`);
  const { alerts, notes } = await evaluate(watch);
  for (const n of notes) log(n);

  const prev = loadState();
  const { state, fresh, changed } = reconcile(prev, alerts);
  if (alerts.length === 0) {
    log('alerts: none');
  } else {
    log(`alerts raised: ${alerts.map((a) => a.type).join(', ')} (${fresh.length} new, ${alerts.length - fresh.length} already notified)`);
  }
  for (const a of fresh) {
    try {
      await postAlert(a);
    } catch (err) {
      warn(`webhook post failed for ${a.type}: ${err.message || err}`);
      // Incident is still recorded so we don't flap; the failure is in the logs.
    }
  }
  if (changed) saveState(state);
  else log('alert-state unchanged');
  log('done (exit 0)');
}

main().catch((e) => {
  warn('fatal:', e);
  // Don't fail the workflow on unexpected errors — a red X every 10 min is noisy.
  // The missing watch.json path already alerts; a genuine crash is in the logs.
  process.exit(0);
});
