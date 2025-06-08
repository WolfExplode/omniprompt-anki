"""
OmniPrompt Anki Add‑on
Features:
- Non-modal dialog for "Update with OmniPrompt."
- Per-provider API keys (not per model).
- prompt_settings.json for storing each prompt's output field preference.
- QCompleter for searching saved prompts.
- CheckBox for "Append AI Output?" in the Update with OmniPrompt dialog.
"""

import requests, logging, os, time, socket, sys, json
from jsonschema import validate
from anki.errors import NotFoundError
from aqt.utils import showInfo, getText
from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QDoubleValidator,
    QIntValidator,
    QKeySequence,
    QShortcut
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QGroupBox, QComboBox, QLabel,
    QLineEdit, QFormLayout, QPushButton, QTextEdit, QHBoxLayout,
    QWidget, QTableWidget, QTableWidgetItem, QMenu, QCheckBox, QCompleter
)
from aqt import mw, gui_hooks
from aqt.browser import Browser
from anki.hooks import addHook
from logging.handlers import RotatingFileHandler

# ----------------------------------------------------------------
# Constants & Config
# ----------------------------------------------------------------
AI_PROVIDERS = ["openai", "deepseek"]

DEFAULT_CONFIG = {
    "_version": 1.1,
    "AI_PROVIDER": "openai",
    "OPENAI_MODEL": "gpt-4o-mini",
    "DEEPSEEK_MODEL": "deepseek-chat",
    # Now just "openai" or "deepseek" for keys
    "API_KEYS": {
        # "openai": "...",
        # "deepseek": "..."
    },
    "TEMPERATURE": 0.2,
    "MAX_TOKENS": 200,
    "API_DELAY": 1,          # Delay (seconds) between API calls
    "TIMEOUT": 20,           # Request timeout
    "PROMPT": "Paste your prompt here.",
    "SELECTED_FIELDS": {
        "output_field": "Output"
    },
    "DEEPSEEK_STREAM": False,
    "APPEND_OUTPUT": False
}

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "_version": {"type": "number"},
        "AI_PROVIDER": {"enum": AI_PROVIDERS},
        "OPENAI_MODEL": {"type": "string"},
        "DEEPSEEK_MODEL": {"type": "string"},
        # Single key per provider
        "API_KEYS": {"type": "object"},
        "TEMPERATURE": {"type": "number"},
        "MAX_TOKENS": {"type": "integer"},
        "API_DELAY": {"type": "number"},
        "TIMEOUT": {"type": "number"},
        "PROMPT": {"type": "string"},
        "DEEPSEEK_STREAM": {"type": "boolean"},
        "APPEND_OUTPUT": {"type": "boolean"},
        "SELECTED_FIELDS": {
            "type": "object",
            "properties": {
                "output_field": {"type": "string"}
            }
        }
    },
    "required": ["AI_PROVIDER", "API_KEYS"]
}

PROMPT_SETTINGS_FILENAME = "prompt_settings.json"

# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def safe_show_info(message: str) -> None:
    QTimer.singleShot(0, lambda: showInfo(message))

def load_prompt_templates() -> dict:
    """Loads prompts from prompt_templates.txt using [[[Name]]] delimiters."""
    templates_path = os.path.join(os.path.dirname(__file__), "prompt_templates.txt")
    templates = {}
    if os.path.exists(templates_path):
        with open(templates_path, "r", encoding="utf-8") as file:
            current_key = None
            current_value = []
            for line in file:
                line = line.rstrip('\n')
                if line.startswith("[[[") and line.endswith("]]]"):
                    if current_key is not None:
                        templates[current_key] = "\n".join(current_value)
                    current_key = line[3:-3].strip()
                    current_value = []
                else:
                    current_value.append(line)
            if current_key is not None:
                templates[current_key] = "\n".join(current_value)
    return templates

def save_prompt_templates(templates: dict) -> None:
    templates_path = os.path.join(os.path.dirname(__file__), "prompt_templates.txt")
    os.makedirs(os.path.dirname(templates_path), exist_ok=True)
    with open(templates_path, "w", encoding="utf-8", newline="\n") as file:
        for key, value in sorted(templates.items()):
            file.write(f"[[[{key}]]]\n{value}\n\n")

