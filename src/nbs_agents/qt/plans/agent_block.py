import json

from qtpy.QtWidgets import QComboBox, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QSpinBox

from nbs_gui.plans.base import PlanWidgetBase

from ...launcher import claude_launch_parameters
from ...queue import make_open_agent_block


class AgentBlockWidget(PlanWidgetBase):
    """
    Queue widget for inserting an agent control block.

    Parameters
    ----------
    model : object
        nbs-gui model providing queue access.
    parent : QWidget, optional
        Parent widget.
    """

    def __init__(self, model, parent=None):
        super().__init__(model, parent)
        self.display_name = "Agent Block"

        self._agent_combo = QComboBox(self)
        self._agent_combo.addItems(["repeat", "claude"])
        self._repeat_count = QSpinBox(self)
        self._repeat_count.setMinimum(1)
        self._repeat_count.setMaximum(1000)
        self._repeat_count.setValue(3)
        self._plan_name = QLineEdit(self)
        self._plan_name.setText("count")
        self._plan_args = QLineEdit(self)
        self._plan_args.setText("[[]]")
        self._plan_kwargs = QLineEdit(self)
        self._plan_kwargs.setText('{"num": 1}')
        self._goal = QPlainTextEdit(self)
        self._goal.setPlaceholderText("What the Claude agent should work on with you")
        self._goal.setFixedHeight(70)
        self._budget = QLineEdit(self)
        self._budget.setPlaceholderText("optional, e.g. 90m or 2h")

        form = QFormLayout()
        form.addRow("Agent:", self._agent_combo)
        form.addRow("Repeat count:", self._repeat_count)
        form.addRow("Plan name:", self._plan_name)
        form.addRow("Plan args (JSON):", self._plan_args)
        form.addRow("Plan kwargs (JSON):", self._plan_kwargs)
        form.addRow("Goal:", self._goal)
        form.addRow("Budget:", self._budget)
        self.layout.addLayout(form)
        self.layout.addWidget(
            QLabel(
                "Submits open_agent, queue_stop, and close_agent as one block. "
                "For claude, nbs-agents-runner starts the agent when open_agent runs."
            )
        )

        self._agent_combo.currentIndexChanged.connect(self._on_agent_changed)
        self._repeat_count.valueChanged.connect(self.check_plan_ready)
        self._plan_name.editingFinished.connect(self.check_plan_ready)
        self._plan_args.editingFinished.connect(self.check_plan_ready)
        self._plan_kwargs.editingFinished.connect(self.check_plan_ready)
        self._goal.textChanged.connect(self.check_plan_ready)
        self._budget.textChanged.connect(self.check_plan_ready)
        self._on_agent_changed()
        self.check_plan_ready()

    def _on_agent_changed(self) -> None:
        """
        Toggle agent-specific controls based on the selected agent.
        """
        is_repeat = self._agent_combo.currentText() == "repeat"
        self._repeat_count.setEnabled(is_repeat)
        self._plan_name.setEnabled(is_repeat)
        self._plan_args.setEnabled(is_repeat)
        self._plan_kwargs.setEnabled(is_repeat)
        self._goal.setEnabled(not is_repeat)
        self._budget.setEnabled(not is_repeat)
        self.check_plan_ready()

    def _check_ready(self) -> bool:
        """
        Validate widget inputs.

        Returns
        -------
        bool
            True if the widget can submit a block.
        """
        if self._agent_combo.currentText() == "claude":
            try:
                claude_launch_parameters(self._goal.toPlainText(), self._budget.text())
            except ValueError:
                return False
            return True
        if self._agent_combo.currentText() != "repeat":
            return True
        try:
            json.loads(self._plan_args.text())
            json.loads(self._plan_kwargs.text())
        except json.JSONDecodeError:
            return False
        return bool(self._plan_name.text().strip())

    def reset(self) -> None:
        """
        Reset widget fields to defaults.
        """
        self._agent_combo.setCurrentIndex(0)
        self._repeat_count.setValue(3)
        self._plan_name.setText("nbs_count")
        self._plan_args.setText("[]")
        self._plan_kwargs.setText('{"num": 5, "dwell": 1}')
        self._goal.clear()
        self._budget.clear()
        self.check_plan_ready()

    def create_plan_items(self):
        """
        Build the agent control block for queue insertion.

        Returns
        -------
        list
            Queue items for ``open_agent``, ``queue_stop``, and ``close_agent``.
        """
        agent_name = self._agent_combo.currentText()
        parameters = {}

        if agent_name == "repeat":
            parameters = {
                "repeat_count": self._repeat_count.value(),
                "plan_name": self._plan_name.text().strip(),
                "plan_args": json.loads(self._plan_args.text()),
                "plan_kwargs": json.loads(self._plan_kwargs.text()),
            }
        elif agent_name == "claude":
            parameters = claude_launch_parameters(self._goal.toPlainText(), self._budget.text())

        return make_open_agent_block(agent_name, parameters)
