"""In-memory stand-ins shared by the tests."""

import fnmatch


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self._queued = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def watch(self, key):
        pass

    def get(self, key):
        return self.store.get(key)

    def multi(self):
        self._queued = []

    def set(self, key, value):
        self._queued.append((key, value))

    def execute(self):
        for key, value in self._queued:
            self.store[key] = value.encode() if isinstance(value, str) else value


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value.encode() if isinstance(value, str) else value

    def scan_iter(self, match="*"):
        return [k.encode() for k in self.store if fnmatch.fnmatch(k, match)]

    def pipeline(self):
        return FakePipeline(self.store)
