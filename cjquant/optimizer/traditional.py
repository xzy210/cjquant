import numpy as np
import pandas as pd
from scipy.optimize import minimize
from .base import BaseOptimizer
from .utils import calculate_shrunk_covariance

class RiskParityOptimizer(BaseOptimizer):
    def __init__(self, returns: pd.DataFrame):
        super().__init__(returns)
        self.cov = calculate_shrunk_covariance(returns)

    def _risk_contribution(self, w, cov):
        w = np.matrix(w)
        sigma = np.sqrt(w * cov * w.T)
        # 边际风险贡献 (MRC)
        mrc = (cov * w.T) / sigma
        # 风险贡献 (RC)
        rc = np.multiply(mrc, w.T)
        return rc

    def _objective(self, w, cov):
        num_assets = len(w)
        rc = self._risk_contribution(w, cov)
        total_rc = np.sum(rc)
        if total_rc <= 0:
            return 1e6
        # Use percentage risk contributions so the optimizer works on an
        # O(1) objective instead of tiny daily-covariance units.
        pct_rc = rc / total_rc
        target_rc = 1.0 / num_assets
        risk_diffs = np.sum(np.square(pct_rc - target_rc))
        return float(risk_diffs)

    def optimize(self, constraints: list = None) -> pd.Series:
        initial_w = np.array([1.0 / self.num_assets] * self.num_assets)
        bounds = [(0, 1) for _ in range(self.num_assets)]
        
        cons = ({'type': 'eq', 'fun': lambda x: np.sum(x) - 1.0})
        
        res = minimize(
            self._objective, 
            initial_w, 
            args=(self.cov.values,),
            method='SLSQP',
            constraints=cons,
            bounds=bounds,
            options={'ftol': 1e-12, 'maxiter': 1000}
        )
        
        return self._validate_weights(res.x)

class MeanVarianceOptimizer(BaseOptimizer):
    def __init__(self, returns: pd.DataFrame, risk_free_rate: float = 0.02):
        super().__init__(returns)
        self.cov = calculate_shrunk_covariance(returns)
        self.exp_rets = returns.mean() * 252
        self.rf = risk_free_rate

    def _objective(self, w):
        port_ret = np.sum(self.exp_rets * w)
        port_vol = np.sqrt(np.dot(w.T, np.dot(self.cov * 252, w)))
        sharpe = (port_ret - self.rf) / port_vol
        return -sharpe # 最小化负夏普

    def optimize(self) -> pd.Series:
        cons = ({'type': 'eq', 'fun': lambda x: np.sum(x) - 1.0})
        bounds = [(0, 1) for _ in range(self.num_assets)]
        res = minimize(self._objective, [1./self.num_assets]*self.num_assets, 
                       method='SLSQP', bounds=bounds, constraints=cons)
        return self._validate_weights(res.x)