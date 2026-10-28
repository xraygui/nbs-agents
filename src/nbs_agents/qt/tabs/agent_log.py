"""nbs-gui tab that shows the shared agent log written by every nbs-agents agent."""

import html
from datetime import datetime

from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QColor
from qtpy.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...agentlog import AGENT_LOG_TOPIC, AgentLog, is_assistant_record
from ...launcher import claude_launch_parameters
from ...queue import submit_agent_block

ALL_AGENTS = "__all__"
ENTRY_COLUMNS = ["Time", "Agent", "Kind", "Text", "Details"]
ENTRY_FIELDS = {"t", "kind", "text", "source"}
STEERABLE_STATUSES = ("opening", "active", "finalizing")
CONVERSATION_KINDS = ("note", "question", "operator", "stop", "finalize")
WAITING_COLOR = "#f0ad4e"
CLAUDE_AGENT = "claude"
RUNNER_POLL_MS = 5000
# runner_alive() result -> (label text, color)
RUNNER_STATES = {
    True: ("Agent runner: running", "#2e7d32"),
    False: ("Agent runner: not running", "#c62828"),
    None: ("Agent runner: unknown", "#a05f00"),
}
WAITING_BACKGROUND = "#fff1cc"

# kind -> (side, background, label); "right" is the operator's side.
BUBBLE_STYLES = {
    "note": ("left", "#eceff3", ""),
    "question": ("left", "#fff1cc", "Question"),
    "finalize": ("left", "#e3f2e6", "Summary"),
    "operator": ("right", "#dbe8fb", ""),
    "stop": ("right", "#f9dcdb", "Stop requested"),
}


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


def is_awaiting_input(record):
    """
    Whether a session has an unanswered question for the operator.

    Parameters
    ----------
    record : dict
        Agent-log record.

    Returns
    -------
    bool
        True if ``awaiting_input`` is set and the session is still running.
    """
    return bool(record.get("awaiting_input")) and record.get("status") in STEERABLE_STATUSES


def session_status(record):
    """
    Status to show for a session, with waiting for input taking precedence.

    Parameters
    ----------
    record : dict
        Agent-log record.

    Returns
    -------
    str
        ``"waiting for input"`` or the record's status.
    """
    return "waiting for input" if is_awaiting_input(record) else str(record.get("status", ""))


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


def split_entries(pairs):
    """
    Separate conversation entries from activity entries.

    Parameters
    ----------
    pairs : list of tuple
        ``(label, entry)`` pairs from ``collect_entries``.

    Returns
    -------
    tuple of list
        ``(conversation, activity)``; conversation holds notes, questions,
        operator messages, stop requests and finalize summaries.
    """
    conversation, activity = [], []
    for pair in pairs:
        (conversation if pair[1].get("kind") in CONVERSATION_KINDS else activity).append(pair)
    return conversation, activity


def bubble_html(label, entry, open_question=False, show_agent=True):
    """
    Render one conversation entry as a chat bubble.

    Parameters
    ----------
    label : str
        Agent label for the entry's session.
    entry : dict
        Agent-log entry.
    open_question : bool, optional
        Highlight the entry as the question the agent is waiting on.
    show_agent : bool, optional
        Name the agent session on operator messages too.

    Returns
    -------
    str
        Qt rich-text HTML: a full-width table with the bubble on the agent's
        (left) or operator's (right) side.
    """
    kind = entry.get("kind")
    side, background, tag = BUBBLE_STYLES.get(kind, BUBBLE_STYLES["note"])
    if side == "right":
        source = entry.get("source")
        who = "Operator" if source in (None, "gui", "operator") else f"Operator ({source})"
        if show_agent:
            who += f" to {label}"
    else:
        who = label
    if open_question:
        tag, background = "Waiting for your answer", WAITING_BACKGROUND
    header = f"<b>{html.escape(who)}</b> &middot; {format_time(entry.get('t'))}"
    if tag:
        color = "#a05f00" if open_question else "#555555"
        header += f" &middot; <b style='color:{color}'>{html.escape(tag)}</b>"
    body = html.escape(str(entry.get("text", ""))).replace("\n", "<br>")
    bubble = (
        f"<td width='80%' bgcolor='{background}' style='color:#1a1a1a'>"
        f"<span style='font-size:small; color:#555555'>{header}</span><br>{body}</td>"
    )
    spacer = "<td width='20%'></td>"
    cells = bubble + spacer if side == "left" else spacer + bubble
    return f"<table width='100%' cellspacing='0' cellpadding='8'><tr>{cells}</tr></table>"


