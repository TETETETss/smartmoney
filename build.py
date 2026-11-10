"""Smart Money (copy) build: fetch the wallets' Polymarket positions and PnL, inject into template.html -> index.html.
Usage: python3 build.py   (needs wallets.json next to it; writes data.json and index.html)"""
import json, time, urllib.request, urllib.parse, re, os, sys, threading

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_VALUE = float(os.environ.get('MIN_VALUE', 10))      # ignore positions worth less than this ($)
MAX_PRICE = 0.98     # hide markets where the leading side already trades above 98c
MIN_TRADERS = int(os.environ.get('MIN_TRADERS', 2))
SOLO_MIN = float(os.environ.get('SOLO_MIN', 0))   # 0 = only markets held by MIN_TRADERS+ traders; set e.g. 250 to also show big single-trader bets

import urllib.error
def get(url, tries=3, quiet=False):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode('utf-8', 'replace')
            if e.code < 500 and e.code != 429:   # not worth retrying
                if not quiet: print('  failed', url[:80], e.code, body)
                return None
            err = f'{e.code} {body}'
        except Exception as e:
            err = str(e)
        time.sleep(1.5 * (i + 1))
    if not quiet: print('  failed', url[:80], err)
    return None

DEBUG = {}
_DBG_LOCK = threading.Lock()
def dbg(k, v):
    with _DBG_LOCK:
        if k not in DEBUG: DEBUG[k] = v
        json.dump(DEBUG, open(os.path.join(HERE, 'debug.json'), 'w', encoding='utf-8'), indent=1, default=str)

V2 = 'https://data-api.polymarket.com/v2/'
def try_get(route, params):
    """try a few names for the wallet parameter; return first non-error JSON"""
    a = params.pop('_addr')
    for key in ('user', 'address', 'wallet', 'proxy_wallet'):
        q = dict(params); q[key] = a
        url = V2 + route + '?' + urllib.parse.urlencode(q)
        d = get(url, quiet=True)
        if d is not None:
            dbg(route + ' url', url); return d, key
        dbg(route + ' fail ' + key, url)
    return None, None

def rows_of(d):
    if isinstance(d, list): return d, None
    if isinstance(d, dict):
        r = d.get('data')
        if isinstance(r, dict): r = r.get('positions') or r.get('items') or r.get('data') or []
        return (r or []), (d.get('pagination') or {}).get('next_cursor')
    return [], None

def positions(addr):
    out, cur = [], None
    for _ in range(60):
        prm = {'_addr': addr, 'limit': 100}
        if cur: prm['cursor'] = cur
        d, key = try_get('positions', prm)
        if d is None: break
        dbg('positions sample', d if not isinstance(d, dict) else {k: (v[:2] if isinstance(v, list) else v) for k, v in d.items()})
        rows, cur = rows_of(d); out += rows
        if not cur or not rows: break
    print('  positions:', len(out)); return out

def pnl_series(addr):
    for prm in ({'interval': 'all', 'fidelity': '1d'}, {'interval': 'max', 'fidelity': '1d'}, {}):
        d, key = try_get('user-pnl', dict(prm, _addr=addr))
        if d is None: continue
        dbg('pnl sample', d if isinstance(d, dict) and not isinstance(d.get('data'), list) else (d[:3] if isinstance(d, list) else {**d, 'data': d['data'][:3]}))
        rows = d if isinstance(d, list) else (d.get('data') or [])
        if isinstance(rows, dict): rows = rows.get('series') or rows.get('points') or []
        pts = []
        for x in rows:
            t = x.get('t', x.get('timestamp', x.get('time'))); v = F(x,'p','economic_pnl','position_pnl','pnl','value')
            if t is None or v is None: continue
            if isinstance(t, str):
                import datetime; t = datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp()
            pts.append((float(t) / (1000 if float(t) > 1e11 else 1), float(v)))
        if pts: return sorted(pts)
    print('  pnl: no data'); return []

def F(p, *names, default=None):
    for n in names:
        if n in p and p[n] is not None: return p[n]
    return default

def name_of(addr, given):
    if given: return given
    p = get('https://gamma-api.polymarket.com/public-profile?address=' + addr, quiet=True) or {}
    return p.get('name') or p.get('pseudonym') or addr[:6] + '…' + addr[-4:]

