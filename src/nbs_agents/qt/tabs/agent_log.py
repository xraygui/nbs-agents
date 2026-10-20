"""nbs-gui tab that shows the shared agent log written by every nbs-agents agent."""

from datetime import datetime

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...agentlog import AGENT_LOG_TOPIC, AgentLog

ALL_AGENTS = "__all__"
SESSION_COLUMNS = ["Agent", "Status", "Opened", "Updated", "Entries", "Goal"]
ENTRY_COLUMNS = ["Time", "Agent", "Kind", "Text", "Details"]
ENTRY_FIELDS = {"t", "kind", "text", "source"}
STEERABLE_STATUSES = ("opening", "active", "finalizing")


def format_time(t, with_date=False):
    """
    Format epoch seconds for display.

    Parameters
    ----------
    t : float or None
        Epoch seconds.
    with_date : bool, optional
        Include the date.

    Returns
    -------
    str
        Local time, or an empty string if ``t`` is missing.
    """
    if t is None:
        return ""
    try:
        fmt = "%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S"
        return datetime.fromtimestamp(float(t)).strftime(fmt)
    except (TypeError, ValueError, OverflowError):
        return str(t)


def short_uid(uid):
    """
    Shorten an agent UID for display.

    Parameters
    ----------
    uid : str
        Agent session UID.

    Returns
    -------
    str
        The first eight characters of ``uid``.
    """
    return str(uid)[:8]


def agent_label(uid, record):
    """
    Label an agent session as ``name (uid)``.

    Parameters
    ----------
    uid : str
        Agent session UID.
    record : dict
        Agent-log record.

    Returns
    -------
    str
        Display label.
    """
    return f"{record.get('agent_name') or '?'} ({short_uid(uid)})"


def entry_details(entry):
    """
    Format an entry's optional references.

    Parameters
    ----------
    entry : dict
        Agent-log entry.

    Returns
    -------
    str
        ``key=value`` pairs for every field other than the standard ones.
    """
    return ", ".join(f"{k}={v}" for k, v in entry.items() if k not in ENTRY_FIELDS)


def sorted_sessions(records):
    """
    Order agent sessions newest first.

    Parameters
    ----------
    records : dict
        Agent-log records keyed by UID.

    Returns
    -------
    list of tuple
        ``(uid, record)`` pairs sorted by last update, newest first.
    """
    return sorted(
        records.items(),
        key=lambda kv: kv[1].get("updated") or kv[1].get("opened") or 0,
        reverse=True,
    )


def collect_entries(records, uid=ALL_AGENTS):
    """
    Gather log entries for one session, or for every session merged by time.

    Parameters
    ----------
    records : dict
        Agent-log records keyed by UID.
    uid : str, optional
        Session UID, or ``ALL_AGENTS``.

    Returns
    -------
    list of tuple
        ``(label, entry)`` pairs in time order.
    """
    uids = list(records) if uid == ALL_AGENTS else [uid]
    out = []
    for u in uids:
        record = records.get(u)
        if not record:
            continue
        label = agent_label(u, record)
        for entry in record.get("log") or []:
            out.append((label, entry))
    out.sort(key=lambda pair: pair[1].get("t") or 0)
    return out


