import pytest

from core.news_v9.finbert import (
    FinancialSentimentService,
    FinBERTAnalyzer,
)


MODEL = "ProsusAI/finbert"
REVISION = "4556d13015211d73dccd3fdd39d39232506f3e43"


@pytest.mark.parametrize(
    ("raw", "label", "score"),
    [
        ({"label": "positive", "score": 0.91}, "positive", 0.91),
        ([{"label": "negative", "score": 0.80}], "negative", -0.80),
        ({"label": "neutral", "score": 0.75}, "neutral", 0.0),
    ],
)
def test_finbert_maps_classifier_labels_to_signed_scores(raw, label, score):
    analyzer = FinBERTAnalyzer(
        model=MODEL, revision=REVISION, classifier=lambda text, **kwargs: raw,
    )
    result = analyzer.analyze("financial statement")
    assert result.available is True
    assert result.label == label
    assert result.score == pytest.approx(score)
    assert result.model == MODEL and result.revision == REVISION


def test_finbert_rejects_unrecognized_or_invalid_classifier_output():
    analyzer = FinBERTAnalyzer(
        model=MODEL, revision=REVISION,
        classifier=lambda text, **kwargs: {"label": "BUY_NOW", "score": 2.0},
    )
    result = analyzer.analyze("x")
    assert result.available is False
    assert "unsupported FinBERT label" in result.error


def test_service_labels_lexicon_fallback_and_does_not_call_it_finbert():
    unavailable = FinBERTAnalyzer(
        model=MODEL, revision=REVISION,
        classifier=lambda text, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    result = FinancialSentimentService(unavailable).analyze("Company beats expectations")
    assert result.available is True
    assert result.fallback_used is True
    assert result.source == "sentiment_lexicon"
    assert "offline" in result.error


def test_model_and_revision_are_mandatory():
    with pytest.raises(ValueError, match="immutable revision"):
        FinBERTAnalyzer(model=MODEL, revision="")


def test_the_model_runs_on_one_thread_whichever_thread_asks():
    """Seen live on 2026-11-24: torch's OpenMP runtime starts a team of worker threads for every
    new thread that runs the model and never gives it back. News batches ask from short-lived
    threads, so the server went from 114 threads to 5,112 in 12 hours (34 more per new asker).
    """
    import threading

    from core.news_v9.finbert import FinBERTAnalyzer

    ran_on, built_on = [], []

    def classifier(text, truncation=True):
        ran_on.append(threading.get_ident())
        return [{"label": "positive", "score": 0.9}]

    analyzer = FinBERTAnalyzer(model="ProsusAI/finbert", revision="0123456789abcdef", classifier=classifier)
    answers = []

    def ask():
        answers.append(analyzer.analyze("Guidance raised").label)

    askers = [threading.Thread(target=ask) for _ in range(6)]
    for thread in askers:
        thread.start()
    for thread in askers:
        thread.join()
    ask()                                             # and once from the main thread

    assert answers == ["positive"] * 7
    assert len(ran_on) == 7 and len(set(ran_on)) == 1, "every answer came from the same model thread"
    assert ran_on[0] != threading.get_ident(), "the asking thread never runs the model itself"

    # Loading the model is torch work too: it happens on the same thread.
    def build():
        built_on.append(threading.get_ident())
        return classifier

    lazy = FinBERTAnalyzer(model="ProsusAI/finbert", revision="0123456789abcdef")
    lazy._build_classifier = build
    assert lazy.analyze("Guidance raised").label == "positive"
    assert built_on == [ran_on[0]] and ran_on[-1] == ran_on[0]


def test_a_failing_model_is_still_reported_as_unavailable_not_raised():
    from core.news_v9.finbert import FinBERTAnalyzer

    def broken(text, truncation=True):
        raise RuntimeError("model crashed")

    result = FinBERTAnalyzer(model="ProsusAI/finbert", revision="0123456789abcdef", classifier=broken).analyze("x")

    assert result.available is False and "model crashed" in result.error
