"""Find likely alt wallets ("entities") for the tracked smart-money wallets.

How it works (all public data, no API keys):
 1. Pull each tracked wallet's stablecoin transfers (pUSD, USDC.e, USDC) from Blockscout.
 2. Drop infrastructure: counterparties that show up in 5+ tracked wallets (exchange, relayers, bridges),
    and transfers made inside trade transactions (fills settle wallet-to-wallet).
 3. What is left are real deposits / withdrawals. A counterparty that is itself a Polymarket account
    and moved money with the tracked wallet repeatedly is a likely alt. A plain wallet (EOA) that funds
    or receives from the tracked wallet is its funding wallet; we follow it one hop to find other
    Polymarket accounts it funds.
 4. Every candidate is checked on Polymarket (profile name, volume, PnL) and scored.

Output: entities.json  {built, entities: {trader: {accounts:[...], funders:[...]}}, debug:{...}}
The result is cached on the live site and only recomputed every ENT_HOURS hours.
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error, collections
import concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.environ.get('SITE_URL', 'https://smartmoney-2ym.pages.dev')
ENT_HOURS = float(os.environ.get('ENT_HOURS', 12))
BS = 'https://polygon.blockscout.com'
V2 = 'https://data-api.polymarket.com/v2/'
STABLES = {'0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb', '0x2791bca1f2de4661ed88a30c99a7a9449aa84174', '0x3c499c542cef5e3811e1192ce70d8cc03d5c3359'}
ZERO = '0x0000000000000000000000000000000000000000'
PROBE = [1]
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36', 'Accept': 'application/json'}

def get(url, tries=6, timeout=40):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                d = json.loads(r.read().decode())
            if isinstance(d, dict) and 'too many' in str(d.get('message', '')).lower():
                time.sleep(3 * (i + 1)); continue
            return d
        except urllib.error.HTTPError as e:
            if PROBE: print('   http', e.code, url[:90], e.read()[:160])
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (i + 1)); continue
            return None
        except Exception as e:
            if PROBE: print('   err', url[:90], str(e)[:160])
            time.sleep(2 * (i + 1))
    return None

# ---------- Blockscout (throttled: one request at a time, ~4/s)
_last = [0.0]
def bs(path):
    wait = 0.28 - (time.time() - _last[0])
    if wait > 0: time.sleep(wait)
    _last[0] = time.time()
    return get(BS + path)

def transfers(addr, n=10000):
    d = bs(f'/api?module=account&action=tokentx&address={addr}&sort=desc&page=1&offset={n}')
    rows = (d or {}).get('result') or []
    if not isinstance(rows, list): rows = []
    out = []
    for x in rows:
        if str(x.get('contractAddress', '')).lower() not in STABLES: continue
        dec = int(x.get('tokenDecimal') or 6)
        out.append({'f': x['from'].lower(), 't': x['to'].lower(), 'v': int(x['value']) / 10 ** dec,
                    'ts': int(x['timeStamp']), 'm': str(x.get('input', ''))[:10], 'h': x['hash']})
    return out, len(rows)

_info = {}
def addr_info(a):
    if a in _info: return _info[a]
    d = bs(f'/api/v2/addresses/{a}') or {}
    impl = d.get('implementations') or []
    i = {'contract': bool(d.get('is_contract')), 'name': d.get('name') or (impl[0].get('name') if impl else None) or '',
         'tx': None}
    _info[a] = i
    return i

# ---------- Polymarket checks
def pm_profile(a):
    p = get('https://gamma-api.polymarket.com/public-profile?address=' + a, tries=2) or {}
    return {'name': p.get('name') or p.get('pseudonym') or '', 'img': p.get('profileImage') or ''}

def pm_stats(a):
    d = get(V2 + 'user-pnl?' + urllib.parse.urlencode({'user': a, 'interval': 'all', 'fidelity': '1d'}), tries=2)
    pts = d if isinstance(d, list) else (d or {}).get('data') or (d or {}).get('points') or []
    if isinstance(pts, dict): pts = pts.get('points') or []
    vol = pnl = 0
    if pts:
        last = pts[-1]
        vol = float(last.get('volume_usdc') or last.get('volume') or 0)
        pnl = float(last.get('economic_pnl') or last.get('pnl') or last.get('p') or 0)
    return {'vol': round(vol), 'pnl': round(pnl), 'days': len(pts)}

def pm_value(a):
    d = get(V2 + 'value?' + urllib.parse.urlencode({'user': a}), tries=2)
    if isinstance(d, list) and d: d = d[0]
    if isinstance(d, dict):
        v = d.get('value')
        if v is None and isinstance(d.get('data'), dict): v = d['data'].get('value')
        try: return round(float(v or 0))
        except Exception: return 0
    return 0

def is_pm_account(info):
    n = (info.get('name') or '').lower()
    return info.get('contract') and ('safe' in n or 'proxy' in n)

# ---------- main analysis
def analyse(W):
    names = {w['address'].lower(): w['name'] for w in W}
    TX, totals = {}, {}
    for w in W:
        a = w['address'].lower()
        TX[a], totals[a] = transfers(a)
        print(f'  {names[a]}: {len(TX[a])} stable transfers ({totals[a]} rows)')

    # selectors used by trade fills: the ones whose rows mostly touch many different wallets
    seen = collections.Counter()
    for a, rows in TX.items():
        for c in {r['t'] if r['f'] == a else r['f'] for r in rows}: seen[c] += 1
    infra = {c for c, k in seen.items() if k >= 5} | {ZERO}

    sel_all, sel_tt = collections.Counter(), collections.Counter()
    for a, rows in TX.items():
        for r in rows:
            sel_all[r['m']] += 1
            c = r['t'] if r['f'] == a else r['f']
            if c in names: sel_tt[r['m']] += 1
    # a selector is "trade" if most of its rows go to infra or to other traders -> learned from data
    sel_infra = collections.Counter()
    for a, rows in TX.items():
        for r in rows:
            c = r['t'] if r['f'] == a else r['f']
            if c in infra: sel_infra[r['m']] += 1
    TRADE_SEL = {s for s, k in sel_all.items() if k >= 30 and sel_infra[s] / k >= 0.6}

    CP = {}
    for a, rows in TX.items():
        m = {}
        for r in rows:
            if r['m'] in TRADE_SEL: continue
            out = r['f'] == a
            c = r['t'] if out else r['f']
            if c in infra: continue
            e = m.setdefault(c, {'in': 0.0, 'out': 0.0, 'k': 0, 'first': r['ts'], 'last': r['ts'], 'sel': collections.Counter()})
            e['out' if out else 'in'] += r['v']; e['k'] += 1; e['sel'][r['m']] += 1
            e['first'] = min(e['first'], r['ts']); e['last'] = max(e['last'], r['ts'])
        CP[a] = m

    # candidates: real money moved, repeated or big
    cand = {}
    for a, m in CP.items():
        ranked = sorted(m.items(), key=lambda kv: -(kv[1]['in'] + kv[1]['out']))
        for c, e in ranked[:12]:
            tot = e['in'] + e['out']
            if tot < 200 and e['k'] < 3: continue
            cand.setdefault(c, []).append(a)
    print(f'  {len(cand)} candidate counterparties, {len(infra)} infra, trade selectors {sorted(TRADE_SEL)}')

    for c in cand: addr_info(c)

    # 2-hop: funding wallets (EOAs) -> other Polymarket accounts they send to / receive from
    hop = {}
    eoas = [c for c in cand if not _info[c]['contract'] and c not in names]
    for c in eoas[:80]:
        rows, _ = transfers(c, 2000)
        m = collections.defaultdict(lambda: [0.0, 0])
        for r in rows:
            o = r['t'] if r['f'] == c else r['f']
            if o in infra or o == c: continue
            m[o][0] += r['v']; m[o][1] += 1
        hop[c] = {o: v for o, v in m.items() if v[0] >= 100}
    second = {o for h in hop.values() for o in h if o not in names and o not in cand}
    # only keep second-hop addresses shared by a funder that also touches a tracked wallet
    for o in list(second)[:250]: addr_info(o)

    # Polymarket check for every account-like address
    pm_addrs = [c for c in set(cand) | second if c in _info and is_pm_account(_info[c]) and c not in names]
    PM = {}
    def chk(a):
        p = pm_profile(a); s = pm_stats(a)
        s.update(p); s['value'] = pm_value(a) if s['vol'] else 0
        return a, s
    with cf.ThreadPoolExecutor(6) as ex:
        for a, s in ex.map(chk, pm_addrs): PM[a] = s

    ents = {}
    for a, m in CP.items():
        trader = names[a]
        accts, funders = [], []
        for c, e in m.items():
            if c not in cand: continue
            i = _info.get(c, {})
            tot = e['in'] + e['out']
            row = {'a': c, 'in': round(e['in']), 'out': round(e['out']), 'n': e['k'], 'first': e['first'], 'last': e['last']}
            if c in names:
                row.update({'tracked': names[c], 'name': names[c]})
                accts.append(row); continue
            if is_pm_account(i):
                s = PM.get(c, {})
                if not s.get('vol'): continue
                row.update(s)
                accts.append(row)
            elif not i.get('contract'):
                row['others'] = []
                for o, (v, k) in (hop.get(c) or {}).items():
                    if o == a: continue
                    if o in names: row['others'].append({'a': o, 'tracked': names[o], 'v': round(v), 'n': k}); continue
                    if o in PM and PM[o].get('vol'):
                        row['others'].append({'a': o, 'v': round(v), 'n': k, **PM[o]})
                funders.append(row)
        # score
        for r in accts:
            both = r['in'] > 0 and r['out'] > 0
            big = r['in'] + r['out']
            sc = 0
            sc += 2 if r['n'] >= 3 else 1 if r['n'] == 2 else 0
            sc += 2 if big >= 10000 else 1 if big >= 1000 else 0
            sc += 1 if both else 0
            r['score'] = sc
            r['conf'] = 'high' if sc >= 4 else 'medium' if sc >= 2 else 'low'
        accts.sort(key=lambda r: (-r['score'], -(r['in'] + r['out'])))
        funders.sort(key=lambda r: -(r['in'] + r['out']))
        ents[trader] = {'a': a, 'accounts': accts[:15], 'funders': funders[:10]}

    # shared funders: same EOA touching 2+ tracked wallets
    shared = collections.defaultdict(list)
    for a, m in CP.items():
        for c in m:
            if c in cand and c in _info and not _info[c]['contract']: shared[c].append(names[a])
    shared = {c: v for c, v in shared.items() if len(v) >= 2}

    debug = {'infra_n': len(infra), 'trade_sel': sorted(TRADE_SEL), 'sel_all': sel_all.most_common(25),
             'sel_tracked_to_tracked': sel_tt.most_common(15), 'totals': {names[a]: totals[a] for a in totals},
             'shared_funders': shared, 'n_cand': len(cand), 'n_second': len(second), 'n_pm': len(PM),
             'info_names': collections.Counter(i['name'] for i in _info.values()).most_common(15)}
    return ents, debug

def main():
    force = os.environ.get('ENT_FORCE') == '1'
    if not force:
        old = get(SITE + '/entities.json', tries=2)
        if isinstance(old, dict) and old.get('built') and old.get('n_tx') and time.time() - old['built'] < ENT_HOURS * 3600:
            json.dump(old, open(os.path.join(HERE, 'entities.json'), 'w'))
            print(f'entities: reused cached file from {int((time.time() - old["built"]) / 60)} min ago')
            return
    W = json.load(open(os.path.join(HERE, 'wallets.json'), encoding='utf-8'))
    W = [dict(w, name=w.get('name') or w['address'][:6] + '…' + w['address'][-4:]) for w in W if w.get('address')]
    probe = get(BS + '/api?module=account&action=tokentx&address=' + W[0]['address'] + '&sort=desc&page=1&offset=5', tries=2)
    print('blockscout probe:', str(probe)[:300])
    PROBE.clear()
    t0 = time.time()
    ents, debug = analyse(W)
    n_tx = sum(debug['totals'].values())
    out = {'built': int(time.time()), 'n_tx': n_tx, 'secs': int(time.time() - t0), 'entities': ents, 'debug': debug}
    json.dump(out, open(os.path.join(HERE, 'entities.json'), 'w'), default=list)
    n = sum(1 for e in ents.values() if e['accounts'])
    print(f'entities: done in {out["secs"]}s, {n} traders with linked accounts')

if __name__ == '__main__':
    main()
