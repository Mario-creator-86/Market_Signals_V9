import json
import os

state_path = 'state.json'
if os.path.exists(state_path):
    with open(state_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    tickers = data.get('tickers', {})
    print(f"Found {len(tickers)} tickers in state.")
    
    targets = ["AAPL", "MARA", "CIFR", "BTC-USD"]
    for t in targets:
        if t in tickers:
            td = tickers[t]
            print(f"{t}: Price={td.get('price')}, Change={td.get('change_pct')} (Source: {td.get('asset_type')})")
        else:
            print(f"{t}: Not found in state.")
else:
    print(f"{state_path} does not exist.")
