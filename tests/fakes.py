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

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    def scan_iter(self, match="*"):
        return [k.encode() for k in self.store if fnmatch.fnmatch(k, match)]

    def pipeline(self):
        return FakePipeline(self.store)


from nbs_agents.beamline_info import BEAM_MAP, DEVICES, ESTIMATES, SAMPLES, XAS_PLANS, BeamlineInfo  # noqa: E402


class StaticInfo(BeamlineInfo):
    """A beamline-info provider with fixed answers; capabilities follow what it is given."""

    name = "static"

    def __init__(self, beam_map=None, device_info=None, samples=None, estimate=None, xas_plans=None, warning=None):
        super().__init__(None)
        self._beam_map, self._device_info, self._samples = beam_map, device_info, samples
        self._estimate, self._xas_plans, self._warning = estimate, xas_plans, warning
        given = {BEAM_MAP: beam_map, DEVICES: device_info, SAMPLES: samples, ESTIMATES: estimate, XAS_PLANS: xas_plans}
        self.capabilities = frozenset(name for name, value in given.items() if value is not None)

    def beam_map(self):
        return dict(self._beam_map) if self._beam_map else None

    def device_info(self, names=None):
        return dict(self._device_info or {})

    def samples(self):
        return self._samples

    def estimator(self):
        return self._estimate

    def xas_plans(self):
        return dict(self._xas_plans or {})

    def add_warning(self, plan, kwargs, parameters, block):
        return self._warning
