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
    QWidget, QTableWidget, QTableWidgetItem, QMenu, QCheckBox, QCompleter,
    QListWidget, QMessageBox, QSplitter
)
from aqt import mw, gui_hooks
from aqt.browser import Browser
from anki.hooks import addHook
from logging.handlers import RotatingFileHandler

# ----------------------------------------------------------------
# Constants & Config
# ----------------------------------------------------------------
AI_PROVIDERS = ["openai", "deepseek", "gemini", "anthropic", "xai"]

DEFAULT_CONFIG = {
    "_version": 1.1,
    "AI_PROVIDER": "openai",
    "OPENAI_MODEL": "gpt-4o-mini",
    "DEEPSEEK_MODEL": "deepseek-chat",
    "GEMINI_MODEL": "gemini-1.5-flash",  # Default to 1.5-pro as it's more capable
    "ANTHROPIC_MODEL": "claude-opus-4-latest",
    "XAI_MODEL": "grok-3-latest",
    # Now includes all supported providers
    "API_KEYS": {
        # "openai": "...",
        # "deepseek": "...",
        # "gemini": "...",
        # "anthropic": "...",
        # "xai": "..."
    },
    "TEMPERATURE": 0.2,
    "MAX_TOKENS": 500,
    "API_DELAY": 2,          # Delay (seconds) between API calls
    "TIMEOUT": 30,           # Request timeout
    "PROMPT": "Paste your prompt here.",
    "SELECTED_FIELDS": {
        "output_field": "Output"
    },
    "DEEPSEEK_STREAM": False,
    "APPEND_OUTPUT": False,
    "DEBUG_MODE": True,         # Show processing popups when enabled
    "FILTER_MODE": False,       # Skip notes where output field is filled
    "MULTI_FIELD_MODE": False,
    "AUTO_SEND_TO_CARD": True,
    "LAST_USED_PROMPT": ""
}

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "_version": {"type": "number"},
        "AI_PROVIDER": {"enum": AI_PROVIDERS},
        "OPENAI_MODEL": {"type": "string"},
        "DEEPSEEK_MODEL": {"type": "string"},
        "GEMINI_MODEL": {"type": "string"},
        "ANTHROPIC_MODEL": {"type": "string"},
        "XAI_MODEL": {"type": "string"},
        # Single key per provider
        "API_KEYS": {"type": "object"},
        "TEMPERATURE": {"type": "number"},
        "MAX_TOKENS": {"type": "integer"},
        "API_DELAY": {"type": "number"},
        "TIMEOUT": {"type": "number"},
        "PROMPT": {"type": "string"},
        "DEEPSEEK_STREAM": {"type": "boolean"},
        "APPEND_OUTPUT": {"type": "boolean"},
        "DEBUG_MODE": {"type": "boolean"},
        "FILTER_MODE": {"type": "boolean"},
        "MULTI_FIELD_MODE": {"type": "boolean"},
        "AUTO_SEND_TO_CARD": {"type": "boolean"},
        "LAST_USED_PROMPT": {"type": "string"},
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
MULTI_FIELD_PATTERN = r'```([\w\s]+)\n([^`]+)```'

# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def safe_show_info(message: str) -> None:
    if omni_prompt_manager.config.get("DEBUG_MODE", True):
        QTimer.singleShot(0, lambda: showInfo(message))
    else:
        logger.info(f"Debug message (not shown to user): {message}")

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
            # Strip trailing whitespace from the value before saving
            cleaned_value = value.rstrip()
            file.write(f"[[[{key}]]]\n{cleaned_value}\n")

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
        elif provider == "gemini":
            return self._make_gemini_request(prompt, stream_progress_callback)
        elif provider == "anthropic":
            return self._make_anthropic_request(prompt, stream_progress_callback)
        elif provider == "xai":
            return self._make_xai_request(prompt, stream_progress_callback)
        else:
            logger.error(f"Invalid AI provider: {provider}")
            return "[Error: Invalid provider]"

    def _make_anthropic_request(self, prompt: str, stream_callback=None) -> str:
        api_key = self.config.get("API_KEYS", {}).get("anthropic", "")
        if not api_key:
            return "[Error: No Anthropic key found]"

        model = self.config.get("ANTHROPIC_MODEL", "claude-3-opus-20240229")
        url = "https://api.anthropic.com/v1/messages"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }

        data = {
            "model": model,
            "max_tokens": self.config.get("MAX_TOKENS", 200),
            "temperature": self.config.get("TEMPERATURE", 0.2),
            "messages": [{"role": "user", "content": prompt}]
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=self.config.get("TIMEOUT", 20))
            response.raise_for_status()
            response_json = response.json()

            if "content" in response_json and response_json["content"]:
                text = response_json["content"][0]["text"]
                return text.strip()
            return "[Error: Unexpected Anthropic response format]"

        except Exception as e:
            logger.exception("Anthropic API request failed:")
            return f"[Error: {str(e)}]"

    def _make_xai_request(self, prompt: str, stream_callback=None) -> str:
        api_key = self.config.get("API_KEYS", {}).get("xai", "")
        if not api_key:
            return "[Error: No xAI key found]"

        model = self.config.get("XAI_MODEL", "grok-1.5")
        url = "https://api.x.ai/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.get("TEMPERATURE", 0.2),
            "max_tokens": self.config.get("MAX_TOKENS", 200)
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=self.config.get("TIMEOUT", 20))
            response.raise_for_status()
            response_json = response.json()

            if "choices" in response_json and response_json["choices"]:
                text = response_json["choices"][0]["message"]["content"]
                return text.strip()
            return "[Error: Unexpected xAI response format]"

        except Exception as e:
            logger.exception("xAI API request failed:")
            return f"[Error: {str(e)}]"

    def _make_gemini_request(self, prompt: str, stream_callback=None) -> str:
        api_key = self.config.get("API_KEYS", {}).get("gemini", "")
        if not api_key:
            return "[Error: No Gemini key found]"

        model = self.config.get("GEMINI_MODEL", "gemini-pro")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        headers = {
            "Content-Type": "application/json"
        }

        data = {
            "contents": [{
                "parts": [{
                    "text": prompt
                }]
            }],
            "generationConfig": {
                "temperature": self.config.get("TEMPERATURE", 0.2),
                "maxOutputTokens": self.config.get("MAX_TOKENS", 200)
            }
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=self.config.get("TIMEOUT", 20))
            response.raise_for_status()
            response_json = response.json()

            if "candidates" in response_json and response_json["candidates"]:
                text = response_json["candidates"][0]["content"]["parts"][0]["text"]
                return text.strip()
            return "[Error: Unexpected Gemini response format]"

        except Exception as e:
            logger.exception("Gemini API request failed:")
            return f"[Error: {str(e)}]"

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

        # Debug mode checkbox
        self.debug_mode_checkbox = QCheckBox("Show processing popups (Debug Mode)")
        self.debug_mode_checkbox.setChecked(True)
        api_layout.addRow(self.debug_mode_checkbox)

        # Filter mode checkbox
        self.filter_mode_checkbox = QCheckBox("Skip notes where output field is filled (Filter Mode)")
        self.filter_mode_checkbox.setChecked(False)
        api_layout.addRow(self.filter_mode_checkbox)

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
        elif provider == "deepseek":
            self.model_combo.setCurrentText(config.get("DEEPSEEK_MODEL", "deepseek-chat"))
        elif provider == "gemini":
            self.model_combo.setCurrentText(config.get("GEMINI_MODEL", "gemini-pro"))
        elif provider == "anthropic":
            self.model_combo.setCurrentText(config.get("ANTHROPIC_MODEL", "claude-opus-4-latest"))
        elif provider == "xai":
            self.model_combo.setCurrentText(config.get("XAI_MODEL", "grok-3-latest"))

        # Show the key for whichever provider is selected
        self.show_provider_key()

        self.temperature_input.setText(str(config.get("TEMPERATURE", 0.2)))
        self.max_tokens_input.setText(str(config.get("MAX_TOKENS", 200)))
        self.debug_mode_checkbox.setChecked(config.get("DEBUG_MODE", True))
        self.filter_mode_checkbox.setChecked(config.get("FILTER_MODE", False))


    def get_updated_config(self) -> dict:
        provider = self.provider_combo.currentText()
        if provider == "openai":
            self.config["OPENAI_MODEL"] = self.model_combo.currentText()
        elif provider == "deepseek":
            self.config["DEEPSEEK_MODEL"] = self.model_combo.currentText()
        elif provider == "gemini":
            self.config["GEMINI_MODEL"] = self.model_combo.currentText()
        elif provider == "anthropic":
            self.config["ANTHROPIC_MODEL"] = self.model_combo.currentText()
        elif provider == "xai":
            self.config["XAI_MODEL"] = self.model_combo.currentText()

        # Store the single key for this provider
        self.config.setdefault("API_KEYS", {})
        self.config["API_KEYS"][provider] = self.api_key_input.text()

        self.config["AI_PROVIDER"] = provider
        self.config["TEMPERATURE"] = float(self.temperature_input.text())
        self.config["MAX_TOKENS"] = int(self.max_tokens_input.text())
        self.config["DEBUG_MODE"] = self.debug_mode_checkbox.isChecked()
        self.config["FILTER_MODE"] = self.filter_mode_checkbox.isChecked()

        return self.config

    def update_model_options(self):
        provider = self.provider_combo.currentText()
        self.model_combo.clear()
        if provider == "openai":
            self.model_combo.addItems(["gpt-4o-mini", "gpt-3.5-turbo", "gpt-4o", "o3-mini", "o1-mini"])
        elif provider == "deepseek":
            self.model_combo.addItems(["deepseek-chat", "deepseek-reasoner"])
        elif provider == "gemini":
            self.model_combo.addItems(["gemini-pro", "gemini-1.5-pro", "gemini-flash"])
        elif provider == "anthropic":
            self.model_combo.addItems(["claude-opus-4-latest", "claude-sonnet-4-latest", "claude-haiku-3.5-latest"])
        elif provider == "xai":
            self.model_combo.addItems(["grok-3-latest", "grok-3-mini-latest"])

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
        self.setMinimumSize(800, 600)  # width and height
        # You could also use self.resize(800, 600) if you want it to open at a fixed size
        self.setup_ui()

        self.setWindowModality(Qt.WindowModality.NonModal)
        self.multi_field_mode = self.manager.config.get("MULTI_FIELD_MODE", False) # Initialize from config
        self.auto_detect_fields = []  # Store auto-detected field names

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

        # Add Multi-field Mode checkbox
        self.multi_field_checkbox = QCheckBox("Auto-detect multiple output fields")
        self.multi_field_checkbox.setChecked(self.manager.config.get("MULTI_FIELD_MODE", False))
        self.multi_field_checkbox.stateChanged.connect(self.toggle_multi_field_mode)
        left_panel.addWidget(self.multi_field_checkbox)

        # NEW: Auto Send Data to Card Checkbox
        self.auto_send_checkbox = QCheckBox("Automatically Send Data to Card")
        self.auto_send_checkbox.setChecked(self.manager.config.get("AUTO_SEND_TO_CARD", True))
        self.auto_send_checkbox.stateChanged.connect(self.on_auto_send_checkbox_changed)
        left_panel.addWidget(self.auto_send_checkbox)


        # Start / Stop / Save Edits
        self.start_button = QPushButton("Start")
        self.start_button.setMinimumSize(80, 30)  # Make it slightly bigger
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.save_changes_button = QPushButton("Send Data To Card")
        left_panel.addWidget(self.start_button)
        left_panel.addWidget(self.stop_button)
        left_panel.addWidget(self.save_changes_button)

        main_layout.addLayout(left_panel, 1)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Progress", "Original", "Generated"])
        self.table.horizontalHeader().setStretchLastSection(True)

        # Increase default row height
        self.table.verticalHeader().setDefaultSectionSize(35) # Default is around 25-30
        # Increase default column width for better horizontal spacing
        self.table.horizontalHeader().setDefaultSectionSize(150) # Adjust as needed
        # You can also set a minimum section size for more consistent spacing
        self.table.horizontalHeader().setMinimumSectionSize(100)
        self.table.verticalHeader().setMinimumSectionSize(30)
        main_layout.addWidget(self.table, 3)

        self.setLayout(main_layout)

        # Connect signals
        self.save_prompt_button.clicked.connect(self.save_current_prompt)
        self.start_button.clicked.connect(self.start_processing)
        self.stop_button.clicked.connect(self.stop_processing)
        self.save_changes_button.clicked.connect(self.save_manual_edits)

        self.load_prompts()

        # Load the last used prompt from config
        last_used_prompt = self.manager.config.get("LAST_USED_PROMPT", "")
        if last_used_prompt and last_used_prompt in load_prompt_templates():
            self.prompt_combo.setCurrentText(last_used_prompt)
            self.load_selected_prompt(last_used_prompt)
        elif self.prompt_combo.count() > 0:
            # If no last used prompt or it's not found, load the first one if available
            self.prompt_combo.setCurrentIndex(0)
            self.load_selected_prompt(self.prompt_combo.currentText())
        self.prompt_combo.currentTextChanged.connect(self.load_selected_prompt)

        # Shortcut for Start
        sc = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc.activated.connect(self.start_processing)

        # Apply initial multi-field mode state
        self.toggle_multi_field_mode(self.manager.config.get("MULTI_FIELD_MODE", False))

    def parse_fields_for_selected(self):
        """Parse generated text for selected rows into fields"""
        selected_rows = [idx.row() for idx in self.table.selectionModel().selectedRows()]
        if not selected_rows:
            safe_show_info("Please select at least one row.")
            return

        for row in selected_rows:
            generated_item = self.table.item(row, 2)
            if not generated_item:
                continue

            explanation = generated_item.text()
            field_map = self.parse_multi_field_output(explanation)

            # Update the table columns
            for col, field_name in enumerate(self.auto_detect_fields, start=3):
                # Ensure we have enough columns
                if col >= self.table.columnCount():
                    self.table.setColumnCount(col + 1)
                    self.table.setHorizontalHeaderItem(
                        col, QTableWidgetItem(f"Field {col-2}")
                    )

                # Create or get the column item
                if self.table.item(row, col) is None:
                    self.table.setItem(row, col, QTableWidgetItem())

                # Set content if field exists in parsed output
                if field_name in field_map:
                    self.table.item(row, col).setText(field_map[field_name])

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

    def on_auto_send_checkbox_changed(self, state: int):
        is_checked = bool(state)
        self.manager.config["AUTO_SEND_TO_CARD"] = is_checked
        try:
            self.manager.save_config()
        except Exception as e:
            logger.exception("Failed to save auto-send to card setting:")
            if self.manager.config.get("DEBUG_MODE", False):
                safe_show_info(f"Failed to save setting: {str(e)}")

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
        current_name = self.prompt_combo.currentText().strip()
        name, ok = getText("Enter a name for the prompt:",
            default=current_name)
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

    def toggle_multi_field_mode(self, state):
        """Enable/disable multi-field output mode and adjust table layout."""
        is_checked = bool(state)
        self.multi_field_mode = is_checked

        # Save to config with error handling
        self.manager.config["MULTI_FIELD_MODE"] = is_checked
        try:
            self.manager.save_config()
        except Exception as e:
            logger.exception("Failed to save multi-field mode setting:")
            if self.manager.config.get("DEBUG_MODE", False):
                safe_show_info(f"Failed to save setting: {str(e)}")

        # Update UI controls
        self.output_field_combo.setEnabled(not self.multi_field_mode)
        self.append_checkbox.setEnabled(not self.multi_field_mode)
        
        # Clear the table when switching modes to prevent an inconsistent state
        self.table.setRowCount(0)
        self.table.setColumnCount(0)

        if self.multi_field_mode:
            # Multi-field mode setup: Remove "Original" column
            self.table.setColumnCount(2)
            self.table.setHorizontalHeaderLabels(["Progress", "Generated"])

            # Add parse button if it doesn't exist
            if not hasattr(self, 'parse_fields_button'):
                self.parse_fields_button = QPushButton("Re-Parse Fields for All Rows")
                self.parse_fields_button.clicked.connect(self.parse_fields_for_all_rows)
                layout = self.layout().itemAt(0).layout()
                try:
                    insert_idx = [layout.itemAt(i).widget() for i in range(layout.count())].index(self.multi_field_checkbox) + 1
                    layout.insertWidget(insert_idx, self.parse_fields_button)
                except (ValueError, AttributeError):
                    layout.addWidget(self.parse_fields_button)
        else:
            # Single-field mode cleanup: Restore "Original" column
            self.table.setColumnCount(3)
            self.table.setHorizontalHeaderLabels(["Progress", "Original", "Generated"])

            # Remove parse button if it exists
            if hasattr(self, 'parse_fields_button'):
                self.parse_fields_button.deleteLater()
                del self.parse_fields_button

            # Clear detected fields
            self.auto_detect_fields = []

    def start_processing(self):
        note_prompts = []
        prompt_template = self.prompt_edit.toPlainText()
        output_field = self.output_field_combo.currentText().strip()
        
        # Only require output field if not in multi-field mode
        if not self.multi_field_mode and not output_field:
            safe_show_info("Please select an output field or enable multi-field mode.")
            return

        filter_mode = self.manager.config.get("FILTER_MODE", False)
        skipped_count = 0

        for note in self.notes:
            try:
                # Skip note if filter mode is on and output field is not empty
                if filter_mode and not self.multi_field_mode and note[output_field].strip():
                    skipped_count += 1
                    continue
                formatted_prompt = prompt_template.format(**note)
            except KeyError as e:
                safe_show_info(f"Missing field {e} in note {note.id}")
                continue
            note_prompts.append((note, formatted_prompt))

        if filter_mode and skipped_count > 0:
            logger.info(f"Filter mode skipped {skipped_count} notes with filled output fields")
            if self.manager.config.get("DEBUG_MODE", True):
                safe_show_info(f"Skipped {skipped_count} notes with filled output fields")

        if not note_prompts:
            safe_show_info("No valid notes to process.")
            return

        self.auto_detect_fields = []  # Clear auto-detect fields when starting

        if self.multi_field_mode:
            # Create two rows per note: one for generated, one for original
            self.table.setRowCount(len(note_prompts) * 2)
            for i, (note, _) in enumerate(note_prompts):
                gen_row, orig_row = i * 2, i * 2 + 1

                # Generated Row
                progress_item = QTableWidgetItem("0%")
                progress_item.setData(Qt.ItemDataRole.UserRole, note.id)
                self.table.setItem(gen_row, 0, progress_item)
                self.table.setItem(gen_row, 1, QTableWidgetItem(""))  # Placeholder for generated content

                # Original Row
                original_label = QTableWidgetItem("Original")
                original_label.setData(Qt.ItemDataRole.UserRole, note.id) # Also store note_id here
                self.table.setItem(orig_row, 0, original_label)
                self.table.setSpan(orig_row, 0, 1, 2) # Span label across the first two columns
        else:
            # Original single-field mode: one row per note
            self.table.setRowCount(len(note_prompts))
            for row, (note, _) in enumerate(note_prompts):
                progress_item = QTableWidgetItem("0%")
                original_text = note[output_field] if output_field and output_field in note else ""
                original_item = QTableWidgetItem(original_text)
                original_item.setData(Qt.ItemDataRole.UserRole, note.id)
                self.table.setItem(row, 0, progress_item)
                self.table.setItem(row, 1, original_item)
                self.table.setItem(row, 2, QTableWidgetItem(""))

        # Save the current prompt as the last used prompt
        current_prompt_name = self.prompt_combo.currentText().strip()
        if current_prompt_name:
            self.manager.config["LAST_USED_PROMPT"] = current_prompt_name
            self.manager.save_config()
            logger.info(f"Saved '{current_prompt_name}' as last used prompt.")

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker = NoteProcessingWorker(note_prompts, self._generate_with_progress)
        self.worker.progress_update.connect(self.update_progress_cell, Qt.ConnectionType.QueuedConnection)
        self.worker.note_result.connect(self.update_note_result, Qt.ConnectionType.QueuedConnection)
        self.worker.finished_processing.connect(self.processing_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def parse_multi_field_output(self, explanation: str) -> dict:
        """Parse AI output into multiple fields using various patterns"""
        import re
        field_map = {}

        # Pattern 1: Code block markers (original)
        pattern1 = r'```\s*([\w\s]+)\s*\n([\s\S]*?)\s*```'
        matches1 = re.findall(pattern1, explanation, re.DOTALL)

        # Pattern 2: XML-like tags
        pattern2 = r'<([\w\s]+)>\s*([\s\S]*?)\s*</\1>'
        matches2 = re.findall(pattern2, explanation, re.DOTALL)

        # Combine matches from both patterns
        all_matches = matches1 + matches2

        for field_name, field_content in all_matches:
            # Clean up field name and content
            field_name = field_name.strip()
            field_content = field_content.strip()

            # Only process non-empty fields
            if field_name and field_content:
                field_map[field_name] = field_content
        return field_map

    def update_note_result(self, note, explanation: str):
        """Handles a single AI response, updating the table and optionally auto-saving."""
        auto_send = self.manager.config.get("AUTO_SEND_TO_CARD", True)
        
        if self.multi_field_mode:
            # Find the generated row (even rows) for this note
            for row in range(0, self.table.rowCount(), 2):
                progress_item = self.table.item(row, 0)
                if progress_item and progress_item.data(Qt.ItemDataRole.UserRole) == note.id:
                    progress_item.setText("100%")
                    self.table.item(row, 1).setText(explanation) # Generated content in column 1
                    logger.info(f"Multi-field: Stored raw explanation for note {note.id}.")

                    # If auto-send is on, parse and save the note immediately.
                    if auto_send:
                        logger.info(f"Multi-field: Auto-sending to note {note.id}.")
                        field_map = self.parse_multi_field_output(explanation)
                        
                        changes_made = False
                        for field_name, content in field_map.items():
                            if field_name in note and note[field_name] != content:
                                note[field_name] = content
                                changes_made = True

                        if changes_made:
                            try:
                                mw.col.update_note(note)
                                logger.info(f"Successfully auto-updated multi-field note {note.id}")
                            except Exception as e:
                                logger.exception(f"Error auto-updating multi-field note {note.id}: {e}")
                        else:
                            logger.info(f"No new content to update for multi-field note {note.id}")
                    break
        else: # Single-field mode
            for row in range(self.table.rowCount()):
                original_item = self.table.item(row, 1)
                if original_item and original_item.data(Qt.ItemDataRole.UserRole) == note.id:
                    self.table.item(row, 0).setText("100%")
                    self.table.item(row, 2).setText(explanation) # Generated content in column 2

                    if auto_send:
                        output_field = self.output_field_combo.currentText().strip()
                        append_mode = self.manager.config.get("APPEND_OUTPUT", False)
                        logger.info(f"Single-field: Auto-sending to note {note.id} in {'append' if append_mode else 'replace'} mode.")
                        
                        if append_mode:
                            original_field_text = note[output_field]
                            note[output_field] = (original_field_text + "\n\n" + explanation) if original_field_text else explanation
                        else:
                            note[output_field] = explanation
                        
                        try:
                            mw.col.update_note(note)
                            logger.info(f"Successfully auto-updated note {note.id}")
                        except Exception as e:
                            logger.exception(f"Error auto-updating note {note.id}: {e}")
                    else:
                        logger.info(f"Single-field: Auto-send is off for note {note.id}.")
                    break

    def stop_processing(self):
        if self.worker:
            self.worker.cancel()
            self.stop_button.setEnabled(False)

    def _generate_with_progress(self, prompt, stream_progress_callback=None):
        return self.manager.generate_ai_response(prompt, stream_progress_callback)

    def update_progress_cell(self, note_index: int, pct: int):
        # In multi-field mode, progress is on the 'generated' row (even rows)
        row_index = note_index * 2 if self.multi_field_mode else note_index
        item = self.table.item(row_index, 0)
        if item:
            item.setText(f"{pct}%")

    def save_manual_edits(self):
        """Saves the current content from the table cells to the Anki notes."""
        if self.multi_field_mode:
            # Iterate through the generated rows (even-numbered) to get new data
            for row in range(0, self.table.rowCount(), 2):
                progress_item = self.table.item(row, 0)
                if not progress_item: continue

                note_id = progress_item.data(Qt.ItemDataRole.UserRole)
                note = mw.col.get_note(note_id)

                # Update each detected field (starting from column 2)
                for col, field_name in enumerate(self.auto_detect_fields, start=2):
                    if col < self.table.columnCount():
                        item = self.table.item(row, col)
                        if item and field_name in note:
                            note[field_name] = item.text()
                
                try:
                    mw.col.update_note(note)
                    logger.info(f"Successfully saved multi-field edits for note {note_id}")
                except Exception as e:
                    logger.exception(f"Error saving manual multi-field edit for note {note_id}: {e}")
            safe_show_info("All multi-field edits saved to notes.")
        else:
            # Original single-field save logic
            output_field = self.output_field_combo.currentText().strip()
            if not output_field:
                safe_show_info("Please select an output field to save changes.")
                return

            for row in range(self.table.rowCount()):
                original_item = self.table.item(row, 1)
                if not original_item: continue

                note_id = original_item.data(Qt.ItemDataRole.UserRole)
                note = mw.col.get_note(note_id)
                new_text = self.table.item(row, 2).text()

                try:
                    note[output_field] = new_text
                    mw.col.update_note(note)
                    logger.info(f"Successfully saved single-field edit for note {note_id}")
                except Exception as e:
                    logger.exception(f"Error saving manual single-field edit for note {note_id}: {e}")
            safe_show_info("All single-field edits saved to notes.")


    def processing_finished(self, processed: int, total: int, error_count: int):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        auto_send = self.manager.config.get("AUTO_SEND_TO_CARD", True)
        
        # In multi-field mode, parse all results now that they are complete to update the UI
        if self.multi_field_mode:
            # Pass save_to_notes=False because if auto_send was on, notes are already saved.
            # If auto_send was off, they shouldn't be saved here anyway until the user clicks the button.
            self.parse_fields_for_all_rows(save_to_notes=False)
            if auto_send:
                safe_show_info("Processing finished. Multi-field data has been automatically sent to cards.")
            else:
                safe_show_info("Processing finished. Press 'Send Data To Card' to save changes.")
        else: # Single-field mode
            message = f"Processing finished. Processed: {processed}/{total} notes."
            if error_count > 0:
                message += f" Errors: {error_count}."
            if not auto_send:
                message += " Press 'Send Data To Card' to save changes."
            safe_show_info(message)


    def parse_fields_for_all_rows(self, save_to_notes: bool = False):
        """Parse generated text for all notes into fields, updating the table layout.
        If save_to_notes is True, also saves changes to the Anki notes.
        """
        if not self.multi_field_mode:
            return

        # 1. Collect field maps and all unique field names from 'Generated' rows
        all_fields = set()
        note_field_maps = []
        for row in range(0, self.table.rowCount(), 2):
            explanation = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            field_map = self.parse_multi_field_output(explanation)
            note_field_maps.append(field_map)
            all_fields.update(field_map.keys())

        # 2. Update table structure with the detected fields
        self.auto_detect_fields = sorted(list(all_fields))
        new_column_count = 2 + len(self.auto_detect_fields)
        self.table.setColumnCount(new_column_count)
        self.table.setHorizontalHeaderLabels(["Progress", "Generated"] + self.auto_detect_fields)

        # 3. Populate all rows (generated and original) with data
        for i, field_map in enumerate(note_field_maps):
            gen_row, orig_row = i * 2, i * 2 + 1
            progress_item = self.table.item(gen_row, 0)
            if not progress_item: continue
            
            note_id = progress_item.data(Qt.ItemDataRole.UserRole)
            note = mw.col.get_note(note_id)

            # Ensure the 'Original' label spans the new column count correctly
            self.table.setSpan(orig_row, 0, 1, 2)

            for col_idx, field_name in enumerate(self.auto_detect_fields):
                target_col = 2 + col_idx

                # Populate generated row with parsed content
                self.table.setItem(gen_row, target_col, QTableWidgetItem(field_map.get(field_name, "")))
                
                # Populate original row with content from the note
                original_content = note[field_name] if field_name in note else ""
                self.table.setItem(orig_row, target_col, QTableWidgetItem(original_content))

            # 4. Conditionally save the new content to the Anki note
            if save_to_notes:
                for field_name, content in field_map.items():
                    if field_name in note:
                        note[field_name] = content
                try:
                    mw.col.update_note(note)
                    logger.info(f"Auto-saved multi-field content for note {note_id}")
                except Exception as e:
                    logger.exception(f"Error auto-saving multi-field note {note_id}: {e}")


# ----------------------------------------------------------------
# ManagePromptsDialog
# ----------------------------------------------------------------
class ManagePromptsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Saved Prompts")
        self.setMinimumSize(600, 500)
        self.init_ui()
        self.load_prompts()

    def init_ui(self):
        # Main splitter layout
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel - prompt list
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)

        self.prompt_list = QListWidget()
        self.prompt_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.prompt_list.itemSelectionChanged.connect(self.update_preview)
        left_layout.addWidget(QLabel("Saved Prompts:"))
        left_layout.addWidget(self.prompt_list)

        # Right panel - preview
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        self.preview_name = QLabel("Select a prompt to preview")
        self.preview_name.setStyleSheet("font-weight: bold; font-size: 14px;")
        right_layout.addWidget(self.preview_name)

        self.preview_content = QTextEdit()
        self.preview_content.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        right_layout.addWidget(QLabel("Prompt Content:"))
        right_layout.addWidget(self.preview_content)

        self.field_info = QLabel()
        right_layout.addWidget(QLabel("Field Mapping:"))
        right_layout.addWidget(self.field_info)

        # Add panels to splitter
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([250, 350])

        # Buttons
        btn_layout = QHBoxLayout()
        self.save_button = QPushButton("Save Changes")
        self.save_button.clicked.connect(self.save_changes)
        self.delete_button = QPushButton("Delete Selected")
        self.delete_button.clicked.connect(self.delete_selected)
        self.cancel_button = QPushButton("Close")
        self.cancel_button.clicked.connect(self.reject)
        btn_layout.addWidget(self.save_button)
        btn_layout.addWidget(self.delete_button)
        btn_layout.addWidget(self.cancel_button)

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.addWidget(splitter)
        main_layout.addLayout(btn_layout)

    def load_prompts(self):
        prompts = load_prompt_templates()
        self.prompt_list.clear()
        for title in sorted(prompts.keys()):
            self.prompt_list.addItem(title)

    def update_preview(self):
        selected = self.prompt_list.selectedItems()
        if not selected:
            self.preview_name.setText("Select a prompt to preview")
            self.preview_content.clear()
            self.field_info.clear()
            return

        prompt_name = selected[0].text()
        prompts = load_prompt_templates()
        prompt_settings = load_prompt_settings()

        self.preview_name.setText(f"Preview: {prompt_name}")
        self.preview_content.setPlainText(prompts.get(prompt_name, ""))

        # Show field mapping if exists
        field = prompt_settings.get(prompt_name, {}).get("outputField", "Not set")
        self.field_info.setText(f"Output field: {field}")

    def save_changes(self):
        selected_items = self.prompt_list.selectedItems()
        if not selected_items:
            return

        prompt_name = selected_items[0].text()
        new_content = self.preview_content.toPlainText()

        prompts = load_prompt_templates()
        if prompt_name in prompts:
            prompts[prompt_name] = new_content
            save_prompt_templates(prompts)
            showInfo("Prompt changes saved successfully.")
        else:
            showInfo("Error: Prompt not found")

    def delete_selected(self):
        selected_items = self.prompt_list.selectedItems()
        if not selected_items:
            return

        # Enhanced confirmation dialog
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Confirm Deletion")

        if len(selected_items) == 1:
            prompt_name = selected_items[0].text()
            content = load_prompt_templates().get(prompt_name, "")
            msg.setText(f"Delete prompt '{prompt_name}'?")
            msg.setInformativeText(
                f"Prompt length: {len(content)} characters\n"
                "This action cannot be undone."
            )
        else:
            msg.setText(f"Delete {len(selected_items)} selected prompts?")
            msg.setInformativeText("This action cannot be undone.")

        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        prompts = load_prompt_templates()
        prompt_settings = load_prompt_settings()

        deleted_count = 0
        for item in selected_items:
            prompt_name = item.text()
            if prompt_name in prompts:
                del prompts[prompt_name]
                deleted_count += 1
            if prompt_name in prompt_settings:
                del prompt_settings[prompt_name]

        save_prompt_templates(prompts)
        save_prompt_settings(prompt_settings)
        self.load_prompts()
        self.update_preview()
        showInfo(f"Deleted {deleted_count} prompt(s).")

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
            "<p>Version: 1.1.5</p>"
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

    manage_action = QAction("Manage Prompts", mw)
    manage_action.triggered.connect(lambda: ManagePromptsDialog(mw).exec())
    omni_menu.addAction(manage_action)

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