def check_internet() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        return False

def get_prompt_settings_path() -> str:
    return os.path.join(os.path.dirname(__file__), PROMPT_SETTINGS_FILENAME)

def load_prompt_settings() -> dict:
    """Load each prompt’s extra settings from JSON (like outputField)."""
    path = get_prompt_settings_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.exception(f"Failed to load prompt settings from {path}:")
        return {}

def save_prompt_settings(settings: dict) -> None:
    path = get_prompt_settings_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        logger.exception(f"Failed to save prompt settings to {path}:")

# ----------------------------------------------------------------
# Logger
# ----------------------------------------------------------------
def get_addon_dir() -> str:
    raw_dir = os.path.dirname(__file__)
    parent = os.path.dirname(raw_dir)
    base = os.path.basename(raw_dir).strip()
    return os.path.join(parent, base)

def setup_logger() -> logging.Logger:
    logger_obj = logging.getLogger("OmniPromptAnki")
    logger_obj.setLevel(logging.INFO)
    addon_dir = get_addon_dir()
    log_file = os.path.join(addon_dir, "omnPrompt-anki.log")
    handler = SafeAnkiRotatingFileHandler(
        filename=log_file,
        mode='a',
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding='utf-8',
        delay=True
    )
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not logger_obj.handlers:
        logger_obj.addHandler(handler)
    return logger_obj

class SafeAnkiRotatingFileHandler(RotatingFileHandler):
    def emit(self, record):
        try:
            super().emit(record)
        except Exception as e:
            print(f"Log write failed: {str(e)}")

    def shouldRollover(self, record) -> bool:
        try:
            return super().shouldRollover(record)
        except Exception as e:
            print(f"Log rotation check failed: {str(e)}")
            return False

    def doRollover(self):
        try:
            super().doRollover()
            print("Successfully rotated log file")
        except PermissionError:
            print("Couldn't rotate log - file in use")
        except Exception as e:
            print(f"Log rotation failed: {str(e)}")

def check_log_size():
    log_path = os.path.join(mw.addonManager.addonsFolder(), "omniprompt-anki", "omnPrompt-anki.log")
    try:
        size = os.path.getsize(log_path)
        if size > 4.5 * 1024 * 1024:
            print("Log file approaching maximum size")
    except Exception:
        pass

addHook("reset", check_log_size)
logger = setup_logger()

# ----------------------------------------------------------------
# Background Worker
# ----------------------------------------------------------------
class NoteProcessingWorker(QThread):
    progress_update = pyqtSignal(int, int)
    note_result = pyqtSignal(object, str)
    error_occurred = pyqtSignal(object, str)
    finished_processing = pyqtSignal(int, int, int)

    def __init__(self, note_prompts: list, generate_ai_response_callback, parent=None):
        super().__init__(parent)
        self.note_prompts = note_prompts
        self.generate_ai_response_callback = generate_ai_response_callback
        self._is_cancelled = False
        self.processed = 0
        self.error_count = 0

    def run(self) -> None:
        total = len(self.note_prompts)
        for i, (note, prompt) in enumerate(self.note_prompts):
            if self._is_cancelled:
                break
            self.progress_update.emit(i, 0)

            try:
                def per_chunk_progress(pct):
                    if pct >= 100:
                        pct = 99
                    self.progress_update.emit(i, pct)

                explanation = self.generate_ai_response_callback(prompt, stream_progress_callback=per_chunk_progress)
                self.progress_update.emit(i, 100)
                self.note_result.emit(note, explanation)
            except Exception as e:
                self.error_count += 1
                logger.exception(f"Error processing note {note.id}")
                self.error_occurred.emit(note, str(e))

            self.processed += 1

        self.finished_processing.emit(self.processed, total, self.error_count)

    def cancel(self) -> None:
        self._is_cancelled = True

