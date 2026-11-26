import copy
import math
import re
import uuid
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from core.config import ConfigManager

logger = logging.getLogger("AlertManager")

class AlertType:
    PRICE_UP = "PRICE_UP"
    PRICE_DOWN = "PRICE_DOWN"
    VOLATILITY = "VOLATILITY"

VALID_ALERT_TYPES = (AlertType.PRICE_UP, AlertType.PRICE_DOWN, AlertType.VOLATILITY)
# A daily move threshold above this can never be reached in practice; it is
# almost always a price typed into the percent field.
MAX_VOLATILITY_THRESHOLD_PCT = 1000.0
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,19}$")


def validate_alert_input(ticker, alert_type, target) -> Tuple[str, str, float]:
    """Normalize and validate one alert request; raise ValueError when the
    alert could never fire or would fire on nonsense.

    check_alerts() silently ignores unknown types and compares against the
    raw target, so an alert with type "PRICE_ABOVE", a NaN/negative price or
    a non-positive percent used to be stored and shown as "Active" forever."""
    ticker_u = str(ticker or "").upper().strip()
    if not ticker_u or not _TICKER_RE.match(ticker_u):
        raise ValueError("Enter a valid ticker symbol (e.g. AAPL or BTC-USD).")
    type_u = str(alert_type or "").upper().strip()
    if type_u not in VALID_ALERT_TYPES:
        raise ValueError(
            "Unknown alert type. Use PRICE_UP, PRICE_DOWN or VOLATILITY."
        )
    try:
        value = float(target)
    except (TypeError, ValueError):
        raise ValueError("Target must be a number.")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Target must be a positive number.")
    if type_u == AlertType.VOLATILITY and value > MAX_VOLATILITY_THRESHOLD_PCT:
        raise ValueError(
            f"Volatility threshold is a daily move in percent (max {MAX_VOLATILITY_THRESHOLD_PCT:g}%)."
        )
    return ticker_u, type_u, value


def find_duplicate_alert(alerts, ticker, alert_type, target) -> Optional[dict]:
    """Return an existing ACTIVE alert with the same ticker, type and target."""
    ticker_u = str(ticker or "").upper().strip()
    for row in alerts or []:
        if not isinstance(row, dict) or not row.get("active", True):
            continue
        if str(row.get("ticker") or "").upper() != ticker_u or row.get("type") != alert_type:
            continue
        try:
            if math.isclose(float(row.get("target")), float(target), rel_tol=1e-9, abs_tol=1e-12):
                return row
        except (TypeError, ValueError):
            continue
    return None


class Alert:
    def __init__(self, ticker: str, alert_type: str, target_value: float, id: str = None, is_active: bool = True):
        self.id = id or str(uuid.uuid4())[:8]
        self.ticker = ticker.upper()
        self.alert_type = alert_type
        self.target_value = target_value
        self.is_active = is_active
        self.created_at = datetime.now().isoformat()
        # Set when the alert fires, cleared when it is re-armed. Lets the UI
        # tell "fired at $X on <date>" apart from "paused by the user".
        self.triggered_at: Optional[str] = None
        self.triggered_price: Optional[float] = None

    def to_dict(self):
        data = {
            "id": self.id,
            "ticker": self.ticker,
            "type": self.alert_type,
            "target": self.target_value,
            "active": self.is_active,
            "created_at": self.created_at
        }
        # Only present once fired, so untouched legacy rows stay byte-identical
        # for the cross-process merge in ConfigManager._save_watchlist.
        if self.triggered_at:
            data["triggered_at"] = self.triggered_at
            data["triggered_price"] = self.triggered_price
        return data

    @classmethod
    def from_dict(cls, data):
        alert = cls(
            ticker=data["ticker"],
            alert_type=data["type"],
            target_value=data["target"],
            id=data.get("id"),
            is_active=data.get("active", True)
        )
        if "created_at" in data:
            alert.created_at = data["created_at"]
        if data.get("triggered_at"):
            alert.triggered_at = str(data["triggered_at"])
            alert.triggered_price = data.get("triggered_price")
        return alert

