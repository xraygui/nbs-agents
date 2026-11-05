"""nbs-gui tab for the read-only beamline assistant hosted by nbs-agents-runner."""

import html
import time

from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from nbs_agents.agentlog import AGENT_LOG_TOPIC, AgentLog, assistant_uid
from nbs_agents.richtext import plain_text
from nbs_agents.qt.agent_log_source import agent_log_dict
from nbs_agents.qt.tabs.agent_log import format_time

HEARTBEAT_STALE_S = 120.0
CLOCK_MS = 10000
RUN_LINK = "run:"

# kind -> (side, background, label); "right" is the operator's side.
POST_STYLES = {
    "summary": ("left", "#e3f2e6", "Summary"),
    "advice": ("left", "#dbe8fb", "Advice"),
    "alert": ("left", "#f9dcdb", "Alert"),
    "answer": ("left", "#ece3f7", "Answer"),
    "operator": ("right", "#eceff3", ""),
    "error": ("left", "#fff1cc", "Assistant error"),
}
ALERT_COLOR = "#c62828"
FLAG_COLOR = "#a05f00"


def assistant_state(record, now=None):
    """
    The assistant's state for the header.

    Parameters
    ----------
    record : dict or None
        Today's assistant record.
    now : float, optional
        Current time, for tests.

    Returns
    -------
    tuple of (str, str)
        ``(text, color)``.
    """
    if not record:
        return "Assistant: no record today (is the runner started with [assistant] enabled?)", "#a05f00"
    now = time.time() if now is None else now
    beat = record.get("heartbeat")
    if record.get("status") in ("stopped", "closed") or beat is None or now - float(beat) > HEARTBEAT_STALE_S:
        return "Assistant: not running", "#c62828"
    if record.get("status") == "calling":
        return "Assistant: thinking…", "#1565c0"
    return "Assistant: watching", "#2e7d32"


def call_summary(record):
    """
    One line on the assistant's calls today.

    Parameters
    ----------
    record : dict or None
        Today's assistant record.

    Returns
    -------
    str
        Plain text, empty without a record.
    """
    if not record:
        return ""
    text = f"{record.get('calls') or 0} call(s) today"
    last = record.get("last_call") or {}
    if last:
        text += f" · last at {format_time(last.get('t'))}"
        if last.get("reasons"):
            text += f" for {', '.join(last['reasons'])}"
        if last.get("model"):
            text += f" ({last['model']})"
        if last.get("returncode") not in (None, 0):
            text += f", exit {last['returncode']}"
    if record.get("pending"):
        text += f" · {record['pending']} event(s) waiting"
    return text


def run_links(uids):
    """Links for run UIDs, labelled by their first eight characters."""
    return ", ".join(
        f"<a href='{RUN_LINK}{html.escape(str(u))}'>{html.escape(str(u)[:8])}</a>" for u in uids or []
    )


def entry_html(entry):
    """
    Render one assistant-record entry.

    Parameters
    ----------
    entry : dict
        Agent-log entry.

    Returns
    -------
    str
        Qt rich-text HTML: run lines as one compact row (flags and alerts
        coloured), posts and operator questions as bubbles.
    """
    kind = entry.get("kind")
    when = format_time(entry.get("t"))
    text = html.escape(plain_text(str(entry.get("text", ""))))
    if kind == "run":
        uid = entry.get("run_uid")
        link = f" <a href='{RUN_LINK}{html.escape(str(uid))}'>{html.escape(str(uid)[:8])}</a>" if uid else ""
        line = f"<span style='color:#777777'>{when}</span> {text}{link}"
        for alert in entry.get("alerts") or []:
            line += f"<br>&nbsp;&nbsp;<b style='color:{ALERT_COLOR}'>alert:</b> {html.escape(alert)}"
        for flag in entry.get("flags") or []:
            line += f"<br>&nbsp;&nbsp;<b style='color:{FLAG_COLOR}'>flag:</b> {html.escape(flag)}"
        return f"<p style='margin:2px 0; font-size:small; color:#333333'>{line}</p>"
    side, background, tag = POST_STYLES.get(kind, ("left", "#eceff3", str(kind)))
    who = "Operator" if side == "right" else "Assistant"
    header = f"<b>{who}</b> &middot; {when}"
    if tag:
        header += f" &middot; <b style='color:#555555'>{html.escape(tag)}</b>"
    body = text.replace("\n", "<br>")
    if entry.get("run_uids"):
        body += f"<br><span style='font-size:small; color:#555555'>runs: {run_links(entry['run_uids'])}</span>"
    bubble = (
        f"<td width='85%' bgcolor='{background}' style='color:#1a1a1a'>"
        f"<span style='font-size:small; color:#555555'>{header}</span><br>{body}</td>"
    )
    spacer = "<td width='15%'></td>"
    cells = bubble + spacer if side == "left" else spacer + bubble
    return f"<table width='100%' cellspacing='0' cellpadding='8'><tr>{cells}</tr></table>"


