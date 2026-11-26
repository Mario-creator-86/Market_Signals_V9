"""
Regression tests for the 2026-09-08 English-only news rule (owner request:
"doar postarile in Engleza sunt acceptate, elimina orice alta limba").

Trigger: eToro serves posts translated to the caller's locale; a Spanish
post was pushed to Telegram as "BREAKING · HUT". Fix: Accept-Language pinned
to en in core/etoro_client.py, plus this detector as the enforcement layer
at news ingestion (market refresh, per-ticker refresh, hydration).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.news_tab_agent import _is_english_text


class TestForeignRejected:
    """Non-English text observed live in the eToro feed must be rejected."""

    def test_spanish(self):
        text = ("[COREA] LA IA ESTÁ PASANDO A SER INFRAESTRUCTURA PÚBLICA... "
                "Y EL MERCADO AÚN NO LO HA DIGERIDO")
        assert _is_english_text(text) is False

    def test_french(self):
        text = ("Bonjour à tous 👋: Le 1er septembre approche : c'est la rentrée, "
                "et elle démarrera avec les marchés")
        assert _is_english_text(text) is False

    def test_german(self):
        text = "Prüfen Sie die $HUT Charts, die ich auf @etoro bereitgestellt habe"
        assert _is_english_text(text) is False

    def test_cjk_mixed_title(self):
        # English-looking prefix, CJK body — the body is the content.
        text = "2026/7/20: $CRCL 漲8% 美軍連9日空襲伊朗 三大指數收黑(投組YTD -1.5%)"
        assert _is_english_text(text) is False

    def test_bold_unicode_spanish(self):
        # eToro posts use Mathematical Bold Unicode (𝗹𝗮); NFKC must fold it
        # before scoring.
        text = ("𝗦𝘁𝗲𝗿𝗹𝗶𝗻𝗴 𝟮/𝟮: 𝗹𝗮 𝘁𝗲𝘀𝗶𝘀 𝘆 𝗹𝗼𝘀 𝗽𝘂𝗻𝘁𝗼𝘀 𝗾𝘂𝗲 𝗵𝗮𝘆 𝗾𝘂𝗲 𝘃𝗶𝗴𝗶𝗹𝗮𝗿 "
                "Atención post para los mas curiosos")
        assert _is_english_text(text) is False


class TestEnglishAccepted:
    """Real English headlines from the live pool must pass untouched."""

    def test_plain_headlines(self):
        for text in (
            "Gold surges past $4,400 as the dollar weakens ahead of Fed",
            "Bernie Sanders Wants to Ban Superintelligence. What It Means for Microsoft",
            "If a Stock Market Crash Is Coming, Investors Will Want to Own This Dividend King",
            "The yen's sudden surge upsets the carry trade faithful",
            "Oil Nears $100 as Markets Assess Fed Rate Expectations and Iran Tensions",
        ):
            assert _is_english_text(text) is True, text

    def test_short_ticker_line_accepted(self):
        assert _is_english_text("HUT ramps") is True

    def test_english_with_one_foreign_name_accepted(self):
        # One accented name must not tip the balance.
        text = "José's restaurant chain gains after upgrade; the stock rallied hard"
        assert _is_english_text(text) is True

    def test_english_with_single_cjk_char_accepted(self):
        # An isolated CJK symbol (brand/quote) in English text is not a
        # foreign post.
        text = "Yuan watch: the 元 slides as traders price Fed hikes"
        assert _is_english_text(text) is True
