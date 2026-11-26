
import re
import logging
from typing import Dict, List, Tuple, Optional
from enum import Enum

logger = logging.getLogger("SentimentEngine")

class NewsSentiment(Enum):
    VERY_BULLISH = "very_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    VERY_BEARISH = "very_bearish"

class SentimentEngine:
    """
    Advanced Sentiment Analysis Engine for Financial News.
    Supports:
    - Whole word matching (Regex)
    - Negation handling (Context awareness)
    - Compound phrases ("beats expectations")
    - Weighted scoring
    """

    # Negation words that flip or nullify sentiment
    NEGATORS = {
        "removes", "eliminated", "eliminates", "avoids", "prevented", "prevents",
        "no", "not", "lack", "without", "resolved", "fix"
    }

    # Lexicon for sentiment scoring
    # Order matters for phrases: Longer phrases should be checked before single words if possible,
    # but our regex approach handles this by matching patterns.
    LEXICON = {
        # --- PHRASES (Higher Priority via Regex) ---
        "phrases": [
            # Bullish Phrases
            (r"beats? expectations?", 1.5),
            (r"beats? estimates?", 1.5),
            (r"record high", 2.0),
            (r"all-time high", 2.0),
            (r"price target raised", 1.5),
            (r"upgrade", 1.5),
            (r"buy rating", 1.5),
            (r"strong buy", 2.0),
            (r"double upgrade", 2.5),
            (r"positive outlook", 1.0),
            (r"dividend increase", 1.2),
            (r"share buyback", 1.5),
            (r"removes? .* risk", 1.0), # Contextual fix for "Removes ... Risk"
            (r"buyers? (step|back) in", 1.5),
            (r"buyers? return", 1.5),
            (r"dip buying", 1.5),
            (r"buying the dip", 1.5),

            # Bearish Phrases
            (r"misses? expectations?", -1.5),
            (r"misses? estimates?", -1.5),
            (r"cuts? guidance", -1.5),
            (r"record low", -2.0),
            (r"all-time low", -2.0),
            (r"price target lowered", -1.5),
            (r"downgrade", -1.5),
            (r"sell rating", -1.5),
            (r"strong sell", -2.0),
            (r"negative outlook", -1.0),
            (r"dividend cut", -1.5),
            (r"regulatory probe", -1.5),
            (r"shares fall", -1.0),
            (r"shares drop", -1.0),
        ],

        # --- SINGLE KEYWORDS (Fallback) ---
        "very_bullish": {
            "keywords": ["surge", "soar", "skyrocket", "breakthrough", "blockbuster", "explode", "moon"],
            "weight": 2.0
        },
        "bullish": {
            "keywords": ["rise", "gain", "up", "positive", "growth", "profit", "beat", "success", 
                        "strong", "outperform", "rally", "momentum", "breakout", "approve", "win", "jump"],
            "weight": 1.0
        },
        "bearish": {
            "keywords": ["fall", "drop", "down", "negative", "loss", "miss", "cut", "weak", 
                        "underperform", "decline", "concern", "warning", "risk", "fail", "fear", "uncertainty"],
            "weight": -1.0
        },
        "very_bearish": {
            "keywords": ["crash", "plunge", "collapse", "disaster", "bankrupt", "fraud", "scandal", 
                        "halt", "suspend", "layoff", "default", "crisis", "panic"],
            "weight": -2.0
        }
    }

    @classmethod
    def analyze(cls, text: str) -> Tuple[NewsSentiment, float, float]:
        """
        Analyze text and return (sentiment, score, confidence).
        """
        if not text:
            return NewsSentiment.NEUTRAL, 0.0, 0.0

        text_lower = text.lower()
        score = 0.0
        matches = 0
        matched_tokens = set()

        # 1. Check Phrases (Regex) first
        # These are specific patterns that carry strong meaning
        for pattern, weight in cls.LEXICON["phrases"]:
            # Use regex to find pattern
            found = re.search(pattern, text_lower)
            if found:
                score += weight
                matches += 1
                # Mark these matched parts to potentially avoid double counting (simplistic approach: just count match)
                # Ideally, we'd remove the matched phrase from text_lower, but let's see if additive score is fine.
                # For "Removes Risk", if we match regex "removes .* risk", we add +1.0.
                # Later, if we match "risk" separately as -1.0, we get 0.0 (Neutral), which is better than -2.0.
                # BUT, let's implement true negation handling for single keywords.
                
                # To prevent double counting "risk" in "removes critical risk", we can remove the match from text.
                text_lower = text_lower.replace(found.group(0), " ") # Replace with space

        # 2. Check Single Keywords with Context/Negation
        # Split text into words for positional context checking
        words = re.findall(r'\b\w+\b', text_lower)
        
        for category, data in cls.LEXICON.items():
            if category == "phrases": continue
            
            weight = data["weight"]
            keywords = data["keywords"]
            
            for kw in keywords:
                # Use regex for whole word matching (\b)
                # This prevents "down" matching "downside"
                # Pattern: \bkeyword\b
                if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                    
                    # Search for negators in the vicinity (3 words before)
                    # Find index of keyword in words list
                    try:
                        # Find all occurrences
                        indices = [i for i, x in enumerate(words) if x == kw]
                        for idx in indices:
                            # Check window [max(0, idx-3) : idx]
                            window = words[max(0, idx-3):idx]
                            is_negated = any(neg in cls.NEGATORS for neg in window)
                            
                            if is_negated:
                                # Negation Logic:
                                # If Bearish + Negation ("no risk") -> Small Bullish (+0.5) or Neutral (0)
                                # If Bullish + Negation ("no growth") -> Small Bearish (-0.5) or Neutral (0)
                                
                                if weight < 0:
                                    score += 0.5 # Removes risk -> Mildly Positive
                                else:
                                    score -= 0.5 # No growth -> Mildly Negative
                                matches += 1
                                # logger.debug(f"Negated keyword found: '{kw}' in '{' '.join(window)} {kw}'")
                            else:
                                score += weight
                                matches += 1
                                
                    except Exception as e:
                        # Fallback for simple match if word processing fails
                        score += weight
                        matches += 1

        # 3. Calculate Confidence
        # Simple heuristic: more matches relative to text length (capped)
        if matches > 0:
            confidence = min(0.9, 0.3 + (matches * 0.15))
        else:
            confidence = 0.1

        # 4. Determine Sentiment Class
        if score >= 1.5:
            sentiment = NewsSentiment.VERY_BULLISH
        elif score >= 0.5:
            sentiment = NewsSentiment.BULLISH
        elif score <= -1.5:
            sentiment = NewsSentiment.VERY_BEARISH
        elif score <= -0.5:
            sentiment = NewsSentiment.BEARISH
        else:
            sentiment = NewsSentiment.NEUTRAL

        return sentiment, score, float(confidence)
