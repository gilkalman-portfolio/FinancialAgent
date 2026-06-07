"""
Unit Tests for Meme-Squeeze Sentinel

Run with: pytest tests/test_meme_squeeze.py -v
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.meme_squeeze_sentinel import (
    MemeSqueezeSentinel,
    SqueezeDatabase,
    SqueezeScore,
    WEIGHTS,
    THRESHOLDS
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_score():
    """Create a mock SqueezeScore for testing"""
    return SqueezeScore(
        ticker='TEST',
        timestamp=datetime.now(),
        explosion_score=75.5,
        squeeze_factor=80.0,
        social_velocity=70.0,
        volume_confirmation=75.0,
        technical_trigger=50.0,
        short_interest_pct=25.5,
        days_to_cover=3.2,
        reddit_mentions_4h=150,
        reddit_mentions_24h=400,
        rvol=4.5,
        price=50.25,
        ema_20=48.50,
        high_5d=51.00,
        institutional_ownership=45.0,
        high_inst_risk=False,
        bull_trap_detected=False,
        google_trend_spike=True,
        catalyst="High SI | Reddit buzz",
        stop_loss_suggestion=46.23
    )


@pytest.fixture
def temp_db(tmp_path):
    """Create temporary database for testing"""
    db_path = tmp_path / "test_squeeze.db"
    return SqueezeDatabase(db_path)


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSqueezeDatabase:
    """Test database operations"""
    
    def test_database_creation(self, temp_db):
        """Test database schema creation"""
        assert temp_db.db_path.exists()
    
    def test_insert_score(self, temp_db, mock_score):
        """Test inserting a score"""
        temp_db.insert_score(mock_score)
        
        results = temp_db.get_top_scores(limit=1)
        assert len(results) == 1
        assert results[0]['ticker'] == 'TEST'
        assert results[0]['explosion_score'] == 75.5
    
    def test_get_top_scores_filtering(self, temp_db, mock_score):
        """Test filtering by minimum score"""
        temp_db.insert_score(mock_score)
        
        # Should return result (score = 75.5 > 60)
        results = temp_db.get_top_scores(min_score=60.0)
        assert len(results) == 1
        
        # Should return nothing (score = 75.5 < 80)
        results = temp_db.get_top_scores(min_score=80.0)
        assert len(results) == 0


# ══════════════════════════════════════════════════════════════════════════════
# SCORING TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestExplosionScore:
    """Test explosion score calculation logic"""
    
    def test_score_weights_sum_to_one(self):
        """Verify weights add up to 1.0"""
        total = sum(WEIGHTS.values())
        assert abs(total - 1.0) < 0.01  # Allow small floating point error
    
    def test_squeeze_factor_threshold(self):
        """Test that squeeze factor requires minimum SI"""
        assert THRESHOLDS['min_short_interest'] > 0
        assert THRESHOLDS['min_short_interest'] <= 100
    
    def test_rvol_threshold(self):
        """Test RVOL threshold is reasonable"""
        assert THRESHOLDS['min_rvol'] >= 1.0
        assert THRESHOLDS['min_rvol'] <= 10.0


# ══════════════════════════════════════════════════════════════════════════════
# SENTINEL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMemeSqueezeSentinel:
    """Test main Sentinel class"""
    
    def test_initialization_with_watchlist(self):
        """Test sentinel initialization with custom watchlist"""
        watchlist = ['GME', 'AMC', 'IONQ']
        sentinel = MemeSqueezeSentinel(watchlist=watchlist, scan_russell=False)
        
        assert sentinel.watchlist == watchlist
        assert sentinel.scan_russell == False
    
    def test_get_scan_list_watchlist_only(self):
        """Test scan list without Russell 2000"""
        watchlist = ['TEST1', 'TEST2']
        sentinel = MemeSqueezeSentinel(watchlist=watchlist, scan_russell=False)
        
        scan_list = sentinel._get_scan_list()
        assert len(scan_list) == 2
        assert 'TEST1' in scan_list
        assert 'TEST2' in scan_list
    
    def test_catalyst_generation(self):
        """Test catalyst string generation"""
        sentinel = MemeSqueezeSentinel(watchlist=['TEST'], scan_russell=False)
        
        catalyst = sentinel._generate_catalyst(
            short_interest=30.0,
            mentions=100,
            rvol=6.0,
            above_ema=True,
            trend_spike=True
        )
        
        assert 'High SI' in catalyst
        assert 'Reddit buzz' in catalyst
        assert 'Volume surge' in catalyst
        assert 'Above 20 EMA' in catalyst
        assert 'Google trend spike' in catalyst


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """Integration tests (require internet + API keys)"""
    
    @pytest.mark.skipif(
        not Path('.env').exists(),
        reason="Requires .env file with API credentials"
    )
    def test_analyze_real_ticker(self):
        """Test analyzing a real ticker (requires yfinance)"""
        sentinel = MemeSqueezeSentinel(watchlist=['AAPL'], scan_russell=False)
        score = sentinel.calculate_explosion_score('AAPL')
        
        # AAPL should return data (even if score is low)
        assert score is not None
        assert score.ticker == 'AAPL'
        assert score.price > 0
        assert 0 <= score.explosion_score <= 100


# ══════════════════════════════════════════════════════════════════════════════
# DATA MODEL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSqueezeScore:
    """Test SqueezeScore dataclass"""
    
    def test_to_dict_conversion(self, mock_score):
        """Test converting score to dictionary"""
        score_dict = mock_score.to_dict()
        
        assert isinstance(score_dict, dict)
        assert score_dict['ticker'] == 'TEST'
        assert score_dict['explosion_score'] == 75.5
        assert score_dict['short_interest_pct'] == 25.5
    
    def test_score_bounds(self, mock_score):
        """Test score is within valid range"""
        assert 0 <= mock_score.explosion_score <= 100
        assert 0 <= mock_score.squeeze_factor <= 100
        assert 0 <= mock_score.social_velocity <= 100
        assert 0 <= mock_score.volume_confirmation <= 100
        assert 0 <= mock_score.technical_trigger <= 100


# ══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, '-v'])