def conversation_html(records, pairs, show_agent=True):
    """
    Render a conversation as chat bubbles.

    Parameters
    ----------
    records : dict
        Agent-log records keyed by UID.
    pairs : list of tuple
        Conversation ``(label, entry)`` pairs in time order.
    show_agent : bool, optional
        Name the agent session on operator messages.

    Returns
    -------
    str
        Qt rich-text HTML.
    """
    open_times = {
        (agent_label(uid, record), record["awaiting_input"].get("t"))
        for uid, record in records.items()
        if is_awaiting_input(record)
    }
    if not pairs:
        return "<p style='color:#777777'>No messages yet.</p>"
    parts = []
    for label, entry in pairs:
        is_open = entry.get("kind") == "question" and (label, entry.get("t")) in open_times
        parts.append(bubble_html(label, entry, open_question=is_open, show_agent=show_agent))
    return "<div style='margin-top:4px'></div>".join(parts)


class NewAgentDialog(QDialog):
    """
    Ask for a Claude agent's goal and budget before queueing its session.

    Parameters
    ----------
    runner_alive : bool or None
        Runner heartbeat state; ``False`` disables OK, ``None`` only warns.
    parent : QWidget, optional
        Parent widget.
    """

    def __init__(self, runner_alive=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Claude agent")
        self.goal_edit = QPlainTextEdit(self)
        self.goal_edit.setPlaceholderText("What should the agent work on? It will plan this with you before queueing scans.")
        self.budget_edit = QLineEdit(self)
        self.budget_edit.setPlaceholderText("optional, e.g. 90m or 2h")
        self.start_check = QCheckBox("Start the queue if idle", self)
        self.start_check.setChecked(True)
        self.runner_label = QLabel(self)
        self.runner_label.setWordWrap(True)
        self.error_label = QLabel(self)
        self.error_label.setStyleSheet("color: #c62828")
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setText("Queue agent")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Goal:", self.goal_edit)
        form.addRow("Budget:", self.budget_edit)
        form.addRow("", self.start_check)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Queues open_agent, queue_stop and close_agent at the back of the queue. "
                "When open_agent runs, nbs-agents-runner starts Claude for the session.",
                self,
            )
        )
        layout.addLayout(form)
        layout.addWidget(self.runner_label)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

        self._runner_alive = runner_alive
        text, color = RUNNER_STATES[runner_alive]
        if runner_alive is False:
            text += ". Start it (`pixi run agent-runner` in nbs-agents) on a host where `claude` is logged in."
        elif runner_alive is None:
            text += ". Could not read its heartbeat; the agent will only start if the runner is up."
        self.runner_label.setText(text)
        self.runner_label.setStyleSheet(f"color: {color}")
        self.goal_edit.textChanged.connect(self._validate)
        self.budget_edit.textChanged.connect(self._validate)
        self._validate()

    def parameters(self):
        """
        ``open_agent`` parameters for the entered goal and budget.

        Returns
        -------
        dict
            Parameters with ``launch: True``.

        Raises
        ------
        ValueError
            If the goal is empty or the budget is malformed.
        """
        return claude_launch_parameters(self.goal_edit.toPlainText(), self.budget_edit.text())

    def start_queue(self):
        """Whether to start the queue if the block lands at its idle head."""
        return self.start_check.isChecked()

    def _validate(self):
        error = ""
        try:
            self.parameters()
        except ValueError as e:
            error = "" if not self.goal_edit.toPlainText().strip() else str(e)
            ok = False
        else:
            ok = self._runner_alive is not False
        self.error_label.setText(error)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)