class AlertManager:
    def __init__(self, config_manager: ConfigManager):
        self.config = config_manager
        self.alerts: List[Alert] = []
        self._load_alerts()

    def _load_alerts(self):
        """Load alerts from watchlist.json via ConfigManager"""
        raw_alerts = self.config.watchlist.get("alerts", [])
        loaded = []
        for a in raw_alerts if isinstance(raw_alerts, list) else []:
            try:
                loaded.append(Alert.from_dict(a))
            except Exception as exc:
                logger.warning(f"Skipping malformed alert entry {a!r}: {exc}")
        self.alerts = loaded
        self._persisted_alerts = copy.deepcopy(raw_alerts) if isinstance(raw_alerts, list) else []
        logger.info(f"Loaded {len(self.alerts)} alerts")

    def reload_from_config(self):
        """Re-read alerts from the watchlist config (call after the config's
        watchlist was refreshed from disk).

        The engine subprocess builds its AlertManager once at startup, but
        alerts created later go through the API process and only land on disk.
        Without this refresh the engine never fires the new alerts and its
        next _save_alerts() silently wipes them from watchlist.json."""
        try:
            self._load_alerts()
        except Exception as exc:
            logger.error(f"Alert reload failed, keeping current list: {exc}")

    def _save_alerts(self):
        """Save alerts to watchlist.json via ConfigManager"""
        applied = self.config._save_watchlist(
            alert_baseline=self._persisted_alerts,
            desired_alerts=[a.to_dict() for a in self.alerts],
        )
        self._load_alerts()
        return applied

    def add_alert(self, ticker: str, alert_type: str, target: float) -> Alert:
        alert = Alert(ticker, alert_type, target)
        self.alerts.append(alert)
        self._save_alerts()
        return alert

    def remove_alert(self, alert_id: str) -> bool:
        original_len = len(self.alerts)
        self.alerts = [a for a in self.alerts if a.id != alert_id]
        if len(self.alerts) < original_len:
            self._save_alerts()
            return True
        return False

    def toggle_alert(self, alert_id: str) -> Optional[bool]:
        for a in self.alerts:
            if a.id == alert_id:
                a.is_active = not a.is_active
                if a.is_active:
                    # Re-armed: the previous fire no longer describes it.
                    a.triggered_at = None
                    a.triggered_price = None
                self._save_alerts()
                return a.is_active
        return None

    def get_alerts(self, ticker: Optional[str] = None) -> List[dict]:
        if ticker:
            return [a.to_dict() for a in self.alerts if a.ticker == ticker.upper()]
        return [a.to_dict() for a in self.alerts]

    def check_alerts(self, live_prices: Dict[str, dict]) -> List[dict]:
        """
        Evaluate active alerts against current prices.
        Returns triggered alerts and deactivates them if needed.
        """
        triggered = []
        for alert in self.alerts:
            if not alert.is_active:
                continue
                
            price_data = live_prices.get(alert.ticker)
            if not price_data:
                continue
                
            if price_data.get("stale") is True or str(price_data.get("freshness", "")).lower() in {"stale", "expired", "unavailable"}:
                continue
            try:
                current_price = float(price_data.get("price"))
                if not math.isfinite(current_price) or current_price <= 0:
                    continue
            except (TypeError, ValueError):
                continue
                
            is_triggered = False
            if alert.alert_type == AlertType.PRICE_UP and current_price >= alert.target_value:
                is_triggered = True
            elif alert.alert_type == AlertType.PRICE_DOWN and current_price <= alert.target_value:
                is_triggered = True
            elif alert.alert_type == AlertType.VOLATILITY:
                # Volatility checks might need 24h change or short-term spikes
                # For now, let's use 24h change vs target (threshold)
                try:
                    change_pct = abs(float(price_data.get("change_pct")))
                    if not math.isfinite(change_pct):
                        continue
                except (TypeError, ValueError):
                    continue
                if change_pct >= alert.target_value:
                    is_triggered = True
            
            if is_triggered:
                alert.is_active = False # One-time trigger by default like KuCoin
                alert.triggered_at = datetime.now().isoformat()
                alert.triggered_price = current_price
                triggered.append({
                    "alert": alert.to_dict(),
                    "current_price": current_price,
                    "timestamp": datetime.now().isoformat()
                })
        
        if triggered:
            applied = self._save_alerts()
            triggered = [t for t in triggered if t["alert"]["id"] in applied]
            
        return triggered
