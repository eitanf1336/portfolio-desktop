#!/usr/bin/env python3
"""datafeed.py — live market data + portfolio math (shekel base).

Two kinds of holding (see ~/.config/portfolio/holdings.json):

  * "equity"  — a real Yahoo ticker priced in its native currency (e.g. GOOG,
    NASA in USD). Shekel value = shares × price × live USD/ILS.

  * "fund"    — an Israeli Nasdaq-100 tracker not on Yahoo. It rides a live
    proxy index (^NDX): a ₪-hedged fund tracks NDX directly, an unhedged one
    tracks NDX × USD/ILS. value_ils is anchored to ref_proxy/ref_fx captured
    when the holding was recorded, then scaled by the live proxy move.

Everything is reported in the base currency (ILS). Prices per holding are kept
in their native currency (priceCur) so a $ row never looks like a ₪ row.
Pure stdlib; imported by portfolio.py and run off the GTK main thread.
"""
import os, json, time, urllib.request, concurrent.futures

CONFIG_PATH = os.path.expanduser('~/.config/portfolio/holdings.json')
HOSTS = ('https://query1.finance.yahoo.com', 'https://query2.finance.yahoo.com')
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) portfolio-widget/1.0'}
FX_SYMBOL = 'USDILS=X'

RANGES = {
    '1D': ('1d',  '5m'),
    '1W': ('5d',  '15m'),
    '1M': ('1mo', '1d'),
    '1Y': ('1y',  '1d'),
    'MAX': ('max', '1mo'),
}
CUR_SYM = {'USD': '$', 'ILS': '₪', 'EUR': '€', 'GBP': '£'}


# --------------------------------------------------------------------------- io
def _get(path):
    last = None
    for host in HOSTS:
        try:
            req = urllib.request.Request(host + path, headers=UA)
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault('base_currency', 'ILS')
    cfg.setdefault('cash', 0)
    cfg.setdefault('income', [])
    cfg.setdefault('holdings', [])
    return cfg


def fetch_chart(symbol, rng_key='1D'):
    """{t:[epochs], c:[closes], meta:{...}} for a range key.

    A 1D series opens on yesterday's close. Everything that reads a day move
    measures from that close, but Yahoo's intraday series starts at today's
    OPEN, which leaves the reference point off the chart entirely: a stock that
    gapped up overnight and then slid all day drew a falling line while the
    label beside it correctly read +0.64%. Anchoring here means a line that ends
    above where it started is exactly a line that is up on the day.

    Longer ranges need no anchor: their first bar already IS the reference.
    """
    yr, yi = RANGES.get(rng_key, RANGES['1D'])
    d = _get(f'/v8/finance/chart/{symbol}?range={yr}&interval={yi}&includePrePost=false')
    res = d['chart']['result'][0]
    meta = res.get('meta', {})
    ts = res.get('timestamp') or []
    quote = (res.get('indicators', {}).get('quote') or [{}])[0]
    closes = quote.get('close') or []
    t, c, last = [], [], None
    for i, ci in enumerate(closes):
        if ci is None:
            ci = last
        if ci is None:
            continue
        last = ci
        t.append(ts[i] if i < len(ts) else None)
        c.append(round(ci, 4))

    if rng_key == '1D' and c:
        prev = meta.get('chartPreviousClose') or meta.get('previousClose')
        if prev is not None:
            step = (t[1] - t[0]) if len(t) > 1 and t[0] and t[1] else 300
            c.insert(0, round(prev, 4))
            t.insert(0, t[0] - step if t[0] else None)

    return {'t': t, 'c': c, 'meta': meta}


def _quote(symbol):
    """Live snapshot for one symbol via the 1d/5m chart."""
    ch = fetch_chart(symbol, '1D')
    m = ch['meta']
    price = m.get('regularMarketPrice')
    if price is None and ch['c']:
        price = ch['c'][-1]
    # fetch_chart already opens the 1D series on yesterday's close, so spark
    # starts at the very point dayChange is measured from.
    return {
        'price': price,
        'prevClose': m.get('chartPreviousClose') or m.get('previousClose') or price,
        'name': m.get('longName') or m.get('shortName') or symbol,
        'currency': m.get('currency', 'USD'),
        'dayHigh': m.get('regularMarketDayHigh'),
        'dayLow': m.get('regularMarketDayLow'),
        'week52High': m.get('fiftyTwoWeekHigh'),
        'week52Low': m.get('fiftyTwoWeekLow'),
        'volume': m.get('regularMarketVolume'),
        'exchange': m.get('fullExchangeName'),
        'spark': ch['c'],
        'sparkT': ch['t'],
    }