class AgentLogTab(QWidget):
    """
    Show agent sessions from the shared Redis agent log as a conversation.

    A drop-down picks one session, or "All agents" to merge every log. The
    upper pane shows the conversation (agent notes and questions, operator
    messages and stop requests, the final summary) as chat bubbles, with a
    box to message the selected running session or ask it to stop. The lower
    table shows the rest of the log: opens, queued and removed plans, runs and
    status changes. When an agent asks a question with ``nbs-cli ask`` an
    amber banner says it is waiting for input until someone replies, and the
    tab title counts waiting agents.

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
        self._conversation_html = None
        try:
            self._records = model.user_status.get_redis_dict(AGENT_LOG_TOPIC)
        except Exception as e:  # Redis unreachable should not break the GUI
            self._error = str(e)

        self.agent_combo = QComboBox(self)
        self.agent_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.agent_combo.currentIndexChanged.connect(self._on_agent_selected)
        self.new_agent_button = QPushButton("New agent…", self)
        self.new_agent_button.setToolTip("Queue a Claude agent session with a goal; the agent runner starts Claude")
        self.new_agent_button.clicked.connect(self.new_agent)
        self.runner_label = QLabel(self)
        picker = QHBoxLayout()
        picker.addWidget(QLabel("Agent:", self))
        picker.addWidget(self.agent_combo)
        picker.addWidget(self.new_agent_button)
        picker.addStretch(1)
        picker.addWidget(self.runner_label)

        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.banner = QLabel(self)
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setStyleSheet(
            f"QLabel {{ background: {WAITING_BACKGROUND}; color: #1a1a1a;"
            f" border: 2px solid {WAITING_COLOR}; border-radius: 4px; padding: 6px; }}"
        )
        self.banner.linkActivated.connect(self.select_agent)
        self.banner.hide()

        self.conversation = QTextBrowser(self)
        self.conversation.setOpenLinks(False)
        self._follow_conversation = True
        bar = self.conversation.verticalScrollBar()
        # Only the operator scrolling (wheel, drag, arrows) stops following new messages.
        bar.actionTriggered.connect(lambda action: QTimer.singleShot(0, self._on_conversation_scrolled))
        bar.sliderReleased.connect(self._on_conversation_scrolled)
        bar.rangeChanged.connect(self._on_conversation_range)

        self.message_edit = QLineEdit(self)
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

        chat = QWidget(self)
        chat_layout = QVBoxLayout(chat)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.addWidget(self.conversation)
        chat_layout.addLayout(steer)

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

        activity = QWidget(self)
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(0, 0, 0, 0)
        activity_label = QLabel("<b>Activity</b> (sessions, queued plans, runs)", self)
        activity_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        activity_layout.addWidget(activity_label)
        activity_layout.addWidget(self.entries)

        splitter = QSplitter(Qt.Vertical, self)
        splitter.addWidget(chat)
        splitter.addWidget(activity)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(picker)
        layout.addWidget(self.summary)
        layout.addWidget(self.banner)
        layout.addWidget(splitter, 1)

        if self._records is not None:
            self._records.changed.connect(self.refresh)
            running = [uid for uid, r in sorted_sessions(self._snapshot()) if r.get("status") in STEERABLE_STATUSES]
            if running:
                self._selected = running[0]
        # The heartbeat is outside the AGENT topic, so poll it
        self._runner_timer = QTimer(self)
        self._runner_timer.timeout.connect(self._update_runner_state)
        self._runner_timer.start(RUNNER_POLL_MS)
        self._update_runner_state()
        self.refresh()

    def _writer(self):
        records = self._records
        return AgentLog(records._redis, records._prefix, AGENT_LOG_TOPIC)

    def runner_alive(self):
        """
        Whether nbs-agents-runner has written a recent heartbeat.

        Returns
        -------
        bool or None
            ``None`` if the heartbeat cannot be read.
        """
        if self._records is None:
            return None
        try:
            return self._writer().runner_alive()
        except Exception:
            return None

    def _update_runner_state(self):
        alive = self.runner_alive()
        text, color = RUNNER_STATES[alive]
        self.runner_label.setText(text)
        self.runner_label.setStyleSheet(f"color: {color}")
        self.new_agent_button.setEnabled(alive is not False)
        if alive is False:
            self.new_agent_button.setToolTip("Start nbs-agents-runner first: it launches the Claude agent")
        else:
            self.new_agent_button.setToolTip("Queue a Claude agent session with a goal; the agent runner starts Claude")

    def new_agent(self):
        """Ask for a goal and queue a Claude session the runner will launch."""
        dialog = NewAgentDialog(self.runner_alive(), self)
        if dialog.exec_() != QDialog.Accepted:
            return
        busy = [
            agent_label(uid, r)
            for uid, r in sorted_sessions(self._snapshot())
            if r.get("agent_name") == CLAUDE_AGENT and r.get("status") in STEERABLE_STATUSES
        ]
        if busy:
            answer = QMessageBox.question(
                self,
                "New agent",
                f"{', '.join(busy)} is still running. Queue another Claude session after it?",
            )
            if answer != QMessageBox.Yes:
                return
        try:
            out = self.submit_new_agent(dialog.parameters(), start=dialog.start_queue())
        except Exception as e:
            QMessageBox.warning(self, "New agent", f"Could not queue the agent: {e}")
            return
        if not out["started"]:
            where = "at the head of the queue" if out["position"] == 0 else f"behind {out['position']} item(s)"
            QMessageBox.information(
                self,
                "New agent",
                f"Queued {where}. Claude starts when open_agent runs; start the queue when ready.",
            )

    def submit_new_agent(self, parameters, start=True):
        """
        Queue a ``claude`` agent block through the GUI's queue server client.

        Parameters
        ----------
        parameters : dict
            ``open_agent`` parameters, normally from ``claude_launch_parameters``.
        start : bool, optional
            Start the queue if it is idle and the block is at its head.

        Returns
        -------
        dict
            ``submit_agent_block`` result: ``items``, ``started``, ``position``.
        """
        run_engine = self.model.run_engine
        user = {}
        if getattr(run_engine, "_user_name", None):
            user["user"] = run_engine._user_name
        if getattr(run_engine, "_user_group", None):
            user["user_group"] = run_engine._user_group
        try:
            return submit_agent_block(run_engine._client, CLAUDE_AGENT, parameters, start=start, **user)
        finally:
            try:
                run_engine.load_re_manager_status(unbuffered=True)
            except Exception:
                pass

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
        if steerable and is_awaiting_input(record):
            self.message_edit.setPlaceholderText("Reply to the agent's question")
        elif steerable:
            self.message_edit.setPlaceholderText("Message to the selected agent")
        elif record is None:
            self.message_edit.setPlaceholderText("Choose a running agent to send it a message")
        else:
            self.message_edit.setPlaceholderText(f"This session is {record.get('status')}")

    def select_agent(self, uid):
        """
        Show one agent session.

        Parameters
        ----------
        uid : str
            Session UID, or ``ALL_AGENTS``.
        """
        index = self.agent_combo.findData(uid)
        if index >= 0:
            self.agent_combo.setCurrentIndex(index)

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
        # The assistant has its own tab, and its record is not a steerable session
        return {
            k: v for k, v in self._records.items() if isinstance(v, dict) and not is_assistant_record(v, k)
        }

    def refresh(self):
        """Redraw everything from the current Redis cache."""
        records = self._snapshot()
        if self._selected != ALL_AGENTS and self._selected not in records:
            self._selected = ALL_AGENTS
        self._fill_agents(records)
        self._fill_banner(records)
        self._fill_entries(records)
        self._update_controls()
        self._update_tab_title(records)

    def _fill_agents(self, records):
        combo = self.agent_combo
        combo.blockSignals(True)
        combo.clear()
        waiting = sum(1 for r in records.values() if is_awaiting_input(r))
        all_label = f"All agents ({len(records)})"
        if waiting:
            all_label += f" • {waiting} waiting for input"
        combo.addItem(all_label, ALL_AGENTS)
        for uid, record in sorted_sessions(records):
            text = f"{agent_label(uid, record)} • {session_status(record)}"
            if record.get("goal"):
                text += f" • {record['goal']}"
            if len(text) > 90:
                text = text[:89] + "…"
            combo.addItem(text, uid)
            index = combo.count() - 1
            combo.setItemData(index, str(record.get("goal") or ""), Qt.ToolTipRole)
            if is_awaiting_input(record):
                combo.setItemData(index, QColor(WAITING_BACKGROUND), Qt.BackgroundRole)
        combo.setCurrentIndex(max(combo.findData(self._selected), 0))
        combo.blockSignals(False)

    def _fill_banner(self, records):
        if self._selected == ALL_AGENTS:
            shown = records
        else:
            shown = {self._selected: records[self._selected]}
        waiting = [(uid, r) for uid, r in sorted_sessions(shown) if is_awaiting_input(r)]
        if not waiting:
            self.banner.hide()
            return
        lines = []
        for uid, record in waiting:
            question = html.escape(str(record["awaiting_input"].get("text", ""))).replace("\n", "<br>")
            line = f"<b>{html.escape(agent_label(uid, record))} is waiting for your input</b>"
            line += f" (asked {format_time(record['awaiting_input'].get('t'))})"
            if self._selected == ALL_AGENTS:
                line += f" &nbsp;<a href='{html.escape(uid)}'>Reply</a><br>{question}"
            else:
                line += ". Reply in the box below."
            lines.append(line)
        self.banner.setText("<br><br>".join(lines))
        self.banner.show()

    def _fill_entries(self, records):
        if self._records is None:
            self.summary.setText(f"Agent log unavailable: {self._error or 'no Redis settings in beamline.toml'}")
            self.entries.setRowCount(0)
            self._set_conversation("")
            return

        if self._selected == ALL_AGENTS:
            self.summary.setText(f"{len(records)} agent session(s) in {AGENT_LOG_TOPIC} log")
        else:
            record = records[self._selected]
            parts = [f"<b>{html.escape(record.get('agent_name') or '?')}</b> {self._selected}"]
            parts.append(f"status: {html.escape(session_status(record))}")
            if record.get("budget"):
                parts.append(f"budget: {html.escape(str(record['budget']))}")
            line = " &middot; ".join(parts)
            if record.get("goal"):
                line += f"<br>goal: {html.escape(str(record['goal']))}"
            if record.get("stop_requested"):
                stop = html.escape(str(record["stop_requested"].get("text", "")))
                line += f"<br><b>stop requested</b>: {stop}"
            if record.get("close_reason"):
                line += f"<br>closed: {html.escape(str(record['close_reason']))}"
            self.summary.setText(line)

        conversation, activity = split_entries(collect_entries(records, self._selected))
        self._set_conversation(conversation_html(records, conversation, show_agent=self._selected == ALL_AGENTS))

        bar = self.entries.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        self.entries.setRowCount(len(activity))
        for row, (label, entry) in enumerate(activity):
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

    def _set_conversation(self, text):
        if text == self._conversation_html:
            return
        bar = self.conversation.verticalScrollBar()
        position = bar.value()
        self._conversation_html = text
        self.conversation.setHtml(text)
        bar.setValue(bar.maximum() if self._follow_conversation else position)

    def _on_conversation_scrolled(self):
        bar = self.conversation.verticalScrollBar()
        self._follow_conversation = bar.value() >= bar.maximum() - 2

    def _on_conversation_range(self, minimum, maximum):
        # Layout finishes after setHtml, so keep following the newest message.
        if self._follow_conversation:
            self.conversation.verticalScrollBar().setValue(maximum)

    def _update_tab_title(self, records):
        """Count waiting agents in the tab title so they show from other tabs."""
        widget, tabs = self, None
        while widget is not None:
            parent = widget.parentWidget()
            if isinstance(parent, QTabWidget):
                tabs = parent
                break
            widget = parent
        if tabs is None:
            return
        index = tabs.indexOf(self)
        if index < 0:
            return
        waiting = sum(1 for r in records.values() if is_awaiting_input(r))
        tabs.setTabText(index, f"{self.name} ({waiting} waiting)" if waiting else self.name)
        tabs.tabBar().setTabTextColor(index, QColor("#b36b00") if waiting else QColor())

    def _on_agent_selected(self, index):
        uid = self.agent_combo.itemData(index)
        if uid is None:
            return
        self._selected = uid
        self._conversation_html = None
        self._follow_conversation = True
        records = self._snapshot()
        self._fill_banner(records)
        self._fill_entries(records)
        self._update_controls()
        self.entries.scrollToBottom()
