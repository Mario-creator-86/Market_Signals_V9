import json
from pathlib import Path

try:
    from PIL import Image, ImageChops
except Exception as e:
    raise SystemExit(f"PIL not available: {e}")

BASE = Path("output/ui_audit_baseline")
CURR = Path("output/ui_audit")
DIFF = Path("output/ui_audit_diff")
DIFF.mkdir(parents=True, exist_ok=True)

report = {
    "baseline": str(BASE),
    "current": str(CURR),
    "diff_dir": str(DIFF),
    "files": [],
}

for img_path in sorted(CURR.glob("*.png")):
    name = img_path.name
    base_path = BASE / name
    entry = {"name": name, "status": "ok"}
    if not base_path.exists():
        entry["status"] = "missing_baseline"
        report["files"].append(entry)
        continue

    try:
        a = Image.open(base_path).convert("RGB")
        b = Image.open(img_path).convert("RGB")
        if a.size != b.size:
            entry["status"] = "size_mismatch"
            entry["baseline_size"] = a.size
            entry["current_size"] = b.size
            report["files"].append(entry)
            continue

        diff = ImageChops.difference(a, b)
        # Count non-zero pixels
        diff_data = diff.getdata()
        total = len(diff_data)
        non_zero = sum(1 for px in diff_data if px != (0, 0, 0))
        pct = (non_zero / total) * 100 if total else 0.0
        entry["diff_pixels"] = non_zero
        entry["diff_pct"] = round(pct, 4)

        # Save diff image (amplify for visibility)
        if non_zero > 0:
            # scale diff for visibility
            amplified = diff.point(lambda x: min(255, x * 4))
            amplified.save(DIFF / name)
            entry["diff_image"] = str(DIFF / name)
        report["files"].append(entry)
    except Exception as e:
        entry["status"] = "error"
        entry["error"] = str(e)
        report["files"].append(entry)

with open(DIFF / "report.json", "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)

print(f"Diff report written to {DIFF / 'report.json'}")
