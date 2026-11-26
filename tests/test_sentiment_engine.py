
import unittest
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from core.sentiment import SentimentEngine, NewsSentiment

class TestSentimentEngine(unittest.TestCase):
    
    def test_removes_risk(self):
        """Test the specific case reported by user."""
        text = "US Jobs Data Removes Critical Downside Risk for Bitcoin"
        sentiment, score, conf = SentimentEngine.analyze(text)
        
        print(f"\n[Test Case 1] '{text}' -> {sentiment.value} (Score: {score})")
        
        # Should NOT be VERY_BEARISH (-1.5)
        # "Removes ... risk" phrase gives +1.0
        # "Downside" doesn't match single word "down" regex
        self.assertNotEqual(sentiment, NewsSentiment.VERY_BEARISH)
        self.assertGreater(score, -0.5) # Should be Neutral or Bullish
        
    def test_negation_simple(self):
        text = "No risk involved in this trade."
        sentiment, score, conf = SentimentEngine.analyze(text)
        print(f"\n[Test Case 2] '{text}' -> {sentiment.value} (Score: {score})")
        
        # "risk" (-1.0) negated by "no" -> +0.5
        self.assertGreater(score, 0) 
        
    def test_whole_word_matching(self):
        text = "The downside is limited."
        # "downside" contains "down" (-1.0). 
        # Whole word regex should ignore "downside" if not explicitly in lexicon.
        # "limited" is neutral.
        sentiment, score, conf = SentimentEngine.analyze(text)
        print(f"\n[Test Case 3] '{text}' -> {sentiment.value} (Score: {score})")
        
        self.assertEqual(score, 0.0) # Should be neutral, assuming 'downside' isn't a keyword
        
    def test_compound_phrase(self):
        text = "Company beats expectations with record high revenue."
        # "beats expectations" -> +1.5
        # "record high" -> +2.0
        # Total -> +3.5
        sentiment, score, conf = SentimentEngine.analyze(text)
        print(f"\n[Test Case 4] '{text}' -> {sentiment.value} (Score: {score})")
        
        self.assertEqual(sentiment, NewsSentiment.VERY_BULLISH)
        
    def test_bearish_case(self):
        text = "Shares drop after CEO resigns amid scandal."
        # "drop" -> -1.0
        # "scandal" -> -2.0
        # Total -> -3.0
        sentiment, score, conf = SentimentEngine.analyze(text)
        print(f"\n[Test Case 5] '{text}' -> {sentiment.value} (Score: {score})")
        
        self.assertEqual(sentiment, NewsSentiment.VERY_BEARISH)

if __name__ == "__main__":
    unittest.main()
