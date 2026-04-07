# Baseline Tracker for Horizontal EOG
# Tracks rolling mean of the signal to compute a slowly-adapting baseline.
# The deviation from this baseline is used to detect left/right eye movements.
# Reference: Extracted from NPG Lite Gaming firmware (BaselineTracker class)

class BaselineTracker:
    def __init__(self, buffer_size=256):
        self.buffer_size = buffer_size
        self.buffer = [0.0] * buffer_size
        self.sum = 0.0
        self.idx = 0
        self.filled = False

    def update(self, sample):
        """Add a new sample to the rolling baseline window."""
        self.sum -= self.buffer[self.idx]
        self.sum += sample
        self.buffer[self.idx] = sample
        self.idx += 1
        if self.idx >= self.buffer_size:
            self.idx = 0
            self.filled = True

    def get_baseline(self):
        """Return the current rolling mean baseline."""
        if not self.filled and self.idx == 0:
            return 0.0
        count = self.buffer_size if self.filled else self.idx
        return (self.sum / count) if count > 0 else 0.0

    def reset(self):
        """Reset all state."""
        self.buffer = [0.0] * self.buffer_size
        self.sum = 0.0
        self.idx = 0
        self.filled = False


# Example usage:
# Signal chain for horizontal EOG: Raw -> Notch -> HP(1Hz) -> LP(10Hz) -> BaselineTracker
#
# baseline = BaselineTracker(256)   # ~0.5s window at 500Hz
# for sample in filtered_eog_samples:
#     baseline.update(sample)
#     deviation = sample - baseline.get_baseline()
#     if deviation > threshold:
#         print("LEFT eye movement")
#     elif deviation < -threshold:
#         print("RIGHT eye movement")