# ----------------------------------------------------------------
# OmniPromptManager
# ----------------------------------------------------------------
class OmniPromptManager:
    @property
    def addon_dir(self) -> str:
        return os.path.dirname(__file__)

    def __init__(self):
        self.config = self.load_config()
        mw.addonManager.setConfigAction(__name__, self.show_settings_dialog)

    def load_config(self) -> dict:
        raw_config = mw.addonManager.getConfig(__name__) or {}
        validated = self.validate_config(raw_config)
        return self.migrate_config(validated)

    def migrate_config(self, config: dict) -> dict:
        """You can simplify or remove if you don't need to handle older versions."""
        current_version = config.get("_version", 0)
        if current_version < 1.0:
            logger.info(f"Config too old (v{current_version}). Forcing reset.")
            return DEFAULT_CONFIG.copy()

        # Combine defaults
        merged = DEFAULT_CONFIG.copy()
        merged.update(config)
        # Bump version if needed
        if merged["_version"] < 1.1:
            merged["_version"] = 1.1
        return merged

    def validate_config(self, config: dict) -> dict:
        try:
            validate(instance=config, schema=CONFIG_SCHEMA)
            return config
        except Exception as e:
            logger.exception(f"Config validation error: {str(e)}")
            logger.info("Reverting to default configuration")
            return DEFAULT_CONFIG.copy()

    def show_settings_dialog(self) -> None:
        dialog = SettingsDialog(mw)
        dialog.load_config(self.config)
        if dialog.exec():
            self.config = dialog.get_updated_config()
            self.save_config()

    def save_config(self) -> None:
        try:
            validated = self.validate_config(self.config)
            migrated = self.migrate_config(validated)
            logger.debug(f"Saving config: { {k: v for k, v in migrated.items() if k != 'API_KEYS'} }")
            logger.debug(f"API_KEYS present: {'openai' in migrated.get('API_KEYS', {})}")
            mw.addonManager.writeConfig(__name__, migrated)
            logger.info("Config saved successfully")
        except Exception as e:
            logger.exception(f"Config save failed: {str(e)}")
            pass

    def generate_ai_response(self, prompt: str, stream_progress_callback=None) -> str:
        provider = self.config.get("AI_PROVIDER", "openai")
        if provider == "openai":
            return self._make_openai_request(prompt, stream_progress_callback)
        elif provider == "deepseek":
            return self._make_deepseek_request(prompt, stream_progress_callback)
        else:
            logger.error(f"Invalid AI provider: {provider}")
            return "[Error: Invalid provider]"

    def _make_openai_request(self, prompt: str, stream_callback=None) -> str:
        api_key = self.config.get("API_KEYS", {}).get("openai", "")
        if not api_key:
            return "[Error: No OpenAI key found]"

        model = self.config.get("OPENAI_MODEL", "gpt-4o-mini")
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config["MAX_TOKENS"],
            "temperature": self.config["TEMPERATURE"]
        }
        return self._send_request(url, headers, data)

    def _make_deepseek_request(self, prompt: str, stream_callback=None) -> str:
        api_key = self.config.get("API_KEYS", {}).get("deepseek", "")
        if not api_key:
            return "[Error: No DeepSeek key found]"

        model = self.config.get("DEEPSEEK_MODEL", "deepseek-chat")
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": self.config.get("TEMPERATURE", 0.2),
            "max_tokens": self.config.get("MAX_TOKENS", 200),
            "stream": self.config.get("DEEPSEEK_STREAM", False)
        }
        timeout_val = self.config.get("TIMEOUT", 20)

        # (Same streaming logic as before)
        try:
            response = requests.post(url, headers=headers, json=data, timeout=timeout_val, stream=data["stream"])
            response.raise_for_status()
        except Exception as e:
            logger.exception("DeepSeek request failed:")
            return "[Error: request failed]"

        if data["stream"]:
            final_msg = ""
            chunk_count = 0
            try:
                for line in response.iter_lines():
                    if self._is_empty_or_keepalive(line):
                        continue
                    chunk_count += 1
                    try:
                        json_line = json.loads(line.decode("utf-8"))
                        delta = json_line.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        final_msg += delta
                        if stream_callback:
                            approximate_pct = min(99, chunk_count * 5)
                            stream_callback(approximate_pct)
                    except Exception:
                        logger.exception("Error parsing a chunk from DeepSeek:")
                return final_msg if final_msg else "[Error: empty streamed response]"
            except Exception:
                logger.exception("Error reading DeepSeek stream:")
                return "[Error: streaming failure]"
        else:
            try:
                resp_json = response.json()
                if "choices" in resp_json and resp_json["choices"]:
                    txt = resp_json["choices"][0].get("message", {}).get("content", "")
                    return txt.strip() if txt else "[Error: empty response]"
                return "[Error: unexpected response format]"
            except Exception:
                logger.exception("DeepSeek non-stream parse error:")
                return "[Error: parse failure]"

    def _send_request(self, url: str, headers: dict, data: dict) -> str:
        retries = 3
        backoff_factor = 2
        timeout_val = self.config.get("TIMEOUT", 20)

        if not check_internet():
            return "[Error: no internet connection]"

        for attempt in range(retries):
            try:
                safe_data = data.copy()
                if "Authorization" in headers:
                    safe_data["Authorization"] = "[REDACTED]"
                logger.info(f"Sending request attempt {attempt+1}: {safe_data}")
                resp = requests.post(url, headers=headers, json=data, timeout=timeout_val)
                resp.raise_for_status()
                resp_json = resp.json()
                time.sleep(self.config.get("API_DELAY", 1))

                if "choices" in resp_json and resp_json["choices"]:
                    content = resp_json["choices"][0].get("message", {}).get("content", "").strip()
                    if content:
                        return content
                    else:
                        return "[Error: empty response]"
                return "[Error: unexpected response format]"
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout. Retrying {attempt+1}/{retries}...")
                time.sleep(backoff_factor * (attempt + 1))
            except Exception as e:
                logger.exception("API error:")
                return f"[Error: request exception: {e}]"
        return "[Error: request failed after retries]"

    @staticmethod
    def _is_empty_or_keepalive(line: bytes) -> bool:
        if not line:
            return True
        text = line.decode("utf-8").strip()
        return (not text) or text.startswith("data: [DONE]") or text.startswith(":")

