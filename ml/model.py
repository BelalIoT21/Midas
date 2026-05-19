"""
ML trade predictor — RandomForest classifier trained on closed trade history.

Lifecycle:
  1. On first import: load model from ML_MODEL_PATH if it exists.
  2. After every trade closes: retrain() is called, rebuilding the model
     on all labeled trades (if >= ML_MIN_TRADES are available).
  3. Before entering a new trade: predict_win_prob(signal) → float 0.0–1.0.
     If below ML_SKIP_BELOW threshold, the trade is skipped.
"""
import logging
import pickle
import numpy as np
from pathlib import Path

logger = logging.getLogger(__name__)


class _TradePredictor:
    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self.clf    = None
        self.scaler = None
        self._n_samples = 0
        self._load()

    def _load(self) -> None:
        if not self.model_path.exists():
            return
        try:
            with open(self.model_path, "rb") as f:
                data = pickle.load(f)
            self.clf       = data["clf"]
            self.scaler    = data["scaler"]
            self._n_samples = data.get("n_samples", 0)
            logger.info(f"[ml] Model loaded — {self._n_samples} training samples")
        except Exception as e:
            logger.warning(f"[ml] Could not load model from {self.model_path}: {e}")

    def train(self, X: np.ndarray, y: np.ndarray) -> None:
        """Fit a fresh RandomForest on (X, y) and persist to disk."""
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.preprocessing import StandardScaler

        if len(X) < 5:
            return

        scaler = StandardScaler()
        X_s = scaler.fit_transform(X)

        clf = RandomForestClassifier(
            n_estimators=200,
            min_samples_leaf=2,
            max_features="sqrt",
            random_state=42,
            class_weight="balanced",
        )
        clf.fit(X_s, y)

        self.clf       = clf
        self.scaler    = scaler
        self._n_samples = len(X)

        try:
            with open(self.model_path, "wb") as f:
                pickle.dump({"clf": clf, "scaler": scaler, "n_samples": len(X)}, f)
            logger.info(f"[ml] Model retrained on {len(X)} samples, saved to {self.model_path}")
        except Exception as e:
            logger.warning(f"[ml] Could not save model: {e}")

    def predict_win_prob(self, features: np.ndarray) -> float:
        """Return probability of this trade winning. 0.5 when no model."""
        if self.clf is None or self.scaler is None:
            return 0.5
        try:
            X = self.scaler.transform(features.reshape(1, -1))
            proba = self.clf.predict_proba(X)[0]
            classes = list(self.clf.classes_)
            win_idx = classes.index(1) if 1 in classes else -1
            return float(proba[win_idx]) if win_idx >= 0 else 0.5
        except Exception as e:
            logger.debug(f"[ml] predict_win_prob error: {e}")
            return 0.5

    def is_ready(self) -> bool:
        return self.clf is not None

    def n_samples(self) -> int:
        return self._n_samples

    def feature_importances(self) -> dict[str, float]:
        if self.clf is None:
            return {}
        from ml.features import FEATURE_NAMES
        return dict(zip(FEATURE_NAMES, self.clf.feature_importances_))

    def top_factors(self, n: int = 5) -> list[tuple[str, float]]:
        """Return the top N most important features sorted by importance."""
        imp = self.feature_importances()
        return sorted(imp.items(), key=lambda x: x[1], reverse=True)[:n]


_predictor: _TradePredictor | None = None


def get_predictor() -> _TradePredictor:
    global _predictor
    if _predictor is None:
        from config import ML_MODEL_PATH
        _predictor = _TradePredictor(ML_MODEL_PATH)
    return _predictor


def retrain() -> bool:
    """
    Load all labeled trades, retrain if enough data.
    Returns True if retraining happened.
    """
    from config import ML_MIN_TRADES
    from ml.features import load_training_data

    X, y = load_training_data()
    if len(X) < ML_MIN_TRADES:
        logger.info(f"[ml] Only {len(X)} labeled trades — need {ML_MIN_TRADES} to train")
        return False

    wins   = int(y.sum())
    losses = len(y) - wins
    logger.info(f"[ml] Retraining on {len(X)} trades ({wins}W / {losses}L)")
    get_predictor().train(X, y)
    return True


def analyze_trade(signal, won: bool) -> str:
    """
    Return a human-readable analysis of why a trade won or lost,
    using ML feature importances as a guide.
    """
    predictor = get_predictor()
    if not predictor.is_ready():
        return ""

    outcome    = "✅ WIN" if won else "❌ LOSS"
    top        = predictor.top_factors(3)
    factor_str = "  |  ".join(f"{name}: {imp:.0%}" for name, imp in top)
    win_prob   = predictor.predict_win_prob_from_signal(signal) if hasattr(predictor, "predict_win_prob_from_signal") else 0.5

    lines = [
        f"🤖 <b>ML Analysis — {outcome}</b>",
        f"Pre-trade win probability: {win_prob:.0%}",
        f"Top factors: {factor_str}",
        f"Model trained on {predictor.n_samples()} trades",
    ]
    return "\n".join(lines)
