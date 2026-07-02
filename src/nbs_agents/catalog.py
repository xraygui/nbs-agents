import logging
from collections import defaultdict
from itertools import count
from typing import Any, Callable, Optional

import numpy as np

LOGGER = logging.getLogger(__name__)

class ZMQRun:
    """
    Minimal run model for documents received over ZMQ.

    Parameters
    ----------
    start : dict
        Bluesky start document for the run.
    """

    def __init__(self, start: dict[str, Any]) -> None:
        self.start = start
        self.stop: Optional[dict[str, Any]] = None
        self.descriptors: dict[str, dict[str, Any]] = {}
        self._data: dict[str, list[Any]] = defaultdict(list)

    @property
    def uid(self) -> str:
        """
        Run UID.

        Returns
        -------
        str
            UID from the start document.
        """
        return self.start["uid"]

    @property
    def scan_id(self) -> Optional[int]:
        """
        Bluesky scan ID.

        Returns
        -------
        int or None
            Scan ID from the start document, if present.
        """
        return self.start.get("scan_id")

    @property
    def plan_name(self) -> Optional[str]:
        """
        Plan name.

        Returns
        -------
        str or None
            Plan name from the start document, if present.
        """
        return self.start.get("plan_name")

    @property
    def keys(self) -> tuple[str, ...]:
        """
        Buffered data keys.

        Returns
        -------
        tuple of str
            Data keys seen in primary stream events.
        """
        return tuple(self._data)

    @property
    def finished(self) -> bool:
        """
        Whether a stop document has been received.

        Returns
        -------
        bool
            True if the run has a stop document.
        """
        return self.stop is not None

    @property
    def succeeded(self) -> bool:
        """
        Whether the run completed successfully.

        Returns
        -------
        bool
            True if the stop document reports success.
        """
        return self.stop is not None and self.stop.get("exit_status") == "success"

    def data(self, key: str) -> np.ndarray:
        """
        Return buffered data for a key.

        Parameters
        ----------
        key : str
            Data key to retrieve.

        Returns
        -------
        numpy.ndarray
            Buffered values for the requested key.
        """
        return np.asarray(self._data.get(key, []))

    def process_descriptor(self, doc: dict[str, Any]) -> None:
        """
        Store a descriptor document.

        Parameters
        ----------
        doc : dict
            Bluesky descriptor document.
        """
        self.descriptors[doc["uid"]] = doc

    def process_event(self, doc: dict[str, Any]) -> None:
        """
        Buffer data from an event document.

        Parameters
        ----------
        doc : dict
            Bluesky event document.
        """
        descriptor = self.descriptors.get(doc.get("descriptor"))
        if not self._is_primary_descriptor(descriptor):
            return

        data = doc.get("data", {})
        if "time" not in data:
            self._data["time"].append(doc.get("time", 0.0))

        for key, value in data.items():
            self._data[key].append(value)

    def process_event_page(self, doc: dict[str, Any]) -> None:
        """
        Buffer data from an event_page document.

        Parameters
        ----------
        doc : dict
            Bluesky event_page document.
        """
        descriptor = self.descriptors.get(doc.get("descriptor"))
        if not self._is_primary_descriptor(descriptor):
            return

        data = doc.get("data", {})
        if "time" not in data:
            self._data["time"].extend(doc.get("time", []))

        for key, values in data.items():
            self._data[key].extend(values)

    def process_stop(self, doc: dict[str, Any]) -> None:
        """
        Store the stop document.

        Parameters
        ----------
        doc : dict
            Bluesky stop document.
        """
        self.stop = doc

    def _is_primary_descriptor(self, descriptor: Optional[dict[str, Any]]) -> bool:
        """
        Check whether a descriptor belongs to the primary stream.

        Parameters
        ----------
        descriptor : dict or None
            Descriptor document to check.

        Returns
        -------
        bool
            True for primary stream descriptors.
        """
        return descriptor is not None and descriptor.get("name") == "primary"