# ----------------------------------------------------------------
# SettingsDialog
# ----------------------------------------------------------------
class SettingsDialog(QDialog):
    """
    Single API key per provider. 
    Changing the model won't affect the key field.
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OmniPrompt Configuration")
        self.setMinimumWidth(500)
        self.config = None
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()

        # AI provider
        provider_group = QGroupBox("AI Provider Selection")
        provider_layout = QVBoxLayout()

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(AI_PROVIDERS)
        provider_layout.addWidget(QLabel("Select AI Provider:"))
        provider_layout.addWidget(self.provider_combo)

        self.model_combo = QComboBox()
        provider_layout.addWidget(QLabel("Model:"))
        provider_layout.addWidget(self.model_combo)

        provider_group.setLayout(provider_layout)
        layout.addWidget(provider_group)

        # Single "API Key" (one per provider)
        api_group = QGroupBox("API Settings")
        api_layout = QFormLayout()

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("Enter API key for the chosen provider")
        api_layout.addRow("API Key:", self.api_key_input)

        self.temperature_input = QLineEdit()
        self.temperature_input.setValidator(QDoubleValidator(0.0, 2.0, 2))
        api_layout.addRow("Temperature:", self.temperature_input)

        self.max_tokens_input = QLineEdit()
        self.max_tokens_input.setValidator(QIntValidator(1, 4000))
        api_layout.addRow("Max Tokens:", self.max_tokens_input)

        api_group.setLayout(api_layout)
        layout.addWidget(api_group)

        # Advanced settings
        self.advanced_button = QPushButton("Advanced Settings")
        self.advanced_button.clicked.connect(lambda: AdvancedSettingsDialog(self).exec())
        layout.addWidget(self.advanced_button)

        # View log
        self.view_log_button = QPushButton("View Log")
        self.view_log_button.clicked.connect(self.show_log)
        layout.addWidget(self.view_log_button)

        # Save/Cancel
        btn_layout = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        btn_layout.addWidget(self.save_button)
        btn_layout.addWidget(self.cancel_button)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        # Connect
        self.provider_combo.currentIndexChanged.connect(self.update_model_options)
        self.model_combo.currentIndexChanged.connect(self.show_provider_key)

    def load_config(self, config: dict) -> None:
        self.config = config
        provider = config.get("AI_PROVIDER", "openai")
        self.provider_combo.setCurrentText(provider)
        self.update_model_options()

        if provider == "openai":
            self.model_combo.setCurrentText(config.get("OPENAI_MODEL", "gpt-4o-mini"))
        else:
            self.model_combo.setCurrentText(config.get("DEEPSEEK_MODEL", "deepseek-chat"))

        # Show the key for whichever provider is selected
        self.show_provider_key()

        self.temperature_input.setText(str(config.get("TEMPERATURE", 0.2)))
        self.max_tokens_input.setText(str(config.get("MAX_TOKENS", 200)))

    def get_updated_config(self) -> dict:
        provider = self.provider_combo.currentText()
        if provider == "openai":
            self.config["OPENAI_MODEL"] = self.model_combo.currentText()
        else:
            self.config["DEEPSEEK_MODEL"] = self.model_combo.currentText()

        # Store the single key for this provider
        self.config.setdefault("API_KEYS", {})
        self.config["API_KEYS"][provider] = self.api_key_input.text()

        self.config["AI_PROVIDER"] = provider
        self.config["TEMPERATURE"] = float(self.temperature_input.text())
        self.config["MAX_TOKENS"] = int(self.max_tokens_input.text())

        return self.config

    def update_model_options(self):
        provider = self.provider_combo.currentText()
        self.model_combo.clear()
        if provider == "openai":
            self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o", "o3-mini", "o1-mini"])
        elif provider == "deepseek":
            self.model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])

        self.show_provider_key()

    def show_provider_key(self):
        provider = self.provider_combo.currentText()
        # Show the single key for that provider
        key = self.config.get("API_KEYS", {}).get(provider, "")
        self.api_key_input.setText(key)

    def show_log(self) -> None:
        log_path = os.path.join(os.path.dirname(__file__), "omnPrompt-anki.log")
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log_content = f.read()
        except Exception as e:
            safe_show_info(f"Failed to load log file: {e}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("OmniPrompt Anki Log")
        dlg.setMinimumSize(600, 400)
        lay = QVBoxLayout(dlg)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(log_content)
        lay.addWidget(txt)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn)

        dlg.exec()

# ----------------------------------------------------------------
# AdvancedSettingsDialog
# ----------------------------------------------------------------
class AdvancedSettingsDialog(QDialog):
    """No append/replace. Just API_DELAY, TIMEOUT, DEEPSEEK_STREAM."""
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Advanced Settings")
        self.setMinimumWidth(400)
        self.config = omni_prompt_manager.config
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()
        form_layout = QFormLayout()

        # API Delay
        self.api_delay_input = QLineEdit()
        self.api_delay_input.setValidator(QDoubleValidator(0.0, 60.0, 2, self))
        self.api_delay_input.setText(str(self.config.get("API_DELAY", 1)))
        form_layout.addRow("API Delay (seconds):", self.api_delay_input)

        # API Timeout
        self.timeout_input = QLineEdit()
        self.timeout_input.setValidator(QDoubleValidator(0.0, 300.0, 1, self))
        self.timeout_input.setText(str(self.config.get("TIMEOUT", 20)))
        form_layout.addRow("API Timeout (seconds):", self.timeout_input)

        # DeepSeek Stream
        self.deepseek_stream_combo = QComboBox()
        self.deepseek_stream_combo.addItems(["False", "True"])
        if self.config.get("DEEPSEEK_STREAM", False):
            self.deepseek_stream_combo.setCurrentText("True")
        else:
            self.deepseek_stream_combo.setCurrentText("False")
        form_layout.addRow("DeepSeek Streaming:", self.deepseek_stream_combo)

        layout.addLayout(form_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        save_button = QPushButton("Save")
        save_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        btn_layout.addWidget(save_button)
        btn_layout.addWidget(cancel_button)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def accept(self) -> None:
        try:
            delay = float(self.api_delay_input.text())
            timeout_val = float(self.timeout_input.text())
        except ValueError:
            safe_show_info("Invalid numeric input.")
            return

        self.config["API_DELAY"] = delay
        self.config["TIMEOUT"] = timeout_val
        self.config["DEEPSEEK_STREAM"] = (self.deepseek_stream_combo.currentText() == "True")

        omni_prompt_manager.save_config()
        super().accept()

# ----------------------------------------------------------------
# UpdateOmniPromptDialog
# ----------------------------------------------------------------
class UpdateOmniPromptDialog(QDialog):
    def __init__(self, notes, manager: OmniPromptManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update with OmniPrompt")
        self.notes = notes
        self.manager = manager
        self.worker = None
        self.setup_ui()

        self.setWindowModality(Qt.WindowModality.NonModal)

    def setup_ui(self):
        main_layout = QHBoxLayout(self)
        left_panel = QVBoxLayout()

        # Saved Prompts
        left_panel.addWidget(QLabel("Saved Prompts:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setEditable(True)
        self.prompt_completer = QCompleter(self)
        self.prompt_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.prompt_combo.setCompleter(self.prompt_completer)
        left_panel.addWidget(self.prompt_combo)

        # Prompt Template
        left_panel.addWidget(QLabel("Prompt Template:"))
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.prompt_edit.setPlainText(self.manager.config.get("PROMPT", ""))
        left_panel.addWidget(self.prompt_edit)

        # Save Prompt
        self.save_prompt_button = QPushButton("Save Current Prompt")
        left_panel.addWidget(self.save_prompt_button)

        # Output Field
        left_panel.addWidget(QLabel("Output Field:"))
        self.output_field_combo = QComboBox()
        if self.notes:
            first_note = self.notes[0]
            model = mw.col.models.get(first_note.mid)
            if model:
                fields = mw.col.models.field_names(model)
                self.output_field_combo.addItems(fields)
        left_panel.addWidget(self.output_field_combo)

        # Append CheckBox
        self.append_checkbox = QCheckBox("Append Output")
        self.append_checkbox.setChecked(self.manager.config.get("APPEND_OUTPUT", False))
        self.append_checkbox.stateChanged.connect(self.on_append_checkbox_changed)
        left_panel.addWidget(self.append_checkbox)

        # Start / Stop / Save Edits
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.save_changes_button = QPushButton("Save Manual Edits")
        left_panel.addWidget(self.start_button)
        left_panel.addWidget(self.stop_button)
        left_panel.addWidget(self.save_changes_button)

        main_layout.addLayout(left_panel, 1)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Progress", "Original", "Generated"])
        self.table.horizontalHeader().setStretchLastSection(True)
        main_layout.addWidget(self.table, 3)

        self.setLayout(main_layout)

        # Connect signals
        self.save_prompt_button.clicked.connect(self.save_current_prompt)
        self.start_button.clicked.connect(self.start_processing)
        self.stop_button.clicked.connect(self.stop_processing)
        self.save_changes_button.clicked.connect(self.save_manual_edits)

        self.load_prompts()
        if self.prompt_combo.currentText():
            self.load_selected_prompt(self.prompt_combo.currentText())
        self.prompt_combo.currentTextChanged.connect(self.load_selected_prompt)

        # Shortcut for Start
        sc = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc.activated.connect(self.start_processing)

    def on_append_checkbox_changed(self, state: int):
        logger.info(f"on_append_checkbox_changed start: state={state}")
        # Properly handle Qt.CheckState (0=Unchecked, 2=Checked)
        is_checked = bool(state)  # Convert Qt.CheckState to boolean
        logger.info(f"is_checked = {is_checked}")
        self.manager.config["APPEND_OUTPUT"] = is_checked
        logger.info(f"post-assign, config[APPEND_OUTPUT]={self.manager.config['APPEND_OUTPUT']}")
        try:
            self.manager.save_config()
            # Verify config was saved by reloading
            reloaded_config = self.manager.load_config()
            logger.info(f"Reloaded config APPEND_OUTPUT={reloaded_config.get('APPEND_OUTPUT')}")
            # Update checkbox to match saved state
            self.append_checkbox.setChecked(bool(reloaded_config.get("APPEND_OUTPUT", False)))
        except Exception as e:
            logger.exception("Failed to save/reload config:")
        logger.info(f"done saving config")

    def load_prompts(self):
        prompts = load_prompt_templates()
        self.prompt_combo.clear()
        for title in sorted(prompts.keys()):
            self.prompt_combo.addItem(title)
        self.prompt_completer.setModel(self.prompt_combo.model())

    def load_selected_prompt(self, text: str):
        prompts = load_prompt_templates()
        if text in prompts:
            self.prompt_edit.setPlainText(prompts[text])
        # Check if there's a saved outputField
        ps = load_prompt_settings()
        if text in ps:
            out_field = ps[text].get("outputField")
            if out_field and out_field in [self.output_field_combo.itemText(i) for i in range(self.output_field_combo.count())]:
                self.output_field_combo.setCurrentText(out_field)

    def save_current_prompt(self):
        name, ok = getText("Enter a name for the prompt:")
        if ok and name:
            p = load_prompt_templates()
            p[name] = self.prompt_edit.toPlainText()
            save_prompt_templates(p)

            ps = load_prompt_settings()
            ps.setdefault(name, {})
            ps[name]["outputField"] = self.output_field_combo.currentText()
            save_prompt_settings(ps)

            self.load_prompts()
            self.prompt_combo.setCurrentText(name)
            showInfo("Prompt saved.")

    def start_processing(self):
        note_prompts = []
        prompt_template = self.prompt_edit.toPlainText()
        output_field = self.output_field_combo.currentText().strip()
        if not output_field:
            safe_show_info("Please select an output field.")
            return

        for note in self.notes:
            try:
                formatted_prompt = prompt_template.format(**note)
            except KeyError as e:
                safe_show_info(f"Missing field {e} in note {note.id}")
                continue
            note_prompts.append((note, formatted_prompt))

        if not note_prompts:
            safe_show_info("No valid notes to process.")
            return

        self.table.setRowCount(len(note_prompts))
        for row, (note, _) in enumerate(note_prompts):
            progress_item = QTableWidgetItem("0%")
            try:
                original_text = note[output_field]
            except Exception:
                original_text = ""

            original_item = QTableWidgetItem(original_text)
            original_item.setData(Qt.ItemDataRole.UserRole, note.id)
            generated_item = QTableWidgetItem("")

            self.table.setItem(row, 0, progress_item)
            self.table.setItem(row, 1, original_item)
            self.table.setItem(row, 2, generated_item)

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self.worker = NoteProcessingWorker(note_prompts, self._generate_with_progress)
        self.worker.progress_update.connect(self.update_progress_cell, Qt.ConnectionType.QueuedConnection)
        self.worker.note_result.connect(self.update_note_result, Qt.ConnectionType.QueuedConnection)
        self.worker.finished_processing.connect(self.processing_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def stop_processing(self):
        if self.worker:
            self.worker.cancel()
            self.stop_button.setEnabled(False)

    def _generate_with_progress(self, prompt, stream_progress_callback=None):
        return self.manager.generate_ai_response(prompt, stream_progress_callback)

    def update_progress_cell(self, row_index: int, pct: int):
        item = self.table.item(row_index, 0)
        if item:
            item.setText(f"{pct}%")

    def update_note_result(self, note, explanation: str):
        output_field = self.output_field_combo.currentText().strip()
        for row in range(self.table.rowCount()):
            original_item = self.table.item(row, 1)
            if original_item and original_item.data(Qt.ItemDataRole.UserRole) == note.id:
                # 100%
                self.table.item(row, 0).setText("100%")
                
                append_mode = self.manager.config.get("APPEND_OUTPUT", False)
                logger.info(f"Processing note {note.id} in {'append' if append_mode else 'replace'} mode")
                
                if append_mode:
                    existing_text = self.table.item(row, 2).text()
                    new_table_text = existing_text + "\n\n" + explanation if existing_text else explanation
                    self.table.item(row, 2).setText(new_table_text)

                    original_field_text = note[output_field]
                    new_field_text = original_field_text + "\n\n" + explanation if original_field_text else explanation
                    note[output_field] = new_field_text
                else:
                    self.table.item(row, 2).setText(explanation)
                    note[output_field] = explanation

                try:
                    mw.col.update_note(note)
                    logger.info(f"Successfully updated note {note.id}")
                except Exception as e:
                    logger.exception(f"Error updating note {note.id}: {e}")
                break

    def save_manual_edits(self):
        output_field = self.output_field_combo.currentText().strip()
        for row in range(self.table.rowCount()):
            original_item = self.table.item(row, 1)
            if not original_item:
                continue
            generated_item = self.table.item(row, 2)
            note_id = original_item.data(Qt.ItemDataRole.UserRole)
            note = mw.col.get_note(note_id)
            new_text = generated_item.text()
            try:
                note[output_field] = new_text
                mw.col.update_note(note)
            except Exception as e:
                logger.exception(f"Error saving manual edit for note {note_id}: {e}")

        safe_show_info("Manual edits saved.")

    def processing_finished(self, processed: int, total: int, error_count: int):
        safe_show_info(f"Processing finished: {processed}/{total} notes processed with {error_count} errors.")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

# ----------------------------------------------------------------
# AboutDialog
# ----------------------------------------------------------------
class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About OmniPrompt Anki")
        layout = QVBoxLayout(self)
        about_text = (
            "<h2>OmniPrompt Anki Add‑on</h2>"
            "<p>Version: 1.1.3</p>"
            "<p><a href='https://ankiweb.net/shared/review/1383162606'>Rate add-on on AnkiWeb</a></p>"
            "<p>For documentation, visit:</p>"
            "<p><a href='https://github.com/stanamosov/omniprompt-anki'>GitHub Repo</a></p>"
            "<p><a href='https://codeberg.org/stanamosov/omniprompt-anki'>Codeberg Repo</a></p>"
            "<p>Credits: Stanislav Amosov</p>"
        )
        lbl = QLabel(about_text)
        lbl.setOpenExternalLinks(True)
        layout.addWidget(lbl)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        self.setLayout(layout)

# ----------------------------------------------------------------
# Tools Menu
# ----------------------------------------------------------------
def setup_omniprompt_menu():
    tools_menu = mw.form.menuTools
    omni_menu = QMenu("OmniPrompt", mw)

    settings_action = QAction("Settings", mw)
    settings_action.triggered.connect(lambda: omni_prompt_manager.show_settings_dialog())
    omni_menu.addAction(settings_action)

    about_action = QAction("About", mw)
    about_action.triggered.connect(lambda: AboutDialog(mw).exec())
    omni_menu.addAction(about_action)

    tools_menu.addMenu(omni_menu)

# ----------------------------------------------------------------
# Browser Context
# ----------------------------------------------------------------
def on_browser_context_menu(browser: Browser, menu):
    note_ids = browser.selectedNotes()
    if note_ids:
        action = QAction("Update with OmniPrompt", browser)
        action.triggered.connect(lambda: update_notes_with_omniprompt(note_ids))
        menu.addAction(action)

gui_hooks.browser_will_show_context_menu.append(on_browser_context_menu)

def update_notes_with_omniprompt(note_ids: list):
    notes = [mw.col.get_note(nid) for nid in note_ids]
    dialog = UpdateOmniPromptDialog(notes, omni_prompt_manager, parent=mw)
    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.show()

# ----------------------------------------------------------------
# Instantiate & Setup
# ----------------------------------------------------------------
omni_prompt_manager = OmniPromptManager()
setup_omniprompt_menu()

def shortcut_update_notes():
    logger.info("Global shortcut activated.")
    browser = mw.app.activeWindow()
    if isinstance(browser, Browser):
        note_ids = browser.selectedNotes()
        if note_ids:
            update_notes_with_omniprompt(note_ids)
        else:
            showInfo("No notes selected in the browser.")
    else:
        showInfo("Browser not available.")
    print("Shortcut activated!")

shortcut_ctrl = QShortcut(QKeySequence("Ctrl+Shift+O"), mw)
shortcut_ctrl.setContext(Qt.ShortcutContext.ApplicationShortcut)
shortcut_ctrl.activated.connect(shortcut_update_notes)

shortcut_meta = QShortcut(QKeySequence("Meta+Shift+O"), mw)
shortcut_meta.setContext(Qt.ShortcutContext.ApplicationShortcut)
shortcut_meta.activated.connect(shortcut_update_notes)
