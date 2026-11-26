"""Render the human checklist from TASKS.json; no application changes."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    tasks = json.loads((ROOT / 'TASKS.json').read_text(encoding='utf-8'))['tasks']
    lines = ['# Starea implementării', '',
             'Generat din TASKS.json. Modifică registrul JSON, apoi regenerează acest fișier.', '',
             'DONE cere dovezi și verificări; lista nu certifică funcționarea aplicației.', '',
             '| Pas | Fază | Stare | Dependențe |', '|---|---|---|---|']
    for task in tasks:
        lines.append(f"| {task['id']} — {task['title']} | {task['phase']} | {task['status']} | {', '.join(task['depends_on']) or '—'} |")
    for task in tasks:
        lines.extend(['', f"## {task['id']} — {task['title']}", '',
                      f"**Stare:** {task['status']} · **Dependențe:** {', '.join(task['depends_on']) or 'niciuna'}", '',
                      f"**Puncte de intrare:** {task['code_entry_points']}", '', '### Subpași', ''])
        for action in task['actions']:
            mark = 'x' if action['status'] == 'DONE' else ' '
            lines.append(f"- [{mark}] **{action['id']} [{action['status']}]** {action['description']}")
        lines.extend(['', '### Acceptare', ''])
        lines.extend(f'- {criterion}' for criterion in task['acceptance_criteria'])
        lines.extend(['', f"**Raport cerut:** `{task['required_report']}`", '',
                      f"**Următoarea acțiune:** {task['next_action'] or '—'}", '',
                      f"**Blocaje:** {'; '.join(task['blockers']) or 'Niciun blocaj înregistrat.'}"])
    (ROOT / 'STATUS.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"Rendered {len(tasks)} tasks, {sum(len(t['actions']) for t in tasks)} actions.")


if __name__ == '__main__':
    main()