CATS = [('Mention', r'\b(say|says|said|mention|mentions|mentioned|tweet|post)\b.*["\u201c]|\bmention'),
        ('Pop culture', r'(movie|film|oscar|academy award|emmy|grammy|golden globe|james bond|halftime|super bowl|big game|album|song|spotify|netflix|box office|top grossing|actor|actress|best picture|searched|time person of the year|pregnant|marvel|avengers|spider-man| as [a-z]+ [a-z]+\?$|taylor swift|drake|kardashian|celebrity)'),
        ('Sports', r'\b(nba|nfl|nhl|mlb|ufc|premier league|champions league|la liga|serie a|bundesliga|world cup|grand slam|f1|grand prix|league of legends|worlds 20\d\d|esports|vs\.?)\b'),
        ('Crypto', r'\b(bitcoin|btc|ethereum|eth|solana|xrp|crypto|doge|token|airdrop|predict\.fun)\b'),
        ('Iran/Israel', r'(iran|israel|gaza|hamas|hezbollah|lebanon|netanyahu|khamenei|hormuz|knesset)'),
        ('US politics', r'\b(trump|senate|house|governor|midterm|democrats?|republicans?|mayor|white house|press secretary|congress|epstein|vance|harris|newsom|mamdani|supreme court|cabinet)\b'),
        ('World', r'\b(putin|russia|ukraine|china|xi|takaichi|kim jong|brazil|bolsonaro|greece|prime minister|president of|election|ceasefire|nato|venezuela)\b'),
        ('Tech & economy', r'\b(openai|anthropic|ai model|ipo|fed|rate|inflation|recession|stock|market cap|oil|diesel|tariff|nvidia|apple|meta|google)\b')]
def cat_of(title):
    t = title.lower()
    if '2028' in t and ('president' in t or 'nomination' in t): return '2028'
    for c, rx in CATS:
        if re.search(rx, t): return c
    return 'Other'

RAW = {}
def load_wallets(fname):
    path = os.path.join(HERE, fname)
    if not os.path.exists(path): return []
    seen, W = set(), []
    for w in json.load(open(path, encoding='utf-8')):
        a = w['address'].lower()
        if a in seen: continue
        item = {'address': a, 'name': w.get('name')}
        if w.get('min') is not None: item['min'] = float(w['min'])
        seen.add(a); W.append(item)
    return W


