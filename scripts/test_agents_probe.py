import urllib.request
import json

endpoints = [
    '/api/agents',
    '/api/agents/control-plane',
    '/api/execution/control',
    '/api/agents/risk/pipeline-status',
    '/api/bot/status',
]

for ep in endpoints:
    url = f'http://127.0.0.1:8000{ep}'
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            print(f'=== {ep} === OK {resp.status}')
            if ep == '/api/agents':
                agents = data.get('agents', {})
                print(f'Total agents: {len(agents)}')
                for k, v in agents.items():
                    print(f"  {k}: enabled={v.get('enabled')}, running={v.get('running')}, lifecycle={v.get('lifecycle_state')}, managed_by={v.get('managed_by')}, last_err={v.get('last_error')}")
            elif ep == '/api/agents/control-plane':
                print('Bot:', data.get('bot'))
                print('Agents in payload:', len(data.get('agents', {})))
            elif ep == '/api/execution/control':
                print('Mode:', data.get('mode'))
                print('KillSwitch:', data.get('kill_switch_active'))
                readiness = data.get('readiness', {})
                print('Readiness ready:', readiness.get('ready'))
                print('Readiness reasons:', readiness.get('reasons'))
            elif ep == '/api/agents/risk/pipeline-status':
                print('Pipeline status keys:', list(data.keys()))
                result = data.get('result', {})
                print('Cycle candidate count:', result.get('cycle', {}).get('candidate_count'))
    except Exception as e:
        print(f'=== {ep} === ERROR: {e}')
