import time
import unittest
from app.models import PairSnapshot
from app.scoring import score_token

def snap(**kw):
    now = int(time.time() * 1000)
    base = dict(
        chain="solana", token_address="ABC", token_name="Rocket Cat", token_symbol="RCAT",
        pair_address="PAIR", dex_id="raydium", url="https://example.com",
        market_cap=1_500_000, fdv=1_500_000, liquidity_usd=120_000,
        pair_created_at_ms=now - 25*60*1000, price_usd=0.0015,
        price_change_m5=18, price_change_h1=55, volume_m5=110_000, volume_h1=650_000,
        buys_m5=130, sells_m5=45, buys_h1=900, sells_h1=370,
        socials_count=2, boosts_active=0, source="test", raw={}
    )
    base.update(kw)
    return PairSnapshot(**base)

class ScoreTests(unittest.TestCase):
    def test_strong_candidate_scores_high(self):
        previous = {"volume_m5": 50_000, "buys_m5": 70}
        r = score_token(snap(), previous=previous, security={})
        self.assertGreaterEqual(r.score, 85)
        self.assertFalse(r.hard_block)

    def test_low_liquidity_blocks(self):
        r = score_token(snap(liquidity_usd=2_000), previous=None)
        self.assertTrue(r.hard_block)

    def test_honeypot_blocks(self):
        r = score_token(snap(), security={"honeypot": True})
        self.assertTrue(r.hard_block)

if __name__ == "__main__":
    unittest.main()
