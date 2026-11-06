"""Smart Money (copy) build: fetch the wallets' Polymarket positions and PnL, inject into template.html -> index.html.
Usage: python3 build.py   (needs wallets.json next to it; writes data.json and index.html)"""
import json, time, urllib.request, urllib.parse, re, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_VALUE = float(os.environ.get('MIN_VALUE', 10))      # ignore positions worth less than this ($)
MAX_PRICE = 0.98     # hide markets where the leading side already trades above 98c
MIN_TRADERS = int(os.environ.get('MIN_TRADERS', 2))
MACRO_MIN_VALUE = float(os.environ.get('MACRO_MIN_VALUE', 500))   # macro traders bet bigger: ignore positions under this ($)
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
def dbg(k, v):
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
        seen.add(a); W.append({'address': a, 'name': w.get('name')})
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

def build_group(W, min_value, label):
    print(f'\n== {label}: {len(W)} traders ==')
    markets, traders = {}, []
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
        s = pnl_series(a)
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

def main():
    # one combined "Macro" group: wallets.json traders keep their small $ minimum, wallets_macro.json traders use $500
    W1, W2 = load_wallets('wallets.json'), load_wallets('wallets_macro.json')
    for w in W1: w['min'] = MIN_VALUE
    for w in W2: w['min'] = MACRO_MIN_VALUE
    seen, W = set(), []
    for w in W1 + W2:
        if w['address'] in seen: continue
        seen.add(w['address']); W.append(w)
    D, T, ok = build_group(W, MACRO_MIN_VALUE, 'Macro')
    CH = charts(D, W, T) if ok and os.environ.get('NO_CHARTS') != '1' else {}
    for m in D: m.pop('cid', None); m.pop('tok', None)
    json.dump(RAW, open(os.path.join(HERE, 'raw.json'), 'w', encoding='utf-8'))
    if not ok:
        sys.exit('No data came back from Polymarket (network blocked?). index.html was NOT changed.')
    json.dump({'D': D, 'TR': T, 'CH': CH}, open(os.path.join(HERE, 'data.json'), 'w', encoding='utf-8'))
    html = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    html = html.replace('__CHARTS__', json.dumps(CH, separators=(',', ':'))).replace('__DATA__', json.dumps(D)).replace('__TRADERS__', json.dumps(T)).replace('__NTRADERS__', str(len(W)))
    open(os.path.join(HERE, 'index.html'), 'w', encoding='utf-8').write(html)
    print(f'\nDone: Macro {len(W)} traders, {len(D)} markets shown -> index.html')

if __name__ == '__main__':
    main()
