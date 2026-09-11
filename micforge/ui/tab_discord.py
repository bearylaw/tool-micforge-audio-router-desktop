"""Discord tab: connect, then say what should happen when you join a channel."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import config, log
from ..discordlink import ipc
from ..discordlink import triggers as triggers_mod
from . import theme
from .widgets import Card, ChoiceRow, Toggle, ValueSlider, hint, hline, section

_log = log.get("ui.discord")

DETECTION_LABELS = {
    "auto": "Automatic (recommended)",
    "rpc": "Discord RPC only",
    "heuristic": "Built-in detector only",
    "off": "Off",
}

SETUP_STEPS = """How to connect properly (5 minutes, once)

1.  Open discord.com/developers/applications and press New Application.
    Any name will do - it is only ever seen by you.
2.  On the OAuth2 page copy the Client ID and the Client Secret into the
    boxes below.
3.  Still on OAuth2, add a redirect of exactly:  http://localhost
4.  Press Authorise below and approve the prompt that appears inside your
    Discord client.

Why bother: only this path can tell you which channel you joined, who else is
in it, and the precise moment your voice connection goes live. Without it the
built-in detector still fires join and leave sounds - it just cannot see names.
"""


class DiscordTab(QWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.cfg = app.cfg
        self._loading = False
        self._selected_rule = ""

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        root.addWidget(self._build_connection())
        root.addWidget(self._build_triggers())
        root.addWidget(self._build_log())
        root.addStretch(1)
        area.setWidget(container)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(area)

        self.app.on_event_log = self._append_log
        self.load_from_config()
        self.refresh_rules()

    # ----------------------------------------------------------- connection
    def _build_connection(self) -> Card:
        card = Card("Discord connection",
                    "MicForge watches for the moment your voice connection is fully "
                    "live - not just when you click a channel - so a join sound "
                    "actually reaches the people in it.")

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        card.add(self.status_label)

        self.enabled = Toggle("Enable Discord integration", True)
        self.enabled.toggled.connect(self._on_enabled)
        card.add(self.enabled)

        self.mode = ChoiceRow("Detection", list(DETECTION_LABELS.values()),
                              label_width=96)
        self.mode.valueChanged.connect(self._on_mode)
        card.add(self.mode)
        card.add(hint("Automatic uses the built-in detector straight away and upgrades "
                      "to the richer RPC data as soon as you authorise it below."))

        card.add(hline())
        card.add(section("Discord application"))

        row = QHBoxLayout()
        id_label = QLabel("Client ID")
        id_label.setObjectName("CardHint")
        id_label.setMinimumWidth(96)
        row.addWidget(id_label)
        self.client_id = QLineEdit()
        self.client_id.setPlaceholderText("18-19 digit application ID")
        self.client_id.editingFinished.connect(self._on_credentials)
        row.addWidget(self.client_id, 1)
        card.add_layout(row)

        row2 = QHBoxLayout()
        secret_label = QLabel("Client secret")
        secret_label.setObjectName("CardHint")
        secret_label.setMinimumWidth(96)
        row2.addWidget(secret_label)
        self.client_secret = QLineEdit()
        self.client_secret.setEchoMode(QLineEdit.Password)
        self.client_secret.setPlaceholderText("kept on this machine only")
        self.client_secret.editingFinished.connect(self._on_credentials)
        row2.addWidget(self.client_secret, 1)
        self.show_secret = QPushButton("Show")
        self.show_secret.setObjectName("Ghost")
        self.show_secret.setCheckable(True)
        self.show_secret.setFixedWidth(68)
        self.show_secret.toggled.connect(
            lambda on: self.client_secret.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))
        row2.addWidget(self.show_secret)
        card.add_layout(row2)

        card.add(hint("Your client secret and token are stored only in this app's "
                      "config file on this computer, and are never sent anywhere "
                      "except to discord.com when you authorise."))

        buttons = QHBoxLayout()
        self.connect_button = QPushButton("Authorise with Discord")
        self.connect_button.setObjectName("Primary")
        self.connect_button.clicked.connect(self._authorise)
        buttons.addWidget(self.connect_button)

        forget = QPushButton("Forget authorisation")
        forget.setObjectName("Ghost")
        forget.clicked.connect(self._forget)
        buttons.addWidget(forget)

        test = QPushButton("Test detection")
        test.setObjectName("Ghost")
        test.clicked.connect(self._test_detection)
        buttons.addWidget(test)
        buttons.addStretch(1)
        card.add_layout(buttons)

        steps = QPlainTextEdit(SETUP_STEPS)
        steps.setReadOnly(True)
        steps.setFixedHeight(180)
        card.add(steps)

        self.stable = ValueSlider("Hold for", 0, 5000, 100, "ms", 1200, label_width=96)
        self.stable.valueChanged.connect(self._on_stable)
        card.add(self.stable)
        card.add(hint("How long the connection has to stay up before a join counts. "
                      "Filters out the blips when Discord moves you between servers."))
        return card

    # ------------------------------------------------------------ triggers
    def _build_triggers(self) -> Card:
        card = Card("Triggers", "What to play, and when.")

        body = QHBoxLayout()
        left = QVBoxLayout()
        self.rule_list = QListWidget()
        self.rule_list.setMinimumHeight(170)
        self.rule_list.itemSelectionChanged.connect(self._on_rule_selected)
        left.addWidget(self.rule_list)

        rule_buttons = QHBoxLayout()
        add = QPushButton("Add trigger")
        add.clicked.connect(self._add_rule)
        rule_buttons.addWidget(add)
        remove = QPushButton("Remove")
        remove.setObjectName("Ghost")
        remove.clicked.connect(self._remove_rule)
        rule_buttons.addWidget(remove)
        test = QPushButton("Test")
        test.setObjectName("Ghost")
        test.clicked.connect(self._test_rule)
        rule_buttons.addWidget(test)
        left.addLayout(rule_buttons)
        body.addLayout(left, 1)

        editor = QVBoxLayout()
        editor.setSpacing(6)
        self.rule_enabled = Toggle("Enabled", True)
        self.rule_enabled.toggled.connect(self._on_rule_field)
        editor.addWidget(self.rule_enabled)

        self.rule_event = ChoiceRow("When", list(triggers_mod.EVENT_LABELS.values()),
                                    label_width=84)
        self.rule_event.valueChanged.connect(self._on_rule_field)
        editor.addWidget(self.rule_event)

        sound_row = QHBoxLayout()
        sound_label = QLabel("Play")
        sound_label.setObjectName("CardHint")
        sound_label.setMinimumWidth(84)
        sound_row.addWidget(sound_label)
        self.rule_sound = QComboBox()
        self.rule_sound.currentIndexChanged.connect(self._on_rule_field)
        sound_row.addWidget(self.rule_sound, 1)
        editor.addLayout(sound_row)

        self.rule_delay = ValueSlider("After", 0, 10000, 50, "ms", 600, label_width=84)
        self.rule_cooldown = ValueSlider("Cooldown", 0, 60000, 250, "ms", 3000,
                                         label_width=84)
        self.rule_gain = ValueSlider("Volume", -24, 12, 0.5, "dB", 0, label_width=84)
        for slider in (self.rule_delay, self.rule_cooldown, self.rule_gain):
            slider.valueChanged.connect(self._on_rule_field)
            editor.addWidget(slider)

        channel_row = QHBoxLayout()
        channel_label = QLabel("Channel")
        channel_label.setObjectName("CardHint")
        channel_label.setMinimumWidth(84)
        channel_row.addWidget(channel_label)
        self.rule_channel = QLineEdit()
        self.rule_channel.setPlaceholderText("any channel - or part of its name")
        self.rule_channel.textEdited.connect(self._on_rule_field)
        channel_row.addWidget(self.rule_channel, 1)
        editor.addLayout(channel_row)

        user_row = QHBoxLayout()
        user_label = QLabel("User")
        user_label.setObjectName("CardHint")
        user_label.setMinimumWidth(84)
        user_row.addWidget(user_label)
        self.rule_user = QLineEdit()
        self.rule_user.setPlaceholderText("any user - or part of a name")
        self.rule_user.textEdited.connect(self._on_rule_field)
        user_row.addWidget(self.rule_user, 1)
        editor.addLayout(user_row)

        self.rule_once = Toggle("Only once per session", False)
        self.rule_once.toggled.connect(self._on_rule_field)
        editor.addWidget(self.rule_once)

        self.rule_note = QLabel("")
        self.rule_note.setObjectName("CardHint")
        self.rule_note.setWordWrap(True)
        editor.addWidget(self.rule_note)
        editor.addStretch(1)
        body.addLayout(editor, 1)

        card.add_layout(body)
        return card

    def _build_log(self) -> Card:
        card = Card("Event log", "Everything Discord tells us, as it happens.")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(150)
        card.add(self.log_view)
        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.clicked.connect(self.log_view.clear)
        card.add(clear)
        return card

    # --------------------------------------------------------------- state
    def load_from_config(self) -> None:
        self._loading = True
        d = self.cfg.discord
        self.enabled.setChecked(d.enabled)
        self.mode.set_value(DETECTION_LABELS.get(d.detection, DETECTION_LABELS["auto"]))
        self.client_id.setText(d.client_id)
        self.client_secret.setText(d.client_secret)
        self.stable.set_value(d.require_stable_ms)
        self._loading = False
        self.refresh_sound_choices()

    def refresh_sound_choices(self) -> None:
        current = self.rule_sound.currentData()
        self.rule_sound.blockSignals(True)
        self.rule_sound.clear()
        for entry in self.cfg.soundboard.sounds:
            self.rule_sound.addItem(entry.name or "(unnamed)", entry.id)
        if current:
            index = self.rule_sound.findData(current)
            if index >= 0:
                self.rule_sound.setCurrentIndex(index)
        self.rule_sound.blockSignals(False)

    def refresh_rules(self) -> None:
        self.rule_list.clear()
        for rule in self.cfg.discord.triggers:
            item = QListWidgetItem(self.app.triggers.describe_rule(rule))
            item.setData(Qt.UserRole, rule.id)
            if not rule.enabled:
                item.setForeground(Qt.gray)
            self.rule_list.addItem(item)
            if rule.id == self._selected_rule:
                self.rule_list.setCurrentItem(item)
        has_rules = bool(self.cfg.discord.triggers)
        for widget in (self.rule_enabled, self.rule_event, self.rule_sound,
                       self.rule_delay, self.rule_cooldown, self.rule_gain,
                       self.rule_channel, self.rule_user, self.rule_once):
            widget.setEnabled(has_rules)
        if has_rules and not self._selected_rule:
            self._select_rule(self.cfg.discord.triggers[0].id)

    def _rule(self) -> config.TriggerRule | None:
        for rule in self.cfg.discord.triggers:
            if rule.id == self._selected_rule:
                return rule
        return None

    def _select_rule(self, rule_id: str) -> None:
        self._selected_rule = rule_id
        rule = self._rule()
        if rule is None:
            return
        self._loading = True
        self.rule_enabled.setChecked(rule.enabled)
        self.rule_event.set_value(
            triggers_mod.EVENT_LABELS.get(rule.event, rule.event))
        index = self.rule_sound.findData(rule.sound_id)
        if index >= 0:
            self.rule_sound.setCurrentIndex(index)
        self.rule_delay.set_value(rule.delay_ms)
        self.rule_cooldown.set_value(rule.cooldown_ms)
        self.rule_gain.set_value(rule.gain_db)
        self.rule_channel.setText(rule.channel_filter)
        self.rule_user.setText(rule.user_filter)
        self.rule_once.setChecked(rule.once_per_session)
        self._loading = False
        self._update_rule_note(rule)

    def _update_rule_note(self, rule: config.TriggerRule) -> None:
        mode = self.cfg.discord.detection
        if (rule.event not in triggers_mod.HEURISTIC_EVENTS
                and mode in ("heuristic",)):
            self.rule_note.setText(
                "This event needs the Discord RPC connection - the built-in detector "
                "only sees joining and leaving.")
            self.rule_note.setStyleSheet(f"color: {theme.WARN};")
        elif rule.channel_filter and self.app.rpc.state != "authenticated":
            self.rule_note.setText(
                "Channel names are only available once RPC is authorised.")
            self.rule_note.setStyleSheet(f"color: {theme.WARN};")
        else:
            self.rule_note.setText(rule.note or "")
            self.rule_note.setStyleSheet(f"color: {theme.TEXT_MUTED};")

    # ------------------------------------------------------------ handlers
    def _on_enabled(self, checked: bool) -> None:
        if self._loading:
            return
        self.cfg.discord.enabled = bool(checked)
        self.app.save(0.2)
        self.app.restart_discord()

    def _on_mode(self, label: str) -> None:
        if self._loading:
            return
        for key, text in DETECTION_LABELS.items():
            if text == label:
                self.cfg.discord.detection = key
                break
        self.app.save(0.2)
        self.app.restart_discord()

    def _on_credentials(self) -> None:
        if self._loading:
            return
        self.cfg.discord.client_id = self.client_id.text().strip()
        self.cfg.discord.client_secret = self.client_secret.text().strip()
        self.app.save(0.2)

    def _on_stable(self, value: float) -> None:
        if self._loading:
            return
        self.cfg.discord.require_stable_ms = int(value)
        self.app.save()

    def _authorise(self) -> None:
        self._on_credentials()
        if not self.cfg.discord.client_id:
            QMessageBox.information(self, "Client ID needed",
                                    "Paste your application's Client ID first. The "
                                    "steps are listed below the fields.")
            return
        if not ipc.discord_is_running():
            QMessageBox.information(self, "Discord not found",
                                    "Start the Discord desktop app first - the browser "
                                    "version cannot be controlled this way.")
            return
        self.cfg.discord.detection = "auto" if self.cfg.discord.detection == "off" \
            else self.cfg.discord.detection
        self.app.save(0.1)
        self.app.restart_discord()
        QMessageBox.information(
            self, "Check Discord",
            "Approve the prompt that appears in your Discord client.\n\n"
            "If nothing appears, make sure the Client ID is right and that you added "
            "http://localhost as a redirect in the developer portal.")

    def _forget(self) -> None:
        self.app.rpc.forget_authorisation()
        self.app.restart_discord()

    def _test_detection(self) -> None:
        from ..discordlink import fallback

        running, in_call = fallback.quick_check(self.cfg)
        pipe = ipc.discord_is_running()
        QMessageBox.information(
            self, "Detection test",
            f"Discord process running: {'yes' if running else 'no'}\n"
            f"Local RPC socket found: {'yes' if pipe else 'no'}\n"
            f"Currently in a voice call: {'yes' if in_call else 'no'}\n\n"
            f"RPC status: {self.app.rpc.summary()}")

    def _add_rule(self) -> None:
        if not self.cfg.soundboard.sounds:
            QMessageBox.information(self, "Add a sound first",
                                    "Put at least one sound on the Soundboard tab, "
                                    "then come back and point a trigger at it.")
            return
        rule = triggers_mod.default_join_rule(self.cfg.soundboard.sounds[0].id)
        self.cfg.discord.triggers.append(rule)
        self._selected_rule = rule.id
        self.app.save(0.2)
        self.refresh_rules()
        self._select_rule(rule.id)

    def _remove_rule(self) -> None:
        rule = self._rule()
        if rule is None:
            return
        self.cfg.discord.triggers = [r for r in self.cfg.discord.triggers
                                     if r.id != rule.id]
        self._selected_rule = ""
        self.app.save(0.2)
        self.refresh_rules()

    def _test_rule(self) -> None:
        rule = self._rule()
        if rule is None:
            return
        if not self.app.triggers.test_rule(rule):
            QMessageBox.warning(self, "Could not play",
                                self.app.soundboard.last_error
                                or "That sound is missing.")

    def _on_rule_selected(self) -> None:
        item = self.rule_list.currentItem()
        if item is None:
            return
        self._select_rule(item.data(Qt.UserRole))

    def _on_rule_field(self, *_args) -> None:
        rule = self._rule()
        if rule is None or self._loading:
            return
        rule.enabled = self.rule_enabled.isChecked()
        label = self.rule_event.value()
        for key, text in triggers_mod.EVENT_LABELS.items():
            if text == label:
                rule.event = key
                break
        rule.sound_id = self.rule_sound.currentData() or rule.sound_id
        rule.delay_ms = int(self.rule_delay.value())
        rule.cooldown_ms = int(self.rule_cooldown.value())
        rule.gain_db = self.rule_gain.value()
        rule.channel_filter = self.rule_channel.text()
        rule.user_filter = self.rule_user.text()
        rule.once_per_session = self.rule_once.isChecked()
        self.app.save()
        self._update_rule_note(rule)

        item = self.rule_list.currentItem()
        if item is not None:
            item.setText(self.app.triggers.describe_rule(rule))
            item.setForeground(Qt.gray if not rule.enabled else Qt.white)

    def _append_log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        QTimer.singleShot(0, lambda: self.log_view.appendPlainText(f"{stamp}  {text}"))

    # ---------------------------------------------------------------- tick
    def tick(self) -> None:
        summary = self.app.discord_summary()
        self.status_label.setText(summary)
        state = self.app.rpc.state
        if state == "authenticated":
            colour = theme.GOOD
        elif state in ("connecting", "ready", "authorising"):
            colour = theme.WARN
        elif self.app.heuristic.running and self.app.heuristic.discord_running:
            colour = theme.GOOD if self.app.heuristic.connected else theme.TEXT_MUTED
        else:
            colour = theme.TEXT_MUTED
        self.status_label.setStyleSheet(f"color: {colour};")
