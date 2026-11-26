"""Provision and verify the immutable Phase 10 FinBERT model cache."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import DEFAULT_CONFIG
from core.news_v9.finbert import FinBERTAnalyzer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="Allow Hugging Face to download the pinned model when it is not cached.",
    )
    args = parser.parse_args()
    config = DEFAULT_CONFIG["news_v9"]["finbert"]
    analyzer = FinBERTAnalyzer(
        model=config["model"],
        revision=config["revision"],
        local_files_only=not args.allow_download,
        max_chars=config["max_chars"],
    )
    smoke = analyzer.analyze("Revenue beat expectations and guidance increased.")
    if not smoke.available:
        print(f"FinBERT unavailable: {smoke.error}")
        return 1
    print(
        f"FinBERT ready: model={smoke.model} revision={smoke.revision} "
        f"label={smoke.label} confidence={smoke.confidence:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