def feed_html(record, show_runs=True):
    """
    Render today's assistant record.

    Parameters
    ----------
    record : dict or None
        Today's assistant record.
    show_runs : bool, optional
        Include the per-run lines.

    Returns
    -------
    str
        Qt rich-text HTML.
    """
    entries = [
        e for e in (record or {}).get("log") or []
        if show_runs or e.get("kind") != "run"
    ]
    if not entries:
        return "<p style='color:#777777'>Nothing from the assistant yet today.</p>"
    return "<div style='margin-top:4px'></div>".join(entry_html(e) for e in entries)


class AssistantTab(QWidget):
    """
    Show today's record of the read-only beamline assistant and let the
    operator ask it questions.

    The feed shows the assistant's summaries, advice, alerts and answers as
    distinct bubbles, the operator's questions, and (optionally) the one-line
    record of every run with its flags. Clicking a run link copies its UID.
    The assistant only informs: nothing here sends anything to a driving
    agent or the queue.

    Parameters
    ----------
    model : object
        nbs-gui model; its ``user_status`` provides the Redis connection.
    parent : QWidget, optional
        Parent widget.
    """

    name = "Assistant"
    run_selected = Signal(str)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._records = None
        self._error = None
        self._html = None
        self._uid = assistant_uid()
        try:
            self._records = agent_log_dict(model.user_status, self)
        except Exception as e:  # Redis unreachable should not break the GUI
            self._error = str(e)

        self.state_label = QLabel(self)
        self.calls_label = QLabel(self)
        self.calls_label.setStyleSheet("color: #555555")
        self.show_runs = QCheckBox("Show every run", self)
        self.show_runs.setChecked(True)
        self.show_runs.toggled.connect(self.refresh)
        header = QHBoxLayout()
        header.addWidget(self.state_label)
        header.addWidget(self.calls_label, 1)
        header.addWidget(self.show_runs)

        self.feed = QTextBrowser(self)
        self.feed.setOpenLinks(False)
        self.feed.anchorClicked.connect(self._on_link)
        self._follow = True
        bar = self.feed.verticalScrollBar()
        bar.actionTriggered.connect(lambda action: QTimer.singleShot(0, self._on_scrolled))
        bar.sliderReleased.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_range)

        self.question_edit = QLineEdit(self)
        self.question_edit.setPlaceholderText("Ask the assistant (it reads the data; it never changes the queue)")
        self.question_edit.returnPressed.connect(self.ask)
        self.ask_button = QPushButton("Ask", self)
        self.ask_button.clicked.connect(self.ask)
        ask_row = QHBoxLayout()
        ask_row.addWidget(self.question_edit)
        ask_row.addWidget(self.ask_button)

        self.status = QLabel(self)
        self.status.setStyleSheet("color: #555555")

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.feed, 1)
        layout.addLayout(ask_row)
        layout.addWidget(self.status)

        if self._records is not None:
            self._records.changed.connect(self.refresh)
        # The heartbeat ages and the day rolls over without a Redis change
        self._clock = QTimer(self)
        self._clock.timeout.connect(self.refresh)
        self._clock.start(CLOCK_MS)
        self.refresh()

    def _writer(self):
        records = self._records
        return AgentLog(records._redis, records._prefix, AGENT_LOG_TOPIC)

    def record(self):
        """
        Today's assistant record.

        Returns
        -------
        dict or None
            The record, or ``None`` if there is none yet.
        """
        if self._records is None:
            return None
        self._uid = assistant_uid()
        for uid, record in self._records.items():
            if uid == self._uid and isinstance(record, dict):
                return record
        return None

    def ask(self):
        """Post the typed question to the assistant's record."""
        text = self.question_edit.text().strip()
        if not text or self._records is None:
            return
        try:
            writer = self._writer()
            uid = writer.ensure_assistant_record()
            writer.message(uid, text, source="gui")
        except Exception as e:
            QMessageBox.warning(self, "Assistant", f"Could not send the question: {e}")
            return
        self.question_edit.clear()
        self._follow = True

    def refresh(self):
        """Redraw from the current Redis cache."""
        if self._records is None:
            self.state_label.setText(f"Assistant unavailable: {self._error or 'no Redis settings in beamline.toml'}")
            self.state_label.setStyleSheet("color: #c62828")
            self.question_edit.setEnabled(False)
            self.ask_button.setEnabled(False)
            self._set_feed("")
            return
        record = self.record()
        text, color = assistant_state(record)
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color}; font-weight: bold")
        self.calls_label.setText(call_summary(record))
        self._set_feed(feed_html(record, show_runs=self.show_runs.isChecked()))

    def _set_feed(self, text):
        if text == self._html:
            return
        bar = self.feed.verticalScrollBar()
        position = bar.value()
        self._html = text
        self.feed.setHtml(text)
        bar.setValue(bar.maximum() if self._follow else position)

    def _on_scrolled(self):
        bar = self.feed.verticalScrollBar()
        self._follow = bar.value() >= bar.maximum() - 2

    def _on_range(self, minimum, maximum):
        if self._follow:
            self.feed.verticalScrollBar().setValue(maximum)

    def _on_link(self, url):
        target = url.toString()
        if not target.startswith(RUN_LINK):
            return
        uid = target[len(RUN_LINK):]
        QApplication.clipboard().setText(uid)
        self.status.setText(f"Copied run {uid} to the clipboard")
        self.run_selected.emit(uid)
