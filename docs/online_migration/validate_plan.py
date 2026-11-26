"""Check handoff consistency, not application correctness or market capacity."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATES = {'TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE'}


def main():
    errors = []
    tasks = json.loads((ROOT / 'TASKS.json').read_text(encoding='utf-8'))['tasks']
    packages = json.loads((ROOT / 'PACKAGE_DRAFT.json').read_text(encoding='utf-8'))
    by_id = {t['id']: t for t in tasks}
    if len(by_id) != len(tasks):
        errors.append('Duplicate task IDs')

    def exists_safe(relative):
        path = (ROOT / relative).resolve()
        return path.is_relative_to(ROOT) and path.is_file()

    action_ids = set()
    for task in tasks:
        tid = task['id']
        if task['status'] not in STATES:
            errors.append(f'{tid}: invalid status')
        if not task['actions'] or not task['acceptance_criteria']:
            errors.append(f'{tid}: missing actions or acceptance criteria')
        for dep in task['depends_on']:
            if dep not in by_id:
                errors.append(f'{tid}: missing dependency {dep}')
        for action in task['actions']:
            aid = action['id']
            if aid in action_ids or not aid.startswith(tid + '.'):
                errors.append(f'{tid}: invalid or duplicate action ID {aid}')
            action_ids.add(aid)
            if action['status'] not in STATES:
                errors.append(f'{aid}: invalid status')
            if action['status'] == 'DONE':
                if not action['evidence'] or not all(exists_safe(p) for p in action['evidence']):
                    errors.append(f'{aid}: DONE requires existing local evidence files')
        if task['status'] == 'BLOCKED' and not task['blockers']:
            errors.append(f'{tid}: BLOCKED requires a recorded blocker')
        if task['status'] == 'DONE':
            if any(by_id.get(dep, {}).get('status') != 'DONE' for dep in task['depends_on']):
                errors.append(f'{tid}: unfinished dependencies')
            if any(a['status'] != 'DONE' for a in task['actions']):
                errors.append(f'{tid}: unfinished actions')
            if not task['completed_at'] or not task['test_results'] or not exists_safe(task['required_report']):
                errors.append(f'{tid}: DONE requires completion date, verification results and report')
    visiting, visited = set(), set()

    def visit(tid):
        if tid in visiting:
            errors.append(f'Dependency cycle at {tid}')
            return
        if tid in visited or tid not in by_id:
            return
        visiting.add(tid)
        for dep in by_id[tid]['depends_on']:
            visit(dep)
        visiting.remove(tid)
        visited.add(tid)

    for tid in by_id:
        visit(tid)
    plans = packages['plans']
    if len(plans) != 1:
        errors.append('Owner decision requires exactly one subscription plan')
    if packages['currency'] != 'usd' or packages['billing_interval'] != 'month' or not packages['prices_decided']:
        errors.append('Owner decision requires USD monthly billing with decided price')
    for plan in plans:
        if plan['monthly_price_minor'] != 1000:
            errors.append('Owner decision requires 1000 USD cents monthly')
    for plan in plans:
        price = plan['monthly_price_minor']
        if not packages['prices_decided'] and price is not None:
            errors.append('Undecided prices must remain null')
        if packages['prices_decided'] and (not isinstance(price, int) or isinstance(price, bool) or price <= 0):
            errors.append('Decided prices must be positive integer minor units')
    if packages['prices_decided'] and not packages['currency']:
        errors.append('Currency required when prices are decided')
    required = ['README.md', 'PLAN.md', 'PACKAGES.md', 'ARCHITECTURE.md', 'CAPACITY.md',
                'STATUS.md', 'HANDOFF.md', 'PROGRESS.md', 'BASELINE.json', 'templates/STEP_REPORT.md',
                'DESIGN_AND_DECISIONS.md', 'GROWTH.md', 'PRODUCT_PRESERVATION.md']
    for filename in required:
        if not exists_safe(filename):
            errors.append(f'Missing file: {filename}')
    for doc in ROOT.glob('*.md'):
        for target in re.findall(r'\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
            if '://' in target or target.startswith('#'):
                continue
            if not exists_safe(target.split('#')[0].strip('<>')):
                errors.append(f'{doc.name}: broken local link {target}')
    result = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Plan consistency only; not SaaS tests, security certification or capacity validation',
              'tasks': len(tasks), 'actions': len(action_ids), 'packages': len(plans),
              'implementation_done': sum(t['status'] == 'DONE' for t in tasks),
              'passed': not errors, 'errors': errors}
    (ROOT / 'VALIDATION.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if errors else 0)


if __name__ == '__main__':
    main()