# ----------------------------------------------------------------- full snapshot
def build_snapshot():
    cfg = load_config()
    base = cfg['base_currency']
    base_sym = CUR_SYM.get(base, base + ' ')

    # collect every symbol we must fetch (equity tickers, fund proxies, FX)
    symbols = {FX_SYMBOL}
    for h in cfg['holdings']:
        if h.get('kind') == 'equity' and h.get('ticker'):
            symbols.add(h['ticker'])
        elif h.get('kind') == 'fund' and h.get('proxy'):
            symbols.add(h['proxy'])

    quotes = {}

    def fetch(sym):
        try:
            return sym, _quote(sym)
        except Exception as e:  # noqa: BLE001
            return sym, {'error': str(e)[:120], 'price': None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for sym, q in ex.map(fetch, symbols):
            quotes[sym] = q

    fxq = quotes.get(FX_SYMBOL, {})
    fx_live = fxq.get('price') or 1.0
    fx_prev = fxq.get('prevClose') or fx_live

    rows = []
    for h in cfg['holdings']:
        try:
            if h.get('kind') == 'equity':
                rows.append(_row_equity(h, quotes, fx_live, base_sym))
            elif h.get('kind') == 'fund':
                rows.append(_row_fund(h, quotes, fx_live, fx_prev, base_sym))
        except Exception as e:  # noqa: BLE001
            rows.append({'ticker': h.get('ticker') or h.get('id') or h.get('name', '?'),
                         'name': h.get('name', ''), 'ok': False, 'error': str(e)[:120],
                         'price': None, 'spark': [], 'priceCur': base_sym})

    invested = sum(r['marketValue'] for r in rows if r.get('ok'))
    for r in rows:
        r['weight'] = (r['marketValue'] / invested * 100) if invested else 0
    rows.sort(key=lambda r: r.get('marketValue', 0), reverse=True)

    # portfolio intraday curve = elementwise sum of each holding's ₪ spark
    sparks = [r['sparkIls'] for r in rows if r.get('ok') and r.get('sparkIls')]
    intraday_t, intraday_v = [], []
    if sparks:
        n = min(len(s) for s in sparks)
        base_t = next((r['sparkT'] for r in rows if r.get('ok') and r.get('sparkT')), [])
        cash = cfg['cash']
        for i in range(n):
            intraday_v.append(round(sum(s[i] for s in sparks) + cash, 2))
        intraday_t = base_t[:n]

    for r in rows:                       # trim payload
        r.pop('sparkT', None)
        r.pop('sparkIls', None)
        # Keep the whole session: a day is ~80 five-minute bars, and the old
        # [-60:] cap dropped the first ~1.5h, including yesterday's close: the
        # very point the line's colour is measured from.
        r['spark'] = r.get('spark', [])

    cash = cfg['cash']
    mv = invested + cash
    cost_basis = sum(r['costBasis'] for r in rows if r.get('ok'))
    day_change = sum(r['dayChange'] for r in rows if r.get('ok'))
    prev_total = mv - day_change
    total_return = invested - cost_basis
    income_total = sum(float(e.get('amount', 0) or 0) for e in cfg.get('income', []))

    return {
        'asOf': time.time(),
        'baseCurrency': base,
        'cash': cash,
        'cashLabel': cfg.get('cash_label', 'Cash'),
        'cashDisplay': cfg.get('cash_display'),   # band string (e.g. "< ₪10k"); hides the exact figure
        'fx': {'USDILS': fx_live},
        'income': {'total': income_total, 'entries': cfg.get('income', [])},
        'totals': {
            'marketValue': mv,
            'investedValue': invested,
            'costBasis': cost_basis,
            'dayChange': day_change,
            'dayChangePct': (day_change / prev_total * 100) if prev_total else 0,
            'totalReturn': total_return,
            'totalReturnPct': (total_return / cost_basis * 100) if cost_basis else 0,
        },
        'intraday': {'t': intraday_t, 'v': intraday_v},
        'holdings': rows,
    }


def _row_equity(h, quotes, fx_live, base_sym):
    q = quotes.get(h['ticker'], {})
    price = q.get('price')
    if price is None:
        raise RuntimeError(q.get('error') or 'no price')
    prev = q.get('prevClose') or price
    shares = float(h.get('shares', 0))
    native = h.get('price_currency', q.get('currency', 'USD'))
    rate = fx_live if native == 'USD' else 1.0
    value = shares * price * rate
    # Return is measured in the asset's OWN currency (pure performance), so a
    # USD stock that rose 20% reads +20% even if USD/ILS drifted. We re-base the
    # cost at the live FX: cost_ils = avg_cost(native) × shares × rate. That makes
    # the shekel return identical to the native return. avg_cost preferred; fall
    # back to a historical ₪ cost if that's all we have.
    avg = float(h.get('avg_cost', 0) or 0)
    if avg:
        cost_ils = avg * shares * rate
        ret_pct = (price / avg - 1) * 100
    else:
        cost_ils = float(h.get('cost_ils', 0))
        ret_pct = ((value / cost_ils - 1) * 100) if cost_ils else 0
    spark_ils = [c * shares * rate for c in q.get('spark', [])]
    return {
        'ticker': h['ticker'], 'name': h.get('name') or q.get('name'),
        'kind': 'equity', 'ok': True, 'error': None,
        'price': price, 'prevClose': prev, 'priceCur': CUR_SYM.get(native, native),
        'avgCost': avg, 'avgCostCur': CUR_SYM.get(native, native),
        'chartSymbol': h['ticker'], 'exchange': q.get('exchange'),
        'shares': shares, 'costBasis': cost_ils, 'marketValue': value,
        'dayChange': (price - prev) * shares * rate,
        'dayChangePct': ((price / prev - 1) * 100) if prev else 0,
        'totalReturn': value - cost_ils,
        'totalReturnPct': ret_pct,
        'dayHigh': q.get('dayHigh'), 'dayLow': q.get('dayLow'),
        'week52High': q.get('week52High'), 'week52Low': q.get('week52Low'),
        'volume': q.get('volume'),
        'spark': spark_ils, 'sparkIls': spark_ils, 'sparkT': q.get('sparkT'),
    }


def _row_fund(h, quotes, fx_live, fx_prev, base_sym):
    q = quotes.get(h['proxy'], {})
    p_live = q.get('price')
    if p_live is None:
        raise RuntimeError(q.get('error') or 'no proxy price')
    p_prev = q.get('prevClose') or p_live
    hedged = bool(h.get('hedged'))
    ref_proxy = float(h['ref_proxy'])
    ref_fx = float(h.get('ref_fx', 1))
    base_val = float(h['value_ils'])
    cost_ils = float(h.get('cost_ils', 0))
    shares = float(h.get('shares', 0))

    if hedged:
        idx_live, idx_prev, ref_index = p_live, p_prev, ref_proxy
        fx_factor = 1.0
    else:
        idx_live, idx_prev = p_live * fx_live, p_prev * fx_prev
        ref_index = ref_proxy * ref_fx
        fx_factor = fx_live
    value = base_val * idx_live / ref_index
    value_prev = base_val * idx_prev / ref_index
    spark_ils = [base_val * ((c * (1 if hedged else fx_factor)) / ref_index)
                 for c in q.get('spark', [])]
    unit = (value / shares) if shares else value
    return {
        'ticker': h.get('id') or h.get('name'), 'name': h.get('name'),
        'kind': 'fund', 'ok': True, 'error': None,
        'price': unit, 'prevClose': (value_prev / shares) if shares else value_prev,
        'avgCost': (cost_ils / shares) if shares else 0, 'avgCostCur': base_sym,
        'priceCur': base_sym, 'chartSymbol': h['proxy'], 'exchange': 'TASE · tracks NDX',
        'shares': shares, 'costBasis': cost_ils, 'marketValue': value,
        'dayChange': value - value_prev,
        'dayChangePct': ((idx_live / idx_prev - 1) * 100) if idx_prev else 0,
        'totalReturn': value - cost_ils,
        'totalReturnPct': ((value / cost_ils - 1) * 100) if cost_ils else 0,
        'dayHigh': None, 'dayLow': None, 'week52High': None, 'week52Low': None,
        'volume': None,
        'spark': spark_ils, 'sparkIls': spark_ils, 'sparkT': q.get('sparkT'),
    }


if __name__ == '__main__':
    snap = build_snapshot()
    t = snap['totals']
    s = CUR_SYM.get(snap['baseCurrency'], '')
    print(f"Portfolio: {s}{t['marketValue']:,.2f}  "
          f"(invested {s}{t['investedValue']:,.2f} + cash {s}{snap['cash']:,.0f})")
    print(f"Day: {t['dayChange']:+,.2f} ({t['dayChangePct']:+.2f}%)   "
          f"Total: {t['totalReturn']:+,.2f} ({t['totalReturnPct']:+.2f}%)   "
          f"FX USDILS={snap['fx']['USDILS']:.4f}")
    for r in snap['holdings']:
        print(f"  {r['ticker']:>10} {r.get('priceCur','')}{r.get('price') or 0:>9,.2f}  "
              f"day {r['dayChangePct']:+6.2f}%  ret {r['totalReturnPct']:+7.2f}%  "
              f"{s}{r['marketValue']:>12,.2f}  ({r['weight']:.1f}%)  "
              f"{'' if r['ok'] else r['error']}")
