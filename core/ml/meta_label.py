"""§56 Meta-labeling — evaluate. Primary model + meta filter -> final label.
Spec example (§56): primary LONG, meta DO_NOT_EXECUTE -> final NO_TRADE is valid."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence, Tuple


_PRIMARY_LONG = "LONG"
_PRIMARY_SHORT = "SHORT"
_META_EXECUTE = "EXECUTE"
_META_DO_NOT_EXECUTE = "DO_NOT_EXECUTE"
_META_INSUFFICIENT = "INSUFFICIENT"
_FINAL_NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class MetaLabel:
    primary: str
    meta: str
    final: str


class MetaLabeler:
    @staticmethod
    def combine(primary: str, meta: str) -> str:
        p = str(primary or "").upper()
        m = str(meta or "").upper()
        if m in (_META_DO_NOT_EXECUTE, _META_INSUFFICIENT):
            return _FINAL_NO_TRADE
        if m == _META_EXECUTE:
            return p if p in (_PRIMARY_LONG, _PRIMARY_SHORT) else _FINAL_NO_TRADE
        return _FINAL_NO_TRADE  # unknown meta -> no-trade (fail-safe)


@dataclass(frozen=True)
class MetaLabelEvaluation:
    primary_precision: float
    primary_recall: float
    meta_precision: float
    meta_recall: float
    sample_size: int
    meta_labeling_improves_precision: bool


def evaluate_meta_labeling(primary_predictions: Sequence[str], meta_predictions: Sequence[str],
                           outcomes: Sequence[str], *, positive_label: str = "LONG") -> MetaLabelEvaluation:
    """§56 'evaluate meta-labeling' — does the meta filter improve precision
    vs primary-only? outcomes are realized labels (LONG/SHORT/NO_TRADE)."""
    if not (len(primary_predictions) == len(meta_predictions) == len(outcomes)):
        raise ValueError("length mismatch")
    n = len(outcomes)

    def metrics(preds: Sequence[str]) -> Tuple[float, float]:
        tp = fp = fn = 0
        for p, o in zip(preds, outcomes):
            o_pos = str(o).upper() == positive_label
            p_pos = str(p).upper() == positive_label
            if p_pos and o_pos:
                tp += 1
            elif p_pos and not o_pos:
                fp += 1
            elif (not p_pos) and o_pos:
                fn += 1
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        return prec, rec

    pp, pr = metrics(primary_predictions)
    combined = [MetaLabeler.combine(p, m)
                for p, m in zip(primary_predictions, meta_predictions)]
    mp, mr = metrics(combined)
    return MetaLabelEvaluation(
        primary_precision=round(pp, 4), primary_recall=round(pr, 4),
        meta_precision=round(mp, 4), meta_recall=round(mr, 4), sample_size=n,
        meta_labeling_improves_precision=mp > pp,
    )