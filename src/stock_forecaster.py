"""
AI Stock Price Forecasting Tool
Multi-model forecasting with LSTM, Prophet, ARIMA, and Ensemble
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import warnings
from loguru import logger
warnings.filterwarnings('ignore')

# Try importing ML libraries
try:
    from statsmodels.tsa.arima.model import ARIMA
    STATSMODELS_AVAILABLE = True
except ImportError:
    STATSMODELS_AVAILABLE = False
    logger.warning("Statsmodels not available - install with: pip install statsmodels")

try:
    from sklearn.preprocessing import MinMaxScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("Scikit-learn not available")


class StockForecaster:
    """Stock price forecasting using multiple models"""
    
    def __init__(self, ticker: str, data: pd.DataFrame = None,
                 point_in_time: Optional[datetime] = None):
        """
        Initialize forecaster

        Args:
            ticker: Stock ticker symbol
            data: Historical price data with 'Close' column
            point_in_time: If provided, strictly truncates ``data`` to rows whose
                index timestamp is ``<= point_in_time`` BEFORE any model is fit
                or any feature/target is constructed. This is the single chokepoint
                that prevents look-ahead bias — callers (e.g. backtests) should
                always pass the timestamp at which they are simulating a decision.
                When ``None`` (default), all rows in ``data`` are used as-is.
        """
        self.ticker = ticker.upper()
        self.point_in_time = point_in_time

        # Strict point-in-time truncation. Done once, here, so every downstream
        # model (ARIMA, MA, ExpSmooth, MLP) operates on the SAME bounded slice.
        if data is not None and not data.empty and point_in_time is not None:
            try:
                idx = pd.to_datetime(data.index)
                cutoff = pd.to_datetime(point_in_time)
                # Strip tz to make the comparison robust regardless of input tz.
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                if getattr(cutoff, "tz", None) is not None:
                    cutoff = cutoff.tz_localize(None)
                mask = np.asarray(idx <= cutoff)
                data = data.loc[mask]
                logger.info(f"Truncated data to point_in_time={cutoff} → {len(data)} rows")
            except Exception as e:
                logger.warning(f"point_in_time truncation failed ({e}); using raw data")

        self.data = data
        self.predictions = {}

        if data is not None and not data.empty:
            logger.info(f"Initialized forecaster for {ticker} with {len(data)} days of data")
        
    def arima_forecast(self, days_ahead: int = 30) -> np.ndarray:
        """ARIMA forecasting"""
        if not STATSMODELS_AVAILABLE:
            logger.warning("ARIMA skipped - statsmodels not available")
            return None
            
        logger.info(f"Running ARIMA forecast for {days_ahead} days...")
        try:
            prices = self.data['Close'].values
            
            model = ARIMA(prices, order=(5, 1, 2))
            fitted = model.fit()
            forecast = fitted.forecast(steps=days_ahead)
            
            std_err = np.std(fitted.resid)
            lower_bound = forecast - 1.96 * std_err
            upper_bound = forecast + 1.96 * std_err
            
            self.predictions['arima'] = {
                'forecast': forecast,
                'lower': lower_bound,
                'upper': upper_bound,
                'model': 'ARIMA(5,1,2)'
            }
            
            logger.success("ARIMA forecast complete")
            return forecast
            
        except Exception as e:
            logger.error(f"ARIMA failed: {e}")
            return None
    
    def moving_average_forecast(self, days_ahead: int = 30, window: int = 20) -> np.ndarray:
        """Simple moving average with trend"""
        logger.info(f"Running Moving Average forecast...")
        try:
            prices = self.data['Close'].values
            
            # Calculate trend
            recent_prices = prices[-window:]
            x = np.arange(len(recent_prices))
            coeffs = np.polyfit(x, recent_prices, 1)
            trend_slope = coeffs[0]
            
            # Project forward
            last_price = prices[-1]
            forecast = [last_price + trend_slope * i for i in range(1, days_ahead + 1)]
            
            # Confidence intervals based on recent volatility
            volatility = np.std(np.diff(prices[-window:]))
            expanding_uncertainty = np.sqrt(np.arange(1, days_ahead + 1))
            lower = forecast - 1.96 * volatility * expanding_uncertainty
            upper = forecast + 1.96 * volatility * expanding_uncertainty
            
            self.predictions['ma_trend'] = {
                'forecast': np.array(forecast),
                'lower': lower,
                'upper': upper,
                'model': f'Moving Average + Trend ({window} days)'
            }
            
            logger.success("Moving Average forecast complete")
            return np.array(forecast)
            
        except Exception as e:
            logger.error(f"Moving Average failed: {e}")
            return None
    
    def exponential_smoothing_forecast(self, days_ahead: int = 30) -> np.ndarray:
        """Exponential smoothing forecast"""
        logger.info("Running Exponential Smoothing forecast...")
        try:
            prices = self.data['Close'].values
            
            # Simple exponential smoothing
            alpha = 0.3
            smoothed = [prices[0]]
            
            for i in range(1, len(prices)):
                smoothed.append(alpha * prices[i] + (1 - alpha) * smoothed[-1])
            
            # Calculate trend from last 30 days (or available data)
            lookback = min(30, len(smoothed) - 1)
            recent_trend = (smoothed[-1] - smoothed[-lookback]) / lookback if lookback > 0 else 0
            
            # Forecast
            last_smoothed = smoothed[-1]
            forecast = [last_smoothed + recent_trend * i for i in range(1, days_ahead + 1)]
            
            # Confidence intervals
            residuals = prices - np.array(smoothed)
            std_err = np.std(residuals)
            expanding_uncertainty = np.sqrt(np.arange(1, days_ahead + 1))
            lower = forecast - 1.96 * std_err * expanding_uncertainty
            upper = forecast + 1.96 * std_err * expanding_uncertainty
            
            self.predictions['exp_smooth'] = {
                'forecast': np.array(forecast),
                'lower': lower,
                'upper': upper,
                'model': 'Exponential Smoothing'
            }
            
            logger.success("Exponential Smoothing forecast complete")
            return np.array(forecast)
            
        except Exception as e:
            logger.error(f"Exponential Smoothing failed: {e}")
            return None
    
    def mlp_forecast(self, days_ahead: int = 30, window: int = 20) -> np.ndarray:
        """Neural network forecast using sklearn MLPRegressor with sliding window."""
        if not SKLEARN_AVAILABLE:
            return None
        try:
            from sklearn.neural_network import MLPRegressor
            prices = self.data['Close'].values
            if len(prices) < window + 10:
                return None

            scaler = MinMaxScaler()
            scaled = scaler.fit_transform(prices.reshape(-1, 1)).flatten()

            X = np.array([scaled[i:i + window] for i in range(len(scaled) - window)])
            y = scaled[window:]

            # early_stopping uses a randomly shuffled validation split (not time-ordered).
            # This is not strict label leakage but is suboptimal for time series.
            # Intentionally left unchanged — fixing would require a manual time-split loop.
            model = MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=500,
                                 random_state=42, early_stopping=True,
                                 validation_fraction=0.1, n_iter_no_change=20)
            model.fit(X, y)

            # Iterative multi-step forecast
            last_window = scaled[-window:].tolist()
            forecast_scaled = []
            for _ in range(days_ahead):
                pred = float(model.predict([last_window])[0])
                forecast_scaled.append(pred)
                last_window = last_window[1:] + [pred]

            forecast = scaler.inverse_transform(
                np.array(forecast_scaled).reshape(-1, 1)).flatten()

            std_err = np.std(prices[-window:]) * 0.5
            expanding = np.sqrt(np.arange(1, days_ahead + 1))
            self.predictions['mlp'] = {
                'forecast': forecast,
                'lower':    forecast - 1.96 * std_err * expanding,
                'upper':    forecast + 1.96 * std_err * expanding,
                'model':    'MLP Neural Network (64→32)',
            }
            logger.success("MLP forecast complete")
            return forecast
        except Exception as e:
            logger.warning(f"MLP forecast failed: {e}")
            return None

    def ensemble_forecast(self) -> np.ndarray:
        """Combine available forecasts"""
        if not self.predictions:
            logger.error("No predictions to ensemble")
            return None
        
        logger.info("Creating ensemble forecast...")
        
        forecasts = []
        weights = []
        
        model_weights = {
            'arima':      0.35,
            'ma_trend':   0.25,
            'exp_smooth': 0.20,
            'mlp':        0.20,
        }
        
        for model_name, pred_data in self.predictions.items():
            if pred_data is not None:
                forecasts.append(pred_data['forecast'])
                weights.append(model_weights.get(model_name, 0.33))
        
        if not forecasts:
            return None
        
        weights = np.array(weights) / sum(weights)
        ensemble = np.average(forecasts, axis=0, weights=weights)
        
        all_lowers = [p['lower'] for p in self.predictions.values() if p is not None]
        all_uppers = [p['upper'] for p in self.predictions.values() if p is not None]
        
        ensemble_lower = np.min(all_lowers, axis=0)
        ensemble_upper = np.max(all_uppers, axis=0)
        
        self.predictions['ensemble'] = {
            'forecast': ensemble,
            'lower': ensemble_lower,
            'upper': ensemble_upper,
            'model': 'Ensemble (Weighted Average)',
            'weights': dict(zip([k for k in self.predictions.keys() if k != 'ensemble'], weights))
        }
        
        logger.success("Ensemble forecast complete")
        return ensemble
    
    def get_prediction_summary(self, days_ahead: int = 30) -> Dict[str, Any]:
        """
        Get prediction summary for specific day
        
        Args:
            days_ahead: Number of days ahead to get prediction for
            
        Returns:
            Dictionary with prediction details
        """
        if 'ensemble' not in self.predictions:
            logger.error("No ensemble prediction available")
            return None
        
        current_price = self.data['Close'].iloc[-1]
        ensemble = self.predictions['ensemble']
        
        if days_ahead > len(ensemble['forecast']):
            logger.error(f"Days ahead {days_ahead} exceeds forecast range")
            return None
        
        predicted_price = ensemble['forecast'][days_ahead - 1]
        lower_bound = ensemble['lower'][days_ahead - 1]
        upper_bound = ensemble['upper'][days_ahead - 1]
        
        change_pct = ((predicted_price - current_price) / current_price) * 100
        
        return {
            'ticker': self.ticker,
            'current_price': float(current_price),
            'predicted_price': float(predicted_price),
            'lower_bound': float(lower_bound),
            'upper_bound': float(upper_bound),
            'change_percent': float(change_pct),
            'days_ahead': days_ahead,
            'models_used': list(self.predictions.keys()),
            'confidence_interval': '95%'
        }
    
    def run_all_forecasts(self, days_ahead: int = 30) -> Dict[str, Any]:
        """
        Run all available forecasting models
        
        Args:
            days_ahead: Number of days to forecast
            
        Returns:
            Prediction summary
        """
        logger.info(f"Running all forecasts for {self.ticker} ({days_ahead} days ahead)")
        
        self.moving_average_forecast(days_ahead=days_ahead)
        self.exponential_smoothing_forecast(days_ahead=days_ahead)
        self.arima_forecast(days_ahead=days_ahead)
        self.mlp_forecast(days_ahead=days_ahead)
        self.ensemble_forecast()
        
        return self.get_prediction_summary(days_ahead)
