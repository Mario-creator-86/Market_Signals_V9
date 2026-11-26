"""
Script to verify commodity keyword detection with simulated titles.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_keyword_detection():
    print("🚀 Testing Keyword Detection Logic...")
    
    test_titles = [
        "Gold price soars to new record as US Federal Reserve faces fresh threats",
        "Stock Market Today: Dow Falls 300 Points On Powell Probe; Nvidia, Tesla Slide",
        "Silver futures rally as investors seek safe haven",
        "Oil prices surge amid Middle East tensions",
        "Bitcoin drops 5% as crypto market corrects",
        "Gold surges to $2,800 on inflation fears",
    ]
    
    for title in test_titles:
        title_lower = title.lower()
        detected = None
        is_market = True
        
        if any(kw in title_lower for kw in ["gold price", "gold futures", "gold soars", "gold drops", "gold record", "precious metal", "xau", "gold surges"]):
            detected = "GOLD"
            is_market = False
        elif any(kw in title_lower for kw in ["silver price", "silver futures", "silver soars", "silver drops", "silver rally"]):
            detected = "SILVER"
            is_market = False
        elif any(kw in title_lower for kw in ["oil price", "crude oil", "wti crude", "brent crude", "oil futures", "oil prices"]):
            detected = "CL=F"
            is_market = False
        
        status = f"✅ {detected}" if detected else "📰 MARKET"
        print(f"{status} | {title[:60]}...")
        
    print("\n✅ Test complete!")

if __name__ == "__main__":
    test_keyword_detection()
