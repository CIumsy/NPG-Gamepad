# One Euro Filter: speed-adaptive low-pass filter for noisy human input.
# Slow movements get heavy smoothing (no jitter); fast movements get little (no lag).
#
# Algorithm from: G. Casiez, N. Roussel, D. Vogel, "1 Euro Filter: A Simple
# Speed-based Low-pass Filter for Noisy Input in Interactive Systems", CHI 2012.
# https://gery.casiez.net/1euro/

import math


class _LowPass:
    def __init__(self):
        self.y = None

    def filter(self, x, alpha):
        self.y = x if self.y is None else alpha * x + (1.0 - alpha) * self.y
        return self.y


class OneEuroFilter:
    def __init__(self, rate_hz, min_cutoff=1.0, beta=0.0, d_cutoff=1.0):
        """
        rate_hz:    sample rate of the input
        min_cutoff: cutoff (Hz) when still — lower = smoother at rest
        beta:       how fast the cutoff rises with speed — higher = less lag
        d_cutoff:   cutoff (Hz) for the speed estimate
        """
        self.dt = 1.0 / rate_hz
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = _LowPass()
        self._dx = _LowPass()
        self._prev = None

    def _alpha(self, cutoff):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / self.dt)

    def reset(self):
        self._x = _LowPass()
        self._dx = _LowPass()
        self._prev = None

    def filter(self, x):
        # Speed is measured against the previous filtered value (as in the reference)
        dx = 0.0 if self._prev is None else (x - self._prev) / self.dt
        edx = self._dx.filter(dx, self._alpha(self.d_cutoff))
        cutoff = self.min_cutoff + self.beta * abs(edx)
        self._prev = self._x.filter(x, self._alpha(cutoff))
        return self._prev