class AgentLogTab(QWidget):
    """
    Show every agent session in the shared Redis agent log and its entries.

    The left table lists sessions (newest first, with an "All agents" row that
    merges every log); the right table shows the selected session's entries
    and follows new ones as they arrive. Below it, an operator can send the
    selected running session a message or ask it to stop.

    Parameters
    ----------
    model : object
        nbs-gui model; its ``user_status`` provides the Redis connection.
    parent : QWidget, optional
        Parent widget.
    """

    name = "Agents Log"

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._selected = ALL_AGENTS
        self._records = None
        self._error = None
        try:
            self._records = model.user_status.get_redis_dict(AGENT_LOG_TOPIC)
        except Exception as e:  # Redis unreachable should not break the GUI
            self._error = str(e)

        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.sessions = QTableWidget(0, len(SESSION_COLUMNS), self)
        self.sessions.setHorizontalHeaderLabels(SESSION_COLUMNS)
        self.sessions.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sessions.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sessions.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sessions.verticalHeader().setVisible(False)
        self.sessions.horizontalHeader().setStretchLastSection(True)
        self.sessions.itemSelectionChanged.connect(self._on_session_selected)

        self.entries = QTableWidget(0, len(ENTRY_COLUMNS), self)
        self.entries.setHorizontalHeaderLabels(ENTRY_COLUMNS)
        self.entries.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.entries.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.entries.setWordWrap(True)
        self.entries.verticalHeader().setVisible(False)
        header = self.entries.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(ENTRY_COLUMNS.index("Text"), QHeaderView.Stretch)
        details = ENTRY_COLUMNS.index("Details")
        header.setSectionResizeMode(details, QHeaderView.Interactive)
        self.entries.setColumnWidth(details, 260)

        self.message_edit = QLineEdit(self)
        self.message_edit.setPlaceholderText("Message to the selected agent")
        self.message_edit.returnPressed.connect(self.send_message)
        self.send_button = QPushButton("Send", self)
        self.send_button.clicked.connect(self.send_message)
        self.stop_button = QPushButton("Request stop", self)
        self.stop_button.setToolTip("Ask the agent to stop adding scans and finalize")
        self.stop_button.clicked.connect(self.request_stop)
        steer = QHBoxLayout()
        steer.addWidget(self.message_edit)
        steer.addWidget(self.send_button)
        steer.addWidget(self.stop_button)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.summary)
        right_layout.addWidget(self.entries)
        right_layout.addLayout(steer)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self.sessions)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        if self._records is not None:
            self._records.changed.connect(self.refresh)
        self.refresh()

    def _writer(self):
        records = self._records
        return AgentLog(records._redis, records._prefix, AGENT_LOG_TOPIC)

    def _selected_record(self):
        if self._records is None or self._selected == ALL_AGENTS:
            return None
        return self._snapshot().get(self._selected)

    def _update_controls(self):
        record = self._selected_record()
        steerable = record is not None and record.get("status") in STEERABLE_STATUSES
        self.message_edit.setEnabled(steerable)
        self.send_button.setEnabled(steerable)
        self.stop_button.setEnabled(steerable and not record.get("stop_requested"))

    def send_message(self):
        """Send the typed message to the selected agent session."""
        text = self.message_edit.text().strip()
        if not text or self._selected_record() is None:
            return
        try:
            self._writer().message(self._selected, text, source="gui")
        except Exception as e:
            QMessageBox.warning(self, "Agents Log", f"Could not send message: {e}")
            return
        self.message_edit.clear()

    def request_stop(self, confirm=True):
        """Ask the selected agent session to stop adding work and finalize."""
        record = self._selected_record()
        if record is None:
            return
        if confirm:
            answer = QMessageBox.question(
                self,
                "Request stop",
                f"Ask {agent_label(self._selected, record)} to stop adding scans and finalize?",
            )
            if answer != QMessageBox.Yes:
                return
        text = self.message_edit.text().strip()
        try:
            self._writer().request_stop(self._selected, text, source="gui")
        except Exception as e:
            QMessageBox.warning(self, "Agents Log", f"Could not request stop: {e}")
            return
        self.message_edit.clear()

    def _snapshot(self):
        if self._records is None:
            return {}
        return {k: v for k, v in self._records.items() if isinstance(v, dict)}

    def refresh(self):
        """Redraw both tables from the current Redis cache."""
        records = self._snapshot()
        if self._selected != ALL_AGENTS and self._selected not in records:
            self._selected = ALL_AGENTS
        self._fill_sessions(records)
        self._fill_entries(records)
        self._update_controls()

    def _fill_sessions(self, records):
        rows = [(ALL_AGENTS, None)] + sorted_sessions(records)
        self.sessions.blockSignals(True)
        self.sessions.setRowCount(len(rows))
        for row, (uid, record) in enumerate(rows):
            if record is None:
                total = sum(len(r.get("log") or []) for r in records.values())
                values = ["All agents", "", "", "", str(total), ""]
            else:
                values = [
                    agent_label(uid, record),
                    str(record.get("status", "")),
                    format_time(record.get("opened"), with_date=True),
                    format_time(record.get("updated")),
                    str(len(record.get("log") or [])),
                    str(record.get("goal") or ""),
                ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, uid)
                item.setToolTip(value)
                self.sessions.setItem(row, col, item)
            if uid == self._selected:
                self.sessions.selectRow(row)
        self.sessions.resizeColumnsToContents()
        self.sessions.blockSignals(False)

    def _fill_entries(self, records):
        if self._records is None:
            self.summary.setText(f"Agent log unavailable: {self._error or 'no Redis settings in beamline.toml'}")
            self.entries.setRowCount(0)
            return

        if self._selected == ALL_AGENTS:
            self.summary.setText(f"{len(records)} agent session(s) in {AGENT_LOG_TOPIC} log")
        else:
            record = records[self._selected]
            parts = [
                f"<b>{record.get('agent_name') or '?'}</b> {self._selected}",
                f"status: {record.get('status', '')}",
            ]
            if record.get("goal"):
                parts.append(f"goal: {record['goal']}")
            if record.get("budget"):
                parts.append(f"budget: {record['budget']}")
            if record.get("stop_requested"):
                parts.append(f"<b>stop requested</b>: {record['stop_requested'].get('text', '')}")
            if record.get("close_reason"):
                parts.append(f"closed: {record['close_reason']}")
            self.summary.setText("<br>".join(parts))

        bar = self.entries.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        pairs = collect_entries(records, self._selected)
        self.entries.setRowCount(len(pairs))
        for row, (label, entry) in enumerate(pairs):
            values = [
                format_time(entry.get("t")),
                label,
                str(entry.get("kind", "")),
                str(entry.get("text", "")),
                entry_details(entry),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.entries.setItem(row, col, item)
        self.entries.setColumnHidden(ENTRY_COLUMNS.index("Agent"), self._selected != ALL_AGENTS)
        self.entries.resizeRowsToContents()
        if at_bottom:
            self.entries.scrollToBottom()

    def _on_session_selected(self):
        items = self.sessions.selectedItems()
        if not items:
            return
        self._selected = items[0].data(Qt.UserRole)
        self._fill_entries(self._snapshot())
        self._update_controls()
        self.entries.scrollToBottom()
