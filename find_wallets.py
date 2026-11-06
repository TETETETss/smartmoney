"""Find the wallet address of every trader on polyintel.pages.dev.
Usage: python find_wallets.py   ->  prints the list and saves old_wallets.txt + old_wallets.json"""
import json, re, urllib.request, urllib.parse, os

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'}

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8', 'replace')

# 1. trader names from the live site
html = fetch('https://polyintel.pages.dev/')
m = re.search(r'const TR=(\[.*?\]);', html, re.S)
names = [t['n'] for t in json.loads(m.group(1))] if m else []
print(f'{len(names)} traders on polyintel.pages.dev\n')

def find(name):
    # a) Polymarket profile search, exact name match
    try:
        q = urllib.parse.urlencode({'q': name, 'search_profiles': 'true', 'limit_per_type': 10})
        d = json.loads(fetch('https://gamma-api.polymarket.com/public-search?' + q))
        for p in d.get('profiles') or []:
            if (p.get('name') or '').lower() == name.lower() or (p.get('pseudonym') or '').lower() == name.lower():
                w = p.get('proxyWallet') or p.get('proxy_wallet')
                if w: return w.lower()
    except Exception:
        pass
    # b) the profile page itself
    try:
        page = fetch('https://polymarket.com/@' + urllib.parse.quote(name))
        w = re.search(r'"proxyWallet":"(0x[a-fA-F0-9]{40})"', page)
        if w: return w.group(1).lower()
    except Exception:
        pass
    return None

out = []
for n in names:
    w = find(n)
    out.append({'name': n, 'address': w})
    print(f'{n:<22} {w or "NOT FOUND"}')

json.dump(out, open(os.path.join(HERE, 'old_wallets.json'), 'w', encoding='utf-8'), indent=1)
with open(os.path.join(HERE, 'old_wallets.txt'), 'w', encoding='utf-8') as f:
    for o in out:
        f.write(f"{o['name']}\t{o['address'] or 'NOT FOUND'}\n")
print(f"\nFound {sum(1 for o in out if o['address'])} of {len(out)} -> old_wallets.txt")
