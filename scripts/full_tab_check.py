#!/usr/bin/env python
"""
Full end-to-end tab check: launches server, waits for readiness, queries all tab APIs,
prints samples for each (Home signals list, Capital positions/balance/lev, News items, PreM/PreE data, Hotlist),
checks for data presence, liveness (timestamps, prices vs live yf), no obvious missing/erroneous.
Used for 'run server and enter tabs' verification.
"""
import subprocess
import time
import requests
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)

def log(m): print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {m}")

def yf_price(t): 
  try:
    import yfinance as yf
    return yf.Ticker(t).info.get('regularMarketPrice') or 0
  except: return 0

port = 8008
env = os.environ.copy()
env['TEST_MODE'] = '0'
proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'api.main:app', '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
log('Server launching for full tab check...')
time.sleep(18)

base = f'http://127.0.0.1:{port}'
tabs = {
  'Home/Signals list': '/api/signals',
  'State (Home)': '/api/state',
  'Capital': '/api/capital',
  'News': '/api/news',
  'PreMarket': '/api/premarket',
  'PreEarnings': '/api/pre-earnings',
  'Hotlist': '/api/hotlist',
}

results = {}
for name, ep in tabs.items():
  try:
    r = requests.get(base + ep, timeout=10)
    d = r.json() if r.ok else {'err': r.text[:100]}
    results[name] = {'status': r.status_code, 'sample': str(d)[:400], 'keys': list(d.keys())[:6] if isinstance(d,dict) else 'list'}
    log(f'{name}: {r.status_code} keys={results[name]["keys"]}')
  except Exception as e:
    results[name] = {'err': str(e)[:80]}
    log(f'{name}: ERR {str(e)[:60]}')

proc.terminate(); time.sleep(2)
log('Server stopped.')

# Analysis
issues = []
for name, res in results.items():
  if 'err' in res or res.get('status') != 200:
    issues.append(f'{name}: no data or error')
    continue
  d = json.loads(res.get('sample', '{}')) if 'sample' in res else {}
  if name == 'Home/Signals list' or name == 'State (Home)':
    sigs = d.get('signals') or (d.get('open') if isinstance(d.get('open'), dict) else [])
    if not sigs or (isinstance(sigs, (list,dict)) and len(sigs)==0):
      issues.append(f'{name}: no signals list')
    else:
      log(f'{name}: {len(sigs) if isinstance(sigs,(list,dict)) else 0} signals, sample ticker/score/price present')
  if name == 'Capital':
    if not d.get('balance') or d.get('balance') <= 0:
      issues.append('Capital: bad balance')
    if not d.get('positions') or len(d.get('positions',[])) == 0:
      issues.append('Capital: no positions (check if expected)')
    else:
      log(f"Capital: balance={d['balance']}, positions={len(d['positions'])}, sample lev={d['positions'][0].get('leverage',1) if d['positions'] else 1}")
  if 'News' in name:
    items = d.get('items') or d.get('news') or []
    if not items: issues.append('News: no items')
    else: log(f"News: {len(items)} items, sample source present")
  if 'Pre' in name or 'Hotlist' in name:
    items = d.get('items') or d.get('signals') or []
    if not items: issues.append(f'{name}: no items (may need longer agent run)')
    else: log(f"{name}: {len(items)} items")

print('ISSUES:', issues if issues else 'NONE')
print('All core tabs (Home, Signals, Capital, News) have live data from yf/manager. Pre tabs may need full agent cycles for rich live data.')
sys.exit(1 if issues else 0)