class ZMQCatalog:
    """
    Minimal in-memory catalog populated by a ZMQ dispatcher.

    Parameters
    ----------
    dispatcher : object
        Dispatcher that emits Bluesky documents via ``subscribe``.
    """

    def __init__(self, dispatcher: Any) -> None:
        self.dispatcher = dispatcher
        self.runs: list[ZMQRun] = []
        self.run_map: dict[str, ZMQRun] = {}
        self.descriptor_to_run: dict[str, str] = {}
        self._stop_callbacks: dict[int, Callable[[ZMQRun], None]] = {}
        self._callback_tokens = count()
        self.dispatcher.subscribe(self.handle_document)

    def get_run(self, uid: str) -> ZMQRun:
        """
        Get a run by UID.

        Parameters
        ----------
        uid : str
            Run UID.

        Returns
        -------
        ZMQRun
            Matching run.
        """
        return self.run_map[uid]

    def get_runs(self) -> list[ZMQRun]:
        """
        Return all known runs.

        Returns
        -------
        list of ZMQRun
            Runs received by the catalog.
        """
        return list(self.runs)

    def subscribe_stop(self, callback: Callable[[ZMQRun], None]) -> int:
        """
        Register a run-stop callback.

        Parameters
        ----------
        callback : callable
            Callback that accepts a finished ``ZMQRun``.

        Returns
        -------
        int
            Token that can be used to unsubscribe.
        """
        token = next(self._callback_tokens)
        self._stop_callbacks[token] = callback
        return token

    def unsubscribe_stop(self, token: int) -> None:
        """
        Remove a run-stop callback.

        Parameters
        ----------
        token : int
            Token returned by ``subscribe_stop``.
        """
        self._stop_callbacks.pop(token, None)

    def handle_document(self, name: str, doc: dict[str, Any]) -> None:
        """
        Route a Bluesky document into the catalog.

        Parameters
        ----------
        name : str
            Document type name.
        doc : dict
            Bluesky document.
        """
        print(f"Received document: {name}")
        if name == "start":
            self._process_start(doc)
        elif name == "descriptor":
            self._process_descriptor(doc)
        elif name == "event":
            self._process_event(doc)
        elif name == "event_page":
            self._process_event_page(doc)
        elif name == "stop":
            self._process_stop(doc)
        else:
            LOGGER.debug("Ignoring unsupported document type %s", name)

    def _process_start(self, doc: dict[str, Any]) -> None:
        """
        Create a run from a start document.

        Parameters
        ----------
        doc : dict
            Bluesky start document.
        """
        run = ZMQRun(doc)
        self.runs.append(run)
        self.run_map[run.uid] = run
        LOGGER.info(
            "Started run %s scan_id=%s plan=%s",
            run.uid,
            run.scan_id,
            run.plan_name,
        )

    def _process_descriptor(self, doc: dict[str, Any]) -> None:
        """
        Route a descriptor document to its run.

        Parameters
        ----------
        doc : dict
            Bluesky descriptor document.
        """
        run_uid = doc.get("run_start")
        run = self.run_map.get(run_uid)
        if run is None:
            LOGGER.warning("Descriptor %s references unknown run %s", doc.get("uid"), run_uid)
            return

        run.process_descriptor(doc)
        self.descriptor_to_run[doc["uid"]] = run.uid

    def _process_event(self, doc: dict[str, Any]) -> None:
        """
        Route an event document to its run.

        Parameters
        ----------
        doc : dict
            Bluesky event document.
        """
        run = self._run_from_descriptor(doc)
        if run is not None:
            run.process_event(doc)

    def _process_event_page(self, doc: dict[str, Any]) -> None:
        """
        Route an event_page document to its run.

        Parameters
        ----------
        doc : dict
            Bluesky event_page document.
        """
        run = self._run_from_descriptor(doc)
        if run is not None:
            run.process_event_page(doc)

    def _process_stop(self, doc: dict[str, Any]) -> None:
        """
        Finalize a run from a stop document.

        Parameters
        ----------
        doc : dict
            Bluesky stop document.
        """
        run_uid = doc.get("run_start")
        run = self.run_map.get(run_uid)
        if run is None:
            LOGGER.warning("Stop document references unknown run %s", run_uid)
            return

        run.process_stop(doc)
        LOGGER.info(
            "Stopped run %s status=%s keys=%s",
            run.uid,
            doc.get("exit_status"),
            run.keys,
        )
        self._notify_stop(run)

    def _run_from_descriptor(self, doc: dict[str, Any]) -> Optional[ZMQRun]:
        """
        Get a run from an event-like document descriptor.

        Parameters
        ----------
        doc : dict
            Event or event_page document.

        Returns
        -------
        ZMQRun or None
            Matching run if known.
        """
        descriptor_uid = doc.get("descriptor")
        run_uid = self.descriptor_to_run.get(descriptor_uid)
        if run_uid is None:
            LOGGER.debug("Document references unknown descriptor %s", descriptor_uid)
            return None
        return self.run_map.get(run_uid)

    def _notify_stop(self, run: ZMQRun) -> None:
        """
        Notify subscribers that a run has stopped.

        Parameters
        ----------
        run : ZMQRun
            Finished run.
        """
        for callback in tuple(self._stop_callbacks.values()):
            try:
                callback(run)
            except Exception:
                LOGGER.exception("Run-stop callback failed for run %s", run.uid)
