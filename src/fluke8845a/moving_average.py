"""Bounded, display-only moving average of individual acquired samples."""

from collections import deque


class MovingAverageTrace:
    def __init__(self, window: int = 20, capacity: int = 100_000):
        self.capacity = capacity
        self.window = window
        self.samples = deque(maxlen=capacity)
        self.values = deque(maxlen=capacity)
        self._recent = deque()
        self._sum = 0.0

    def _calculate(self, value: float, gap: bool) -> float:
        if gap:
            self._recent.clear()
            self._sum = 0.0
        self._recent.append(value)
        self._sum += value
        if len(self._recent) > self.window:
            self._sum -= self._recent.popleft()
        return self._sum / len(self._recent)

    def append(self, x: float, value: float, gap: bool = False) -> None:
        self.samples.append((x, value, gap))
        self.values.append(self._calculate(value, gap))

    def set_window(self, window: int) -> None:
        if window < 1 or window > self.capacity:
            raise ValueError("average window outside bounded sample cache")
        if window == self.window:
            return
        self.window = window
        self._recent.clear()
        self._sum = 0.0
        self.values.clear()
        for _x, value, gap in self.samples:
            self.values.append(self._calculate(value, gap))

    def clear(self) -> None:
        self.samples.clear()
        self.values.clear()
        self._recent.clear()
        self._sum = 0.0