# ---------- chart data: YES price history + the tracked traders' trades in each shown market ----------
import concurrent.futures as cf
def _price_history(tok, side):
    if not tok: return []
    for fid in (60, 240, 1440):
        d = get(f'https://clob.polymarket.com/prices-history?market={tok}&interval=max&fidelity={fid}', quiet=True)
        h = (d or {}).get('history') if isinstance(d, dict) else None
        if h:
            dbg('prices sample', {'history': h[:3]})
            pts = [(int(x['t']), float(x['p'])) for x in h if 't' in x and 'p' in x]
            if side == 'No': pts = [(t, 1 - p) for t, p in pts]
            step = max(1, len(pts) // 300); out = pts[::step]
            if out and out[-1] != pts[-1]: out.append(pts[-1])
            return [[t, round(p, 4)] for t, p in out]
    return []

def _trades(addr, conds):
    rows = []
    for i in range(0, len(conds), 20):
        batch = conds[i:i+20]; cur = None
        for _ in range(10):
            q = {'user': addr, 'condition': ','.join(batch), 'taker_only': 'false', 'limit': 1000}
            if cur: q['cursor'] = cur
            d = get(V2 + 'trades?' + urllib.parse.urlencode(q), quiet=True)
            if d is None: break
            r, cur = rows_of(d)
            if r: dbg('trades sample', r[:2])
            rows += r
            if not cur or not r: break
    want = set(conds)
    return [x for x in rows if F(x, 'condition_id', 'conditionId') in want]

def charts(D, W, T):
    print('\n== charts: price history + trades ==')
    name2addr = {t['n']: t['a'] for t in T}
    held = {}
    for m in D:
        for t in m['tr']:
            a = name2addr.get(t['n'])
            if a: held.setdefault(a, set()).add(m['cid'])
    with cf.ThreadPoolExecutor(8) as ex:
        ph = dict(zip([m['url'] for m in D], ex.map(lambda m: _price_history(*(m['tok'] or (None, None))), D)))
        tr = dict(zip(list(held), ex.map(lambda a: _trades(a, sorted(held[a])), list(held))))
    addr2name = {a: n for n, a in name2addr.items()}
    by_cid = {}
    for a, rows in tr.items():
        for x in rows:
            try:
                o = F(x, 'outcome'); ix = F(x, 'outcome_index', 'outcomeIndex')
                o = o if o in ('Yes', 'No') else ('Yes' if str(ix) == '0' else 'No')
                px, sz = float(F(x, 'price', default=0)), float(F(x, 'size', default=0))
                ts = int(float(F(x, 'timestamp', default=0)))
                if ts > 1e11: ts //= 1000
                by_cid.setdefault(F(x, 'condition_id', 'conditionId'), []).append(
                    [ts, addr2name.get(a, a[:8]), 'B' if str(F(x, 'side')).upper() == 'BUY' else 'S', o[0], round(px, 4), round(sz, 2)])
            except Exception:
                continue
    CH = {}
    for m in D:
        p = ph.get(m['url']) or []; t = sorted(by_cid.get(m['cid'], []))
        if p or t: CH[m['url']] = {'p': p, 't': t}
    print(f'charts: {sum(1 for v in CH.values() if v["p"])} price histories, {sum(len(v["t"]) for v in CH.values())} trades')
    return CH

def prefetch_positions(addrs):
    need = [a for a in addrs if a not in RAW]
    if not need: return
    print(f'  fetching positions for {len(need)} wallets')
    with cf.ThreadPoolExecutor(8) as ex:
        got = list(ex.map(positions, need))
    for a, P in zip(need, got):
        RAW[a] = P or []

def build_group(W, min_value, label):
    print(f'\n== {label}: {len(W)} traders ==')
    prefetch_positions([w['address'] for w in W])
    markets, traders = {}, []
    with cf.ThreadPoolExecutor(8) as ex:
        pnl_by = dict(zip([w['address'] for w in W], ex.map(lambda w: pnl_series(w['address']), W)))
    for w in W:
        a = w['address']
        P = RAW[a] if a in RAW else positions(a)
        RAW[a] = P
        n = w['name'] or next((F(p,'name') for p in P if F(p,'name')), None) or name_of(a, None); print('-', n)
        # markets where this trader holds BOTH sides (hedged): leave the trader out of that market
        sides = {}
        for p in P:
            if F(p,'redeemable') in (True, 'True', 'true') or float(F(p,'current_size','size',default=0) or 0) <= 0: continue
            o = F(p,'outcome'); ix = F(p,'outcomeIndex','outcome_index')
            sides.setdefault(F(p,'conditionId','condition_id','asset','token_id'), set()).add(o if o in ('Yes','No') else str(ix))
        both = {c for c, v in sides.items() if len(v) > 1}
        if both: print(f'  skipping {len(both)} market(s) where {n} holds both sides')
        for p in P:
            if F(p,'conditionId','condition_id','asset','token_id') in both: continue
            price = float(F(p,'curPrice','cur_price','current_price','price',default=0) or 0); val = float(F(p,'currentValue','current_value','value',default=0) or 0)
            is_mention = cat_of(F(p,'title','market_title','question',default='')) == 'Mention'
            if F(p,'redeemable') in (True, 'True', 'true') or (val < w.get('min', min_value) and not is_mention) or val <= 0 or price <= 0 or price >= 1: continue
            out = F(p,'outcome'); idx = F(p,'outcomeIndex','outcome_index')
            side = out if out in ('Yes', 'No') else ('Yes' if str(idx) == '0' else 'No')
            yes = price if side == 'Yes' else 1 - price
            cid = F(p,'conditionId','condition_id','asset','token_id')
            m = markets.setdefault(cid, {'t': F(p,'title','market_title','question',default=''), 'slug': F(p,'slug','market_slug'), 'ev': F(p,'eventSlug','event_slug'),
                 'yes': yes, 'tok': (F(p,'token_id','asset','tokenId'), side), 'end': str(F(p,'endDate','end_date',default='') or '')[:10], 'img': F(p,'icon','image',default='') or '', 'tr': {}, 'out2': (out, F(p,'oppositeOutcome','opposite_outcome'))})
            prev = m['tr'].get(n)
            if prev and prev['val'] >= val: continue   # a trader holding both sides: keep the bigger one
            m['tr'][n] = {'n': n, 'side': side, 'sh': round(float(F(p,'current_size','size','shares',default=0)), 4), 'avg': round(float(F(p,'avgPrice','avg_price','average_price',default=0) or 0), 4), 'val': round(val, 4), 'ex': None, 'fe': int(float(F(p,'first_entry_at','firstEntryAt',default=0) or 0))}
        s = pnl_by.get(a) or []
        if s:
            pts = [v for _, v in s]; step = max(1, len(pts) // 90); spark = pts[::step][-90:]
            if spark[-1] != pts[-1]: spark.append(pts[-1])
            month = next((v for t, v in s if t >= s[-1][0] - 30 * 86400), pts[0])
            traders.append({'n': n, 'a': a, 'pnl': pts[-1], 'mpnl': pts[-1] - month, 'days': int((s[-1][0] - s[0][0]) / 86400), 's': [round(v, 2) for v in spark]})
        else:
            traders.append({'n': n, 'a': a, 'pnl': None, 'mpnl': None, 'days': 0, 's': []})
    D = []
    multi = sum(len(m['tr']) >= MIN_TRADERS for m in markets.values())
    solo_ok = SOLO_MIN > 0
    for cid, m in markets.items():
        tr = list(m['tr'].values())
        if len(tr) < MIN_TRADERS and not (solo_ok and sum(t['val'] for t in tr) >= SOLO_MIN): continue   # every market needs 2+ traders (mention markets: no $ minimum)
        cy = sum(t['side'] == 'Yes' for t in tr); top = 'Yes' if cy >= len(tr) - cy else 'No'
        lead = m['yes'] if top == 'Yes' else 1 - m['yes']
        if lead > MAX_PRICE: continue
        url = 'https://polymarket.com/event/' + (f"{m['ev']}/{m['slug']}" if m['ev'] and m['ev'] != m['slug'] else (m['slug'] or ''))
        title = m['t']
        o1, o2 = m['out2']
        if o1 not in ('Yes', 'No') and o1 and o2: title = f"{title} ({o1} = YES, {o2} = NO)"
        D.append({'cid': cid, 'tok': m['tok'], 't': title, 'url': url, 'cat': cat_of(m['t']), 'bid': round(m['yes'], 4), 'ask': round(m['yes'], 4), 'top': top,
                  'cnt': max(cy, len(tr) - cy), 'n': len(tr), 'tot': round(sum(t['val'] for t in tr), 2), 'tr': tr,
                  'q': urllib.parse.quote_plus(re.sub(r'^Will ', '', m['t']).rstrip('?')), 'end': m['end'], 'img': m['img']})
    traders.sort(key=lambda t: -(t['pnl'] or -1e18))
    print(f'{label}: {len(markets)} markets held, {multi} by {MIN_TRADERS}+ traders, {len(D)} shown')
    return D, traders, bool(markets) or any(t['pnl'] is not None for t in traders)

# ---------- live trades: recent v2 activity for every tracked wallet ----------
LIVE_HOURS = float(os.environ.get('LIVE_HOURS', 36))
LIVE_PAGES = int(os.environ.get('LIVE_PAGES', 3))
LIVE_ROWS = int(os.environ.get('LIVE_ROWS', 120))
FILL_GAP = 120          # fills of one order, grouped when they land within two minutes
MIN_TRADE_USD = 1.0

def norm_ts(x):
    t = F(x, 'timestamp', 'ts', 'time')
    if t is None: return 0
    try:
        t = float(t)
    except (TypeError, ValueError):
        return 0
    if t > 1e11: t /= 1000
    return int(t)

def market_url(slug, event_slug):
    slug, ev = slug or '', event_slug or ''
    path = f'{ev}/{slug}' if ev and slug and ev != slug else (slug or ev)
    return 'https://polymarket.com/event/' + path if path else 'https://polymarket.com/'

def js_json(obj):
    return json.dumps(obj, separators=(',', ':')).replace('<', '\\u003c')

def _activity_pages(addr):
    """Recent TRADE rows from data-api v2 activity, falling back to v2 trades. v1 is retired (HTTP 410)."""
    cutoff = time.time() - LIVE_HOURS * 3600
    rows, cur = [], None
    for _ in range(max(1, LIVE_PAGES)):
        q = {'user': addr, 'limit': 100, 'type': 'TRADE'}
        if cur: q['cursor'] = cur
        d = get(V2 + 'activity?' + urllib.parse.urlencode(q), quiet=True)
        if d is None: break
        r, cur = rows_of(d)
        rows += r or []
        if not r or not cur: break
        if min(norm_ts(x) or cutoff for x in r) < cutoff: break
    trades = [x for x in rows if str(F(x, 'type', default='TRADE')).upper() == 'TRADE' and str(F(x, 'side', default='')).upper() in ('BUY', 'SELL') and norm_ts(x) >= cutoff]
    if trades or rows: return trades
    rows, cur = [], None
    for _ in range(max(1, LIVE_PAGES)):
        q = {'user': addr, 'limit': 100, 'taker_only': 'false'}
        if cur: q['cursor'] = cur
        d = get(V2 + 'trades?' + urllib.parse.urlencode(q), quiet=True)
        if d is None: break
        r, cur = rows_of(d)
        rows += r or []
        if not r or not cur: break
        if min(norm_ts(x) or cutoff for x in r) < cutoff: break
    return [x for x in rows if str(F(x, 'side', default='')).upper() in ('BUY', 'SELL') and norm_ts(x) >= cutoff]

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def pos_index():
    """Shares and dollar value currently held: addr -> condition -> outcome.
    Value prefers current_value from data-api v2, otherwise current price × size."""
    idx, vals = {}, {}
    for addr, P in RAW.items():
        for p in P or []:
            if F(p, 'redeemable') in (True, 'True', 'true', 1): continue
            sh = _num(F(p, 'current_size', 'size', default=0))
            if sh is None or sh <= 0.01: continue
            cid = F(p, 'condition_id', 'conditionId')
            if not cid: continue
            out = F(p, 'outcome') or ('Yes' if str(F(p, 'outcome_index', 'outcomeIndex')) == '0' else 'No')
            val = _num(F(p, 'current_value', 'currentValue'))
            if val is None:
                px = _num(F(p, 'current_price', 'cur_price', 'curPrice')) or 0.0
                val = sh * px
            slot = idx.setdefault(addr, {}).setdefault(cid, {})
            slot[out] = slot.get(out, 0.0) + sh
            vslot = vals.setdefault(addr, {}).setdefault(cid, {})
            vslot[out] = vslot.get(out, 0.0) + val
    return idx, vals

def _holders(state):
    """state: addr -> outcome -> shares. One wallet on both outcomes is a hedge and is left out."""
    h = {}
    for addr, sides in state.items():
        live = {o: sh for o, sh in sides.items() if sh > 0.01}
        if len(live) != 1: continue
        o = next(iter(live))
        h.setdefault(o, set()).add(addr)
    return h

def _summary(holders):
    if not holders: return {'n': 0, 'cnt': 0, 'top': None, 'frac': 0.0}
    counts = {o: len(s) for o, s in holders.items()}
    top = max(counts, key=lambda o: (counts[o], o))
    n = sum(counts.values())
    return {'n': n, 'cnt': counts[top], 'top': top, 'frac': counts[top] / n if n else 0.0}

def _arrow(before, now):
    if now['n'] == 0: return '↓' if before['n'] else ''
    if before['n'] == 0: return '↑'
    if before['top'] != now['top']:
        return '↑' if now['frac'] >= before['frac'] else '↓'
    if abs(now['frac'] - before['frac']) < 1e-9 and now['cnt'] == before['cnt']: return ''
    if now['frac'] > before['frac'] + 1e-9 or (abs(now['frac'] - before['frac']) < 1e-9 and now['cnt'] > before['cnt']): return '↑'
    if now['frac'] < before['frac'] - 1e-9 or now['cnt'] < before['cnt']: return '↓'
    return ''

def _chip(side, outcome, summary, addr, holders):
    if summary['n'] == 0: return 'Exited', ''
    only = next(iter(next(iter(holders.values())))) if summary['n'] == 1 else None
    if summary['n'] == 1 and side == 'BUY' and only == addr: return 'First smart wallet', ''
    supports = (side == 'BUY' and outcome == summary['top']) or (side == 'SELL' and outcome != summary['top'])
    return ('With' if supports else 'Against'), f"{summary['cnt']}/{summary['n']}"

def _held_peak(held, events):
    """Peak shares of this outcome across the fetched window, so a partial sell shrinks the bar and a full exit empties it."""
    sh = held
    for t in reversed(events):
        sh += -t['shares'] if t['side'] == 'BUY' else t['shares']
        if sh < 0: sh = 0.0
    peak, cur = max(held, sh), sh
    for t in events:
        cur += t['shares'] if t['side'] == 'BUY' else -t['shares']
        if cur < 0: cur = 0.0
        if cur > peak: peak = cur
    return max(peak, held)

def live_feed(addrs, names):
    print(f'\n== live trades: {len(addrs)} wallets, last {LIVE_HOURS:g}h ==')
    with cf.ThreadPoolExecutor(8) as ex:
        fetched = list(ex.map(_activity_pages, addrs))
    raw_n = sum(len(x) for x in fetched)
    trades = []
    for addr, rows in zip(addrs, fetched):
        for x in rows:
            ts = norm_ts(x)
            side = str(F(x, 'side', default='')).upper()
            if side not in ('BUY', 'SELL') or not ts: continue
            try:
                shares = float(F(x, 'size', default=0) or 0)
                price = float(F(x, 'price', default=0) or 0)
            except (TypeError, ValueError):
                continue
            if shares <= 0: continue
            usd = F(x, 'usdc_size', 'usdcSize')
            try:
                usd = float(usd) if usd is not None else shares * price
            except (TypeError, ValueError):
                usd = shares * price
            if usd < MIN_TRADE_USD: continue
            cid = F(x, 'condition_id', 'conditionId') or ''
            if not cid: continue
            outcome = F(x, 'outcome') or ('Yes' if str(F(x, 'outcome_index', 'outcomeIndex')) == '0' else 'No')
            trades.append({'a': addr, 'n': names.get(addr) or F(x, 'name') or F(x, 'pseudonym') or (addr[:6] + '…' + addr[-4:]),
                'avatar': F(x, 'profile_image', 'profileImage') or '', 'cid': cid, 't': F(x, 'title', 'market_title', 'question') or 'Market',
                'slug': F(x, 'slug', 'market_slug') or '', 'ev': F(x, 'event_slug', 'eventSlug') or '', 'img': F(x, 'icon', 'image') or '',
                'side': side, 'out': outcome, 'shares': shares, 'price': price, 'usd': usd, 'ts': ts})
    print(f'  activity rows: {raw_n}, priced trades >= ${MIN_TRADE_USD:g}: {len(trades)}')
    idx, vals = pos_index()
    by_cid = {}
    for t in trades: by_cid.setdefault(t['cid'], []).append(t)
    cons, arrows = {}, {}
    for cid, evs in by_cid.items():
        state = {}
        for addr, markets in idx.items():
            sides = {o: sh for o, sh in (markets.get(cid) or {}).items() if sh > 0.01}
            if sides: state[addr] = dict(sides)
        now_h = _holders(state)
        now = _summary(now_h)
        for t in sorted(evs, key=lambda z: z['ts'], reverse=True):
            sides = state.setdefault(t['a'], {})
            cur = sides.get(t['out'], 0.0)
            cur += -t['shares'] if t['side'] == 'BUY' else t['shares']
            if cur <= 0.01: sides.pop(t['out'], None)
            else: sides[t['out']] = cur
            if not sides: state.pop(t['a'], None)
        before = _summary(_holders(state))
        cons[cid] = (now, now_h)
        arrows[cid] = _arrow(before, now)
    # group a burst of fills from the same wallet, market, side and outcome
    groups = []
    for t in sorted(trades, key=lambda z: (z['a'], z['cid'], z['side'], z['out'], z['ts'])):
        g = groups[-1] if groups else None
        if g and g['a'] == t['a'] and g['cid'] == t['cid'] and g['side'] == t['side'] and g['out'] == t['out'] and t['ts'] - g['ts'] <= FILL_GAP:
            g['fills'].append(t); g['ts'] = t['ts']
        else:
            groups.append({'a': t['a'], 'cid': t['cid'], 'side': t['side'], 'out': t['out'], 'ts': t['ts'], 'fills': [t]})
    ev_key = {}
    for t in trades: ev_key.setdefault((t['a'], t['cid'], t['out']), []).append(t)
    for k in ev_key: ev_key[k].sort(key=lambda z: z['ts'])
    rows = []
    for g in groups:
        fills = sorted(g['fills'], key=lambda z: z['ts'])
        head = max(fills, key=lambda z: z['ts'])
        usd, shares = sum(f['usd'] for f in fills), sum(f['shares'] for f in fills)
        price = usd / shares if shares else head['price']
        held = float((idx.get(g['a'], {}).get(g['cid'], {}) or {}).get(g['out'], 0.0) or 0.0)
        hval = float((vals.get(g['a'], {}).get(g['cid'], {}) or {}).get(g['out'], 0.0) or 0.0)
        if held <= 0.01: held, hval = 0.0, 0.0
        peak = _held_peak(held, ev_key.get((g['a'], g['cid'], g['out']), []))
        now, holders = cons.get(g['cid'], (_summary({}), {}))
        chip, split = _chip(g['side'], g['out'], now, g['a'], holders)
        title, slug, ev, img = head['t'], head['slug'], head['ev'], head['img']
        if title == 'Market' or not img:
            for p in RAW.get(g['a']) or []:
                if F(p, 'condition_id', 'conditionId') != g['cid']: continue
                title = title if title != 'Market' else (F(p, 'title', 'market_title', 'question') or title)
                slug = slug or F(p, 'slug', 'market_slug') or ''
                ev = ev or F(p, 'event_slug', 'eventSlug') or ''
                img = img or F(p, 'icon', 'image') or ''
                break
        rows.append({'n': head['n'], 'avatar': head['avatar'] or '', 't': title, 'url': market_url(slug, ev), 'img': img or '', 'cid': g['cid'],
            'side': g['side'], 'out': g['out'], 'price': round(price, 4), 'usd': round(usd, 2), 'shares': round(shares, 2),
            'ts': head['ts'], 'held': round(held, 2), 'hval': round(hval, 2), 'peak': round(max(peak, held), 2), 'chip': chip, 'split': split,
            'arrow': arrows.get(g['cid'], ''),
            'fills': [{'ts': f['ts'], 'usd': round(f['usd'], 2), 'shares': round(f['shares'], 2), 'price': round(f['price'], 4)} for f in fills] if len(fills) > 1 else []})
    rows.sort(key=lambda r: -r['ts'])
    # Parlay legs sometimes come back without a slug or image. Keep rows that link to a real market.
    rows = [r for r in rows if r['t'] and r['t'] != 'Market' and r['url'].startswith('https://polymarket.com/event/')]
    rows = rows[:max(1, LIVE_ROWS)]
    for i, r in enumerate(rows): r['id'] = i
    print(f'live trades: {len(rows)} rows from {len(addrs)} wallets')
    if rows:
        s = rows[0]
        print(f'  newest: {s["n"]} {s["side"]} {s["out"]} ${s["usd"]} · {s["chip"]} {s["split"]} {s["arrow"]} · {s["t"][:70]}')
    return rows

def main():
    # One tracked list. Each wallet keeps its own position minimum (macro $500, fast $10).
    W = load_wallets('wallets.json')
    D, T, ok = build_group(W, MIN_VALUE, 'Markets')
    CH = charts(D, W, T) if ok and os.environ.get('NO_CHARTS') != '1' else {}
    names = {w['address']: w['name'] for w in W if w.get('name')}
    addrs = [w['address'] for w in W]
    prefetch_positions(addrs)
    LIVE = live_feed(addrs, names)
    for m in D: m.pop('tok', None)
    json.dump(RAW, open(os.path.join(HERE, 'raw.json'), 'w', encoding='utf-8'))
    if not ok:
        sys.exit('No data came back from Polymarket (network blocked?). index.html was NOT changed.')
    if not LIVE:
        sys.exit('No recent trades came back from the Polymarket data API. index.html was NOT changed.')
    json.dump({'D': D, 'TR': T, 'CH': CH, 'LIVE': LIVE}, open(os.path.join(HERE, 'data.json'), 'w', encoding='utf-8'))
    html = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    html = html.replace('__CHARTS__', js_json(CH)).replace('__DATA__', js_json(D)).replace('__TRADERS__', js_json(T)).replace('__NTRADERS__', str(len(W)))
    html = html.replace('__LIVE__', js_json(LIVE)).replace('__NLIVE__', str(len(LIVE))).replace('__BUILT__', str(int(time.time())))
    open(os.path.join(HERE, 'index.html'), 'w', encoding='utf-8').write(html)
    print(f'\nDone: {len(W)} traders, {len(D)} markets shown -> index.html')

if __name__ == '__main__':
    main()
