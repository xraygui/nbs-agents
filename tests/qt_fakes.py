"""Qt stand-ins shared by the Qt tab tests."""

from qtpy.QtCore import QObject, Signal
from qtpy.QtWidgets import QApplication

_APP = None


def qapp():
    """Return the QApplication, keeping a reference so it is not garbage collected."""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP



class FakeRedisDict(QObject):
    changed = Signal()

    def __init__(self, data):
        super().__init__()
        self.data = data
        self._redis = None
        self._prefix = "bl-"

    def items(self):
        return self.data.items()


class FakeUserStatus:
    def __init__(self, data):
        self.redis_dict = FakeRedisDict(data)

    def get_redis_dict(self, topic):
        assert topic == "AGENT"
        return self.redis_dict


class FakeModel:
    def __init__(self, data):
        self.user_status = FakeUserStatus(data)


