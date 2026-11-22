"""Trailing time window of samples (monotonic seconds)."""

from collections import deque


class Window:
    def __init__(self, seconds):
        self.seconds = seconds
        self._d = deque()

    def expire(self, now):
        """Drop samples older than `seconds` before `now` (a sample exactly that old is out)."""
        cutoff = now - self.seconds
        while self._d and self._d[0][0] <= cutoff:
            self._d.popleft()

    def add(self, now, value):
        self._d.append((now, value))
        self.expire(now)

    def mean(self):
        """Mean of the samples present (fewer while the window fills), or None when empty."""
        if not self._d:
            return None
        return sum(v for _, v in self._d) / len(self._d)

    def __len__(self):
        return len(self._d)
