import json
import logging
import os
import threading
import copy
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("AnalysisStore")

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _resolve_data_file() -> str:
    """data/news_analysis.json, or the same file under MS_DATA_DIR when set."""
    data_dir = os.getenv("MS_DATA_DIR") or os.path.join(_PROJECT_ROOT, "data")
    return os.path.join(data_dir, "news_analysis.json")


DATA_FILE = _resolve_data_file()


class AnalysisStore:
    """
    Unified analysis persistence.

    Schema (v2):
    {
      "_meta": {"version": 2},
      "by_url": { "<url>": {record} },
      "by_article": { "<article_id>": [record, ...] },
      "narratives": { "<key>": {record} }
    }
    """

    def __init__(self, file_path: str = DATA_FILE):
        self.file_path = file_path
        self._tmp_path = f"{self.file_path}.tmp"
        self._backup_path = f"{self.file_path}.bak"
        self._cache: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._load()

    @staticmethod
    def _clone(value: Any) -> Any:
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    @staticmethod
    def _to_jsonable(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for k, v in value.items():
                out[str(k)] = AnalysisStore._to_jsonable(v)
            return out
        if isinstance(value, (list, tuple, set)):
            return [AnalysisStore._to_jsonable(v) for v in value]
        return str(value)

    def _snapshot_cache(self) -> Dict[str, Any]:
        return self._to_jsonable(self._clone(self._cache))

    def _ensure_schema(self) -> None:
        if not isinstance(self._cache, dict):
            self._cache = {}
        self._cache.setdefault("_meta", {"version": 2})
        self._cache.setdefault("by_url", {})
        self._cache.setdefault("by_article", {})
        self._cache.setdefault("narratives", {})

    @staticmethod
    def _parse_ts(ts: Optional[str]) -> datetime:
        if not ts:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _parse_json_best_effort(raw_text: str) -> Optional[Dict[str, Any]]:
        """
        Best-effort parser for partially corrupted JSON files.
        Handles common cases like trailing garbage after a valid object.
        """
        text = (raw_text or "").strip()
        if not text:
            return None

        # 1) Strict parse first.
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

        # 2) Parse first complete JSON document and ignore trailing bytes.
        try:
            decoder = json.JSONDecoder()
            parsed, end = decoder.raw_decode(text)
            if isinstance(parsed, dict):
                trailing = text[end:].strip()
                if trailing:
                    logger.warning("Analysis store recovered with trailing data trimmed (%s chars).", len(trailing))
                return parsed
        except Exception:
            pass

        # 3) Fallback: trim to the last closing brace and retry.
        last_brace = text.rfind("}")
        if last_brace > 0:
            candidate = text[: last_brace + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    logger.warning("Analysis store recovered by trimming trailing bytes.")
                    return parsed
            except Exception:
                pass

        return None

    def _load_json_file(self, path: str) -> tuple[Optional[Dict[str, Any]], bool]:
        """
        Return (parsed_dict, repaired) where repaired=True when best-effort
        recovery was needed.
        """
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()

        # Strict parse first (repaired=False).
        try:
            strict = json.loads(raw)
            return (strict if isinstance(strict, dict) else {}), False
        except Exception:
            pass

        recovered = self._parse_json_best_effort(raw)
        if isinstance(recovered, dict):
            return recovered, True
        return None, False

    def _load(self):
        """Load analysis data from disk and upgrade legacy format if needed."""
        with self._lock:
            if not os.path.exists(self.file_path):
                self._cache = {}
                self._ensure_schema()
                return

            loaded: Any = {}
            needs_resave = False
            try:
                loaded, repaired_primary = self._load_json_file(self.file_path)
                if loaded is None:
                    raise ValueError("invalid json structure")
                if repaired_primary:
                    logger.warning("Primary analysis store required recovery and will be repaired.")
                    self._quarantine_corrupt_file()
                    needs_resave = True
            except Exception as e:
                logger.error(f"Failed to load analysis store: {e}")
                self._quarantine_corrupt_file()
                loaded, fallback_repaired = self._load_fallback_store()
                if fallback_repaired:
                    logger.warning("Fallback analysis store required recovery and will be repaired.")
                needs_resave = True

            # Legacy format: top-level {url: {...}}
            if isinstance(loaded, dict) and "by_url" not in loaded and "by_article" not in loaded:
                upgraded = {"_meta": {"version": 2}, "by_url": {}, "by_article": {}, "narratives": {}}
                for k, v in loaded.items():
                    if not isinstance(v, dict):
                        continue
                    upgraded["by_url"][k] = {
                        "timestamp": v.get("timestamp", datetime.now(timezone.utc).isoformat()),
                        "model": v.get("model", "unknown"),
                        "analysis": v.get("analysis", {}),
                        "analysis_type": v.get("analysis_type", "deep"),
                        "url": k,
                    }
                self._cache = upgraded
                needs_resave = True
            else:
                self._cache = loaded if isinstance(loaded, dict) else {}

            self._ensure_schema()
            if needs_resave and isinstance(self._cache, dict) and self._cache:
                # Re-write after successful load so next start has a clean atomic snapshot.
                self._save()
            logger.info(
                "Loaded analysis store: %s urls, %s article keys",
                len(self._cache.get("by_url", {})),
                len(self._cache.get("by_article", {})),
            )

    def _quarantine_corrupt_file(self) -> None:
        try:
            if not os.path.exists(self.file_path):
                return
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            corrupt_path = f"{self.file_path}.corrupt.{ts}.json"
            shutil.copy2(self.file_path, corrupt_path)
            logger.warning("Corrupt analysis store was copied to %s", corrupt_path)
        except Exception as e:
            logger.warning("Failed to quarantine corrupt analysis store: %s", e)

    def _load_fallback_store(self) -> tuple[Dict[str, Any], bool]:
        for candidate in (self._backup_path, self._tmp_path):
            if not os.path.exists(candidate):
                continue
            try:
                recovered, repaired = self._load_json_file(candidate)
                if isinstance(recovered, dict):
                    logger.warning("Recovered analysis store from fallback: %s", candidate)
                    return recovered, repaired
            except Exception as e:
                logger.warning("Fallback analysis store is invalid (%s): %s", candidate, e)
        return {}, False

    def _save(self):
        """Save analysis data to disk atomically."""
        with self._lock:
            try:
                dir_path = os.path.dirname(self.file_path) or "."
                os.makedirs(dir_path, exist_ok=True)
                snapshot = self._snapshot_cache()

                fd, temp_path = tempfile.mkstemp(prefix="analysis_store_", suffix=".tmp", dir=dir_path)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(snapshot, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_path, self.file_path)
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)

                try:
                    shutil.copy2(self.file_path, self._backup_path)
                except Exception as be:
                    logger.warning("Failed to refresh analysis store backup: %s", be)
            except Exception as e:
                logger.error(f"Failed to save analysis store: {e}")

    def get_analysis(self, url: str) -> Optional[Dict]:
        """Legacy URL lookup."""
        if not url:
            return None
        with self._lock:
            self._ensure_schema()
            rec = self._cache["by_url"].get(url)
            return self._clone(rec) if isinstance(rec, dict) else None

    def has_analysis(self, url: str) -> bool:
        if not url:
            return False
        with self._lock:
            self._ensure_schema()
            return url in self._cache["by_url"]

    def save_analysis(
        self,
        url: str,
        analysis_data: Dict,
        model: str,
        article_id: Optional[str] = None,
        analysis_type: str = "deep",
    ):
        """Legacy save with optional article binding."""
        ts = datetime.now(timezone.utc).isoformat()
        record = {
            "timestamp": ts,
            "model": model or "unknown",
            "analysis": self._to_jsonable(self._clone(analysis_data or {})),
            "analysis_type": analysis_type or "deep",
        }
        if url:
            record["url"] = url
        if article_id:
            record["article_id"] = article_id

        with self._lock:
            self._ensure_schema()
            if url:
                self._cache["by_url"][url] = dict(record)
            if article_id:
                self._append_article_record(article_id, dict(record))
            self._save()

    def _append_article_record(self, article_id: str, record: Dict) -> None:
        records = self._cache["by_article"].setdefault(article_id, [])
        records.append(record)
        # Keep newest 30 records only.
        records.sort(key=lambda r: self._parse_ts(r.get("timestamp")), reverse=True)
        self._cache["by_article"][article_id] = records[:30]

    def save_article_analysis(
        self,
        article_id: str,
        analysis_data: Dict,
        model: str,
        analysis_type: str = "deep",
        url: Optional[str] = None,
    ) -> None:
        if not article_id:
            return
        self.save_analysis(
            url=url or "",
            analysis_data=analysis_data,
            model=model,
            article_id=article_id,
            analysis_type=analysis_type,
        )

    def get_article_analysis(
        self,
        article_id: str,
        analysis_type: Optional[str] = None,
        model: Optional[str] = None,
        max_age_seconds: Optional[int] = None,
    ) -> Optional[Dict]:
        if not article_id:
            return None

        with self._lock:
            self._ensure_schema()
            records: List[Dict] = self._cache["by_article"].get(article_id, []) or []
            if not records:
                return None

            now_utc = datetime.now(timezone.utc)
            for rec in records:
                if analysis_type and rec.get("analysis_type") != analysis_type:
                    continue
                if model and rec.get("model") != model:
                    continue
                if max_age_seconds is not None:
                    age = now_utc - self._parse_ts(rec.get("timestamp"))
                    if age.total_seconds() > max_age_seconds:
                        continue
                return self._clone(rec)
            return None

    def has_article_analysis(self, article_id: str, analysis_type: Optional[str] = None) -> bool:
        return self.get_article_analysis(article_id, analysis_type=analysis_type) is not None

    def save_narrative(self, key: str, narrative_data: Dict, model: str) -> None:
        if not key:
            return
        with self._lock:
            self._ensure_schema()
            self._cache["narratives"][key] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": model or "unknown",
                "analysis": self._to_jsonable(self._clone(narrative_data or {})),
            }
            self._save()

    def get_narrative(self, key: str, max_age_seconds: Optional[int] = None) -> Optional[Dict]:
        if not key:
            return None
        with self._lock:
            self._ensure_schema()
            rec = self._cache["narratives"].get(key)
            if not isinstance(rec, dict):
                return None
            if max_age_seconds is not None:
                age = datetime.now(timezone.utc) - self._parse_ts(rec.get("timestamp"))
                if age.total_seconds() > max_age_seconds:
                    return None
            return self._clone(rec)


# Global instance
analysis_store = AnalysisStore()
