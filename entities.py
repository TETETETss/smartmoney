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
PROBE = []
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

# ---------- Polygon RPC (Blockscout blocks GitHub runners, public RPC does not)
RPCS = ['https://polygon-bor-rpc.publicnode.com', 'https://polygon.drpc.org', 'https://1rpc.io/matic']
TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
CHUNK = 9999
def rpc(method, params, tries=5):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    err = None
    for i in range(tries):
        url = RPCS[i % len(RPCS)]
        try:
            req = urllib.request.Request(url, data=body, headers={'content-type': 'application/json', **UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
            if 'result' in d: return d['result']
            err = d.get('error')
        except Exception as e:
            err = str(e)[:120]
        time.sleep((6 if '403' in str(err) or '429' in str(err) else 1.5) * (i + 1))
    raise RuntimeError(f'{method} failed: {err}')

def pad(a): return '0x' + a.lower()[2:].rjust(64, '0')

def scan(addrs, b0, b1, threads=5, budget=None):
    """Stablecoin Transfer logs from/to any of addrs in blocks b0..b1, newest chunk first.
    Stops starting new chunks after `budget` seconds. Returns (rows, lo, stats): every block in lo..b1 was read."""
    pads = [pad(a) for a in addrs]
    chunks = []
    e = b1
    while e >= b0:
        s = max(b0, e - CHUNK); chunks.append((s, e)); e = s - 1
    t0 = time.time()
    def one(c):
        if budget and time.time() - t0 > budget: return c, None
        s, e = c
        got = []
        try:
            for tp in ([TOPIC, pads], [TOPIC, None, pads]):
                got += rpc('eth_getLogs', [{'fromBlock': hex(s), 'toBlock': hex(e), 'address': sorted(STABLES), 'topics': tp}], tries=4)
        except Exception as ex:
            print('   chunk failed', s, str(ex)[:120]); return c, None
        return c, got
    rows, done, n = [], {}, 0
    with cf.ThreadPoolExecutor(threads) as ex:
        for c, res in ex.map(one, chunks):
            n += 1
            if n % 100 == 0: print(f'   {n}/{len(chunks)} chunks, {int(time.time() - t0)}s')
            done[c] = res is not None
            for l in res or []:
                if len(l.get('topics', [])) < 3: continue
                rows.append(('0x' + l['topics'][1][26:], '0x' + l['topics'][2][26:], int(l['data'], 16) / 1e6, int(l['blockNumber'], 16), l.get('transactionHash')))
    lo = b1 + 1
    for c in chunks:
        if not done.get(c): break
        lo = c[0]
    # drop rows from chunks below the contiguous range (they will be read again next time)
    rows = [r for r in rows if r[3] >= lo]
    return rows, lo, {'chunks': len(chunks), 'ok': sum(done.values()), 'secs': int(time.time() - t0)}

_code = {}
def is_contract(a):
    if a not in _code:
        try: _code[a] = len(rpc('eth_getCode', [a, 'latest'])) > 2
        except Exception: _code[a] = None
    return _code[a]

def nonce(a):
    try: return int(rpc('eth_getTransactionCount', [a, 'latest']), 16)
    except Exception: return None

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


# ---------- main analysis
DAYS = float(os.environ.get('ENT_DAYS', 180))      # history depth for the tracked wallets
HOP_DAYS = float(os.environ.get('ENT_HOP_DAYS', 90))
SCAN_BUDGET = float(os.environ.get('ENT_SCAN_SECS', 480))
HOP_BUDGET = float(os.environ.get('ENT_HOP_SECS', 180))
RC_BUDGET = float(os.environ.get('ENT_RC_SECS', 180))

def merge(agg, rows, wallets):
    for f, t, v, b, h in rows:
        for w, c, side in ((f, t, 1), (t, f, 0)):
            if w not in wallets or c == w: continue
            e = agg.setdefault(w, {}).setdefault(c, [0.0, 0.0, 0, b, b, []])   # in, out, count, first block, last block, top transfers
            e[side] += v; e[2] += 1; e[3] = min(e[3], b); e[4] = max(e[4], b)
            if h and v >= 50:
                e[5].append([round(v, 2), h, b, side]); e[5].sort(key=lambda x: -x[0]); del e[5][TOPS:]

CTF = '0x4d97dcd97ec945f40cf65f87097ace5ea0476045'    # Polymarket outcome tokens (ERC1155)
TOPS = 4
def has_ctf(h):
    """True if the transaction also moved outcome tokens (a trade, split, merge or redeem), not a plain transfer."""
    try:
        r = rpc('eth_getTransactionReceipt', [h], tries=3)
        return any(str(l.get('address', '')).lower() == CTF for l in (r or {}).get('logs', []))
    except Exception:
        return None

def pm_check(a):
    s = pm_stats(a)
    if s['vol']:
        s.update(pm_profile(a)); s['value'] = pm_value(a)
    return a, s

def analyse(W, state):
    names = {w['address'].lower(): w['name'] for w in W}
    wallets = set(names)
    head = int(rpc('eth_blockNumber', []), 16)
    span = int(DAYS * 86400 / 2.05)
    hts = int(rpc('eth_getBlockByNumber', [hex(head), False])['timestamp'], 16)
    ots = int(rpc('eth_getBlockByNumber', [hex(head - span), False])['timestamp'], 16)
    spb = (hts - ots) / span
    bts = lambda b: int(hts - (head - b) * spb)

    target = head - span
    same = set(state.get('wallets') or []) == wallets and state.get('ver') == 2
    agg = {w: {c: list(v) for c, v in m.items()} for w, m in (state.get('agg') or {}).items()} if same else {}
    hi, lo = (state.get('hi'), state.get('lo')) if same and state.get('hi') else (None, None)
    stats = {}
    t0 = time.time()
    if hi is None:
        rows, lo, st = scan(list(wallets), target, head, budget=SCAN_BUDGET)
        merge(agg, rows, wallets); hi = head; stats['first'] = st
    else:
        if hi < head:
            rows, l2, st = scan(list(wallets), hi + 1, head, budget=SCAN_BUDGET)
            if l2 == hi + 1: merge(agg, rows, wallets); hi = head
            stats['new'] = st
        left = SCAN_BUDGET - (time.time() - t0)
        if lo > target and left > 30:
            rows, l2, st = scan(list(wallets), target, lo - 1, budget=left)
            merge(agg, rows, wallets); lo = min(lo, l2); stats['back'] = st
    print(f'  history: {(head - lo) * spb / 86400:.0f} of {DAYS:.0f} days read, {stats}')
    for w in wallets: agg.setdefault(w, {})

    seen = collections.Counter(c for m in agg.values() for c in m)
    infra = {c for c, k in seen.items() if k >= 5} | {ZERO}

    cand = {}
    for w, m in agg.items():
        ranked = sorted(((c, e) for c, e in m.items() if c not in infra), key=lambda kv: -(kv[1][0] + kv[1][1]))
        for c, e in ranked[:40]:
            if e[0] + e[1] < 200: continue
            cand.setdefault(c, []).append(w)
    others = [c for c in cand if c not in wallets]
    with cf.ThreadPoolExecutor(8) as ex: list(ex.map(is_contract, others))

    # contract <-> wallet transfers are usually trade fills (they settle wallet to wallet).
    # Keep a pair only if at least one of its biggest transfers moved no outcome tokens.
    RC = dict(state.get('rc') or {})
    need = []
    for c, ws in cand.items():
        if _code.get(c) is False: continue
        for w in ws: need += [x[1] for x in agg[w][c][5] if x[1] not in RC]
    need = list(dict.fromkeys(need))
    t1 = time.time()
    def rc_one(h):
        if time.time() - t1 > RC_BUDGET: return h, None
        return h, has_ctf(h)
    with cf.ThreadPoolExecutor(6) as ex:
        for h, v in ex.map(rc_one, need):
            if v is not None: RC[h] = 1 if v else 0
    pending = sum(1 for h in need if h not in RC)
    GEN = {}
    for c, ws in list(cand.items()):
        if _code.get(c) is False: continue
        keep = []
        for w in ws:
            g = [x for x in agg[w][c][5] if RC.get(x[1]) == 0]
            if g:
                GEN[(w, c)] = g; keep.append(w)
        if keep: cand[c] = keep
        else: del cand[c]
    print(f'  receipts: {len(need) - pending} checked, {pending} left for next run, {len(GEN)} wallet pairs with plain transfers')
    others = [c for c in cand if c not in wallets]
    PM = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for a, s in ex.map(pm_check, [c for c in others if _code.get(c)]): PM[a] = s
    funders = [c for c in others if _code.get(c) is False]
    NON = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for c, n in zip(funders, ex.map(nonce, funders)): NON[c] = n
    hop_src = [c for c in funders if NON.get(c) is not None and NON[c] < 20000]
    print(f'  {len(cand)} candidates: {len(PM)} contracts, {sum(1 for s in PM.values() if s["vol"])} Polymarket accounts, {len(funders)} plain wallets ({len(hop_src)} followed)')

    # one hop out from the plain (funding) wallets
    hop = collections.defaultdict(dict)
    if hop_src:
        src = set(hop_src)
        rows, hlo, st = scan(hop_src, head - int(HOP_DAYS * 86400 / 2.05), head, threads=4, budget=HOP_BUDGET)
        stats['hop'] = st; stats['hop_days'] = round((head - hlo) * spb / 86400)
        for f, t, v, b, _h in rows:
            for x, o in ((f, t), (t, f)):
                if x not in src or o in infra or o == x: continue
                e = hop[x].setdefault(o, [0.0, 0])
                e[0] += v; e[1] += 1
        second = {o for h in hop.values() for o, e in h.items() if e[0] >= 100 and o not in wallets and o not in PM}
        second = sorted(second, key=lambda o: -max(h.get(o, [0])[0] for h in hop.values()))[:250]
        with cf.ThreadPoolExecutor(8) as ex: list(ex.map(is_contract, second))
        with cf.ThreadPoolExecutor(6) as ex:
            for a, s in ex.map(pm_check, [o for o in second if _code.get(o)]): PM[a] = s
        print(f'  hop: {len(rows)} transfers, {len(second)} new addresses checked')

    ents = {}
    for w, m in agg.items():
        accts, fund = [], []
        for c, (i, o, k, b0, b1, tops) in m.items():
            if c not in cand or w not in cand[c]: continue
            row = {'a': c, 'in': round(i), 'out': round(o), 'n': k, 'first': bts(b0), 'last': bts(b1)}
            g = GEN.get((w, c))
            if g:   # contract pair: count only the plain transfers we verified
                row.update({'in': round(sum(x[0] for x in g if x[3] == 0)), 'out': round(sum(x[0] for x in g if x[3] == 1)), 'n': len(g),
                            'first': bts(min(x[2] for x in g)), 'last': bts(max(x[2] for x in g)), 'tx': [x[1] for x in g][:3]})
            if c in wallets:
                row.update({'tracked': names[c], 'name': names[c]}); accts.append(row)
            elif c in PM and PM[c].get('vol'):
                row.update(PM[c]); accts.append(row)
            elif _code.get(c) is False:
                row['nonce'] = NON.get(c)
                if NON.get(c) is not None and NON[c] >= 20000: row['exchange'] = True
                row['others'] = []
                for x, (v, kk) in hop.get(c, {}).items():
                    if x == w or v < 100: continue
                    if x in wallets: row['others'].append({'a': x, 'tracked': names[x], 'v': round(v), 'n': kk})
                    elif x in PM and PM[x].get('vol'): row['others'].append({'a': x, 'v': round(v), 'n': kk, **PM[x]})
                row['others'].sort(key=lambda r: -r['v']); row['others'] = row['others'][:10]
                fund.append(row)
        for r in accts:
            tot = r['in'] + r['out']
            sc = (2 if r['n'] >= 3 else 1 if r['n'] == 2 else 0) + (2 if tot >= 10000 else 1 if tot >= 1000 else 0) + (1 if r['in'] and r['out'] else 0)
            r['score'] = sc; r['conf'] = 'high' if sc >= 4 else 'medium' if sc >= 2 else 'low'
        accts.sort(key=lambda r: (-r['score'], -(r['in'] + r['out'])))
        fund.sort(key=lambda r: -(r['in'] + r['out']))
        ents[names[w]] = {'a': w, 'accounts': accts[:15], 'funders': fund[:10]}

    shared = collections.defaultdict(list)
    for c, ws in cand.items():
        if _code.get(c) is False and len(ws) >= 2: shared[c] = [names[x] for x in ws]

    # keep state small: drop dust counterparties
    keep = {w: {c: [round(e[0], 2), round(e[1], 2), e[2], e[3], e[4], e[5] if c not in infra and e[0] + e[1] >= 200 else []]
                for c, e in m.items() if c in infra or e[0] + e[1] >= 20 or e[2] >= 2} for w, m in agg.items()}
    live = {x[1] for m in keep.values() for e in m.values() for x in e[5]}
    RC = {h: v for h, v in RC.items() if h in live}
    debug = {'infra': sorted(infra)[:80], 'infra_n': len(infra), 'scan': stats, 'n_cand': len(cand), 'n_pm': len(PM),
             'shared_funders': shared, 'cp_per_wallet': {names[w]: len(m) for w, m in agg.items()}}
    debug['days'] = round((head - lo) * spb / 86400); debug['complete'] = lo <= target
    debug['receipts_left'] = pending
    return ents, debug, {'ver': 2, 'hi': hi, 'lo': lo, 'wallets': sorted(wallets), 'agg': keep, 'rc': RC}

def main():
    force = os.environ.get('ENT_FORCE') == '1'
    old = {} if force else (get(SITE + '/entities.json', tries=2) or {})
    if not isinstance(old, dict): old = {}
    if old.get('built') and old.get('n_tx') and (old.get('debug') or {}).get('complete') and not (old.get('debug') or {}).get('receipts_left') and time.time() - old['built'] < ENT_HOURS * 3600:
        json.dump(old, open(os.path.join(HERE, 'entities.json'), 'w'))
        print(f'entities: reused cached file from {int((time.time() - old["built"]) / 60)} min ago')
        return
    W = json.load(open(os.path.join(HERE, 'wallets.json'), encoding='utf-8'))
    W = [dict(w, name=w.get('name') or w['address'][:6] + '…' + w['address'][-4:]) for w in W if w.get('address')]
    t0 = time.time()
    ents, debug, state = analyse(W, old.get('state') or {})
    n_tx = sum(debug['cp_per_wallet'].values())
    out = {'built': int(time.time()), 'secs': int(time.time() - t0), 'n_tx': n_tx, 'entities': ents, 'debug': debug, 'state': state}
    json.dump(out, open(os.path.join(HERE, 'entities.json'), 'w'), default=list, separators=(',', ':'))
    n = sum(1 for e in ents.values() if e['accounts'])
    print('\n=== linked accounts ===')
    for t, e in sorted(ents.items()):
        for r in e['accounts'][:6]:
            print(f"  {t:22s} <-> {(r.get('name') or r['a'][:10]):22s} {r['a']} in ${r['in']:>9,} out ${r['out']:>9,} n={r['n']:<3} {r.get('conf')} vol=${r.get('vol', 0):,} pnl=${r.get('pnl', 0):,}")
        for r in e['funders'][:3]:
            print(f"  {t:22s} <- wallet {r['a']} in ${r['in']:>9,} out ${r['out']:>9,} n={r['n']:<3} nonce={r.get('nonce')} also funds: {[o.get('tracked') or o.get('name') or o['a'][:10] for o in r['others'][:5]]}")
    print('shared funders:', debug['shared_funders'])
    print(f'entities: done in {out["secs"]}s, {n} traders with linked accounts, file {os.path.getsize(os.path.join(HERE, "entities.json")) // 1024} KB')

if __name__ == '__main__':
    main()
