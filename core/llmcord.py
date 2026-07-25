import asyncio
import atexit
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Literal, Optional

# ── Suppress noisy warnings ──
# PyTorch quantize deprecation (PocketTTS imports torch internally)
warnings.filterwarnings("ignore", category=UserWarning, module="torch.ao")
# SQLite WAL-reset corruption warning (upstream issue in holo plugin, benign here)
warnings.filterwarnings("ignore", message=".*WAL-reset corruption.*")
# Any other torch/warnings that spam the terminal
warnings.filterwarnings("ignore", category=DeprecationWarning, module="torch")

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows-only fallback
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows-only fallback
    fcntl = None

import discord
from discord.app_commands import Choice
from discord.ext import commands
from discord.ui import LayoutView, TextDisplay
import httpx
from openai import AsyncOpenAI
import yaml

# ── PocketTTS multipart helper ──
def _pocket_tts_multipart(url, fields, files, timeout=600):
    import urllib.request
    import uuid
    boundary = "----" + uuid.uuid4().hex
    CRLF = b"\r\n"
    body = bytearray()
    for key, value in fields.items():
        body.extend(b"--" + boundary.encode() + CRLF)
        line = 'Content-Disposition: form-data; name="' + key + '"\r\n\r\n'
        body.extend(line.encode())
        body.extend(value.encode() + CRLF)
    for key, filepath in files.items():
        with open(filepath, "rb") as f:
            file_data = f.read()
        filename = filepath.replace("\\", "/").split("/")[-1]
        body.extend(b"--" + boundary.encode() + CRLF)
        line = 'Content-Disposition: form-data; name="' + key + '"; filename="' + filename + '"\r\n'
        body.extend(line.encode())
        body.extend(b"Content-Type: audio/wav\r\n\r\n")
        body.extend(file_data)
        body.extend(CRLF)
    body.extend(b"--" + boundary.encode() + b"--" + CRLF)
    req = urllib.request.Request(url, data=bytes(body),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

# ── Voice chat module ──
try:
    from voice_chat import (VoiceManager, setup_commands as setup_voice_commands, _read_group_state,
                             _init_debate_state, _init_council_state, _init_podcast_state, _init_ttrpg_state,
                             _fetch_podcast_articles, _release_prompt,
                             _write_group_state as _wgs_debate)

    voice_manager = VoiceManager()
    _VOICE_AVAILABLE = True
except ImportError:
    voice_manager = None
    _VOICE_AVAILABLE = False

# ── Memory store (optional) ──
try:
    from core.memory_store import get_store
    _MEMORY_AVAILABLE = True
    logger.info("Bot-Forge memory store available")
except ImportError:
    _MEMORY_AVAILABLE = False
    logger.info("Bot-Forge memory store not available")

try:
    from plugins.memory.holographic.store import MemoryStore
    holo_store = MemoryStore(hrr_dim=2048)
    _HOLO_AVAILABLE = True
    logging.info("Holographic memory available")
except Exception:
    holo_store = None
    _HOLO_AVAILABLE = False

# ── Auto category/tags for memory store ──
_AUTO_PROJECT_KW = frozenset([
    "project", "website", "build", "deploy",
    "lead gen", "client", "business", "pricing", "subscription",
    "revenue", "package", "service", "marketing", "ads", "seo",
    "campaign", "conversion", "funnel",
    "stripe", "payment", "lead", "outreach", "cold email", "sms",
    "appointment", "booking", "calendar",
])
_AUTO_USER_KW = frozenset([
    "love", "like", "hate", "prefer", "preference",
    "want", "need", "think", "feel", "hope",
    "birthday", "family", "friend", "work",
    "weight", "diet", "food", "eat", "drink", "cook", "recipe",
    "sleep", "anxiety",
    "wedding", "relationship", "date", "dating",
    "personality", "favourite", "favorite",
    "workout", "fitness", "gym", "calories", "protein",
    "breakfast", "lunch", "dinner", "meal", "snack",
    "health",
])
_AUTO_TOOL_KW = frozenset([
    "command", "config", "script", "tool", "api", "endpoint",
    "function", "cron", "cronjob",
    "memory", "database", "sqlite", "python", "node", "npm",
    "cli", "terminal", "setup", "install", "deploy", "git",
    "skill", "plugin", "provider", "model", "llm",
    "gateway", "discord bot",
    "voice", "tts", "audio", "speech",
    "code", "server", "port", "localhost", "docker",
    "error", "bug", "fix", "patch", "update", "upgrade",
])

import re as _re

def _sanitize_api_key(key: str) -> str:
    """Strip redaction markers like «redacted:sk-…»: from stored API keys."""
    if key and key.startswith("«"):
        colon_idx = key.find(":")
        if 0 < colon_idx < len(key) - 1:
            return key[colon_idx + 1:]
    return key


def _rough_token_count(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4


def _condense_context(messages: list[dict], max_tokens: int = 6000) -> list[dict]:
    """Condense message context when total exceeds max_tokens.
    Prunes oldest user/assistant turns, keeps most recent context."""
    total = sum(_rough_token_count(str(m.get("content", ""))) for m in messages)
    if total <= max_tokens:
        return messages

    # Separate system messages from conversation
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    conv_msgs = [m for m in messages if m.get("role") != "system"]

    # Keep dropping oldest conversation messages until under limit
    while conv_msgs and total > max_tokens:
        dropped = conv_msgs.pop(0)
        total -= _rough_token_count(str(dropped.get("content", "")))

    result = sys_msgs + conv_msgs
    logging.info("Condensed context: %d messages (%d est. tokens)", len(result), total)
    return result


def _auto_memory_retain(user_msg: str, author_name: str, author_id: int = 0) -> list[dict]:
    """Save any substantive information from a user message to memory.
    Categorizes by who said it. Saves if message contains meaningful content
    (not just greetings/one-word replies)."""
    msg_lower = user_msg.lower().strip()
    
    # Skip trivial messages
    trivial = {"hey", "hi", "hello", "sup", "lol", "lmao", "yeah", "nah", "ok", "k", "ty", "thanks",
               "good", "bad", "yes", "no", "maybe", "idk", "?"}
    if msg_lower in trivial or len(msg_lower.split()) < 3:
        return []

    # Check for known person aliases for cross-referencing
    known_people = {
        # Users can add their own mappings here
        # Format: "alias": "Full Name",
    }

    entries = []

    # 1. Save info ABOUT the speaker themselves (their preferences, details)
    entries.append({
        "content": f"[{author_name}]: {user_msg[:400]}",
        "category": "user_pref",
        "tags": f"{author_name.lower().replace(' ','')},discord,auto",
    })

    # 2. If they mention known people, cross-reference those mentions
    import re as _re
    sentences = _re.split(r'[.!?]+', user_msg)
    for sent in sentences:
        sent = sent.strip()
        if not sent or len(sent.split()) < 3:
            continue
        sent_lower = sent.lower()
        for alias, full_name in known_people.items():
            if alias in sent_lower and alias not in author_name.lower():
                entries.append({
                    "content": f"{full_name}: {sent[:300]} (from {author_name})",
                    "category": "user_pref",
                    "tags": f"{alias},{author_name.lower().replace(' ','')},discord,auto",
                })
                break

    return entries

_AUTO_STOPWORDS = frozenset([
    "the", "and", "for", "are", "but", "not", "you", "all", "can",
    "had", "her", "was", "one", "our", "out", "has", "have", "been",
    "some", "them", "than", "that", "this", "very", "were", "what",
    "when", "where", "which", "while", "who", "will", "with",
    "your", "from", "they", "also", "just", "more", "over", "such",
    "into", "about", "would", "could", "should", "their", "there",
    "these", "those", "because", "before", "after", "still", "even",
])

def auto_classify(content: str) -> tuple[str, str]:
    """Auto-detect category and generate tags from content. Same logic as holographic-cli.py."""
    lower = content.lower()
    proj = sum(1 for kw in _AUTO_PROJECT_KW if kw in lower)
    usr = sum(1 for kw in _AUTO_USER_KW if kw in lower)
    tool = sum(1 for kw in _AUTO_TOOL_KW if kw in lower)
    if "i " in lower or "my " in lower or "we " in lower:
        usr += 2
    if proj >= usr and proj >= tool and proj >= 1:
        cat = "project"
    elif usr >= proj and usr >= tool and usr >= 1:
        cat = "user_pref"
    elif tool >= 1:
        cat = "tool"
    else:
        cat = "general"
    tokens = set()
    for word in content.split():
        w = word.strip(".,;:!?\"'()[]{}#@<>$%^&*=+/\\|`~").lower()
        if len(w) < 3 or w in _AUTO_STOPWORDS or w == cat:
            continue
        tokens.add(w)
    tags = ",".join(sorted({cat} | {t for t in tokens if len(t) >= 4})[:8])
    return cat, tags

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

VISION_MODEL_TAGS = ("claude", "gemini", "gemma", "gpt-4", "gpt-5", "grok-4", "llama", "llava", "mistral", "o3", "o4", "vision", "vl")

EMBED_COLOR_COMPLETE = discord.Color.dark_green()
EMBED_COLOR_INCOMPLETE = discord.Color.orange()

STREAMING_INDICATOR = " ⚪"
EDIT_DELAY_SECONDS = 1

MAX_MESSAGE_NODES = 500

# ── Hermes tool pattern ──
HERMES_TOOL_RE = re.compile(r'<hermes\s+tool="([^"]+)"\s*>(.*?)</hermes>', re.DOTALL)

# ── TTS state ──
tts_channels: set[int] = set()

# ── TTS HTTP server (single shared instance) ──
TTS_SERVER_URL = "http://127.0.0.1:8769/tts"
TTS_SERVER_HEALTH = "http://127.0.0.1:8769/health"
_tts_available: bool = False

COMFY_PYTHON = r"F:\\ComfyUI_windows_portable\\python_embeded\\python.exe"
VOICE_REFS_DIR = os.path.join(os.path.dirname(__file__), "..", "voice-refs")
SFX_DIR = os.path.join(VOICE_REFS_DIR, "sfx")


def get_config(filename: str = "config.yaml") -> dict[str, Any]:
    with open(filename, encoding="utf-8") as file:
        return yaml.safe_load(file)


config = get_config()
curr_model = next(iter(config["models"]))
tts_personality = config.get("tts_personality", "")

def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_single_instance_lock() -> None:
    """Prevent two llmcord processes from running in the same bot directory."""
    lock_path = Path.cwd() / ".llmcord.lock"

    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing_pid = None
            try:
                existing_pid = int(lock_path.read_text(encoding="utf-8").strip())
            except Exception:
                pass

            if existing_pid and _pid_is_running(existing_pid):
                raise SystemExit(f"Another llmcord instance is already running in {Path.cwd()}")

            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue

        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            lock_file.write(str(os.getpid()))
            lock_file.flush()

        atexit.register(lambda: lock_path.unlink(missing_ok=True))
        return

    raise SystemExit(f"Unable to acquire llmcord lock in {Path.cwd()}")


acquire_single_instance_lock()

msg_nodes = {}
last_task_time = 0

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
# Server Members Intent must be enabled in Discord Developer Portal (already done).
activity = discord.CustomActivity(name=(config.get("status_message") or "github.com/jakobdylanc/llmcord")[:128])
discord_bot = commands.Bot(intents=intents, activity=activity, command_prefix=None)

httpx_client = httpx.AsyncClient()


@dataclass
class MsgNode:
    role: Literal["user", "assistant"] = "assistant"

    text: Optional[str] = None
    images: list[dict[str, Any]] = field(default_factory=list)

    has_bad_attachments: bool = False
    fetch_parent_failed: bool = False

    parent_msg: Optional[discord.Message] = None

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ═══════════════════════════════════════════════════════════
#  HERMES TOOL EXECUTION
# ═══════════════════════════════════════════════════════════

async def execute_hermes(hermes_args: list[str], input_text: str = "") -> str:
    """Run a Hermes CLI command and return stdout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *hermes_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(
            input=input_text.encode() if input_text else None,
            timeout=120,
        )
        if stdout:
            return stdout.decode("utf-8", errors="replace").strip()
        if stderr:
            return f"[Hermes stderr] {stderr.decode('utf-8', errors='replace').strip()[:500]}"
        return "[Hermes: no output]"
    except subprocess.TimeoutExpired:
        proc.kill()
        return "[Hermes: timed out after 120s]"
    except Exception as e:
        return f"[Hermes error: {e}]"


async def run_hermes_tool(tool_name: str, tool_input: str, channel_id: int) -> str:
    """Dispatch a Hermes tool and return the result text."""
    tl = tool_input.strip()

    if tool_name == "web_search":
        return await execute_hermes(["hermes", "web", "search", tl])
    elif tool_name == "memory_store":
        if _HOLO_AVAILABLE and holo_store is not None:
            try:
                cat, tags = auto_classify(tl)
                fid = holo_store.add_fact(content=tl, category=cat, tags=tags)
                return json.dumps({"fact_id": fid, "status": "stored", "category": cat})
            except Exception as e:
                return f"[Memory error: {e}]"
        return await execute_hermes(["python", r"C:\Users\josep\.hermes\scripts\holographic-cli.py", "store", tl])
    elif tool_name == "memory_recall":
        if _HOLO_AVAILABLE and holo_store is not None:
            try:
                results = holo_store.search_facts(tl, limit=5)
                if results:
                    lines = [f"#{r['fact_id']} [{r['category']}] {r['content'][:300]}" for r in results]
                    return "\n".join(lines)
                return "No results found."
            except Exception as e:
                return f"[Memory error: {e}]"
        return await execute_hermes(["python", r"C:\Users\josep\.hermes\scripts\holographic-cli.py", "recall", tl])
    elif tool_name == "run_code":
        # Write code to temp file and run
        tmp = os.path.join(tempfile.gettempdir(), f"hermes_run_{os.getpid()}.py")
        with open(tmp, "w") as f:
            f.write(tl)
        result = await execute_hermes(["python", tmp])
        os.unlink(tmp)
        return result
    elif tool_name == "cron_create":
        # Expect format: schedule | prompt | deliver_info
        # e.g. "0 9 * * * | daily summary | channel:1518853259740188672"
        parts = [p.strip() for p in tl.split("|", 2)]
        if len(parts) >= 2:
            schedule = parts[0]
            prompt = parts[1]
            deliver_target = parts[2] if len(parts) >= 3 else f"discord:{channel_id}"
            full_prompt = (
                f"{prompt}\n\n"
                f"[Delivery: send the result to Discord channel {channel_id} "
                f"via the llmcord bot that created this job.]"
            )
            return await execute_hermes([
                "hermes", "cron", "create",
                "--schedule", schedule,
                "--prompt", full_prompt,
                "--deliver", deliver_target,
            ])
        return "[Hermes: cron_create needs format: schedule | prompt | channel_id]"
    else:
        return f"[Hermes: unknown tool '{tool_name}']"


# ═══════════════════════════════════════════════════════════
#  TTS GENERATION
# ═══════════════════════════════════════════════════════════

async def _check_tts_server() -> bool:
    """Check if the PocketTTS server is running. Returns True if ready."""
    global _tts_available
    if _tts_available:
        return True
    try:
        import urllib.request
        req = urllib.request.Request(TTS_SERVER_HEALTH)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if data.get("status") == "ok" or resp.status == 200:
                _tts_available = True
                logging.info("PocketTTS server ready on %s", TTS_SERVER_URL)
                return True
            logging.warning("TTS server not ready: %s", data)
            return False
    except Exception as e:
        logging.warning("TTS server not available: %s — start with: "
                        "pocket-tts serve --port 8769", e)
        return False


async def generate_tts_audio(text: str) -> Optional[str]:
    """Generate TTS via PocketTTS HTTP server. Returns path to WAV or None."""
    global tts_personality
    if not tts_personality:
        logging.warning("TTS skipped: no tts_personality configured")
        return None

    if not await _check_tts_server():
        return await _generate_tts_subprocess(text)

    import uuid
    tag = str(uuid.uuid4())[:8]
    tmp_out = os.path.join(tempfile.gettempdir(), f"llmcord_tts_out_{tag}.wav")

    # Resolve voice ref from personality name
    voice_ref = tts_personality.lower()
    voice_ref_path = os.path.join(VOICE_REFS_DIR, f"{voice_ref}.wav")
    if not os.path.exists(voice_ref_path):
        voice_ref_path = os.path.join(VOICE_REFS_DIR, "default.wav")
        if not os.path.exists(voice_ref_path):
            logging.warning("No voice ref found, falling back to subprocess")
            return await _generate_tts_subprocess(text)

    for attempt in range(1, 4):
        try:
            wav_data = _pocket_tts_multipart(
                TTS_SERVER_URL,
                fields={"text": text},
                files={"voice_wav": voice_ref_path},
                timeout=600,
            )

            if len(wav_data) < 1000:
                logging.warning("PocketTTS returned small audio (%d bytes)", len(wav_data))
                return None

            os.makedirs(os.path.dirname(tmp_out) or ".", exist_ok=True)
            with open(tmp_out, "wb") as f:
                f.write(wav_data)

            logging.info("PocketTTS: %d bytes -> %s", len(wav_data), tmp_out)
            return tmp_out

        except Exception as e:
            logging.warning("PocketTTS attempt %d/3 failed: %s", attempt, e)
            if attempt < 3:
                await asyncio.sleep(1)
            else:
                logging.error("PocketTTS failed after 3 attempts")
                _tts_available = False
                return await _generate_tts_subprocess(text)

    return None

async def _generate_tts_subprocess(text: str) -> Optional[str]:
    """Fallback: one-shot subprocess TTS via PocketTTS CLI."""
    global tts_personality
    import uuid
    tag = str(uuid.uuid4())[:8]
    tmp_out = os.path.join(tempfile.gettempdir(), f"llmcord_tts_out_{tag}.wav")

    try:
        voice_ref = tts_personality.lower()
        voice_ref_path = os.path.join(VOICE_REFS_DIR, f"{voice_ref}.wav")
        if not os.path.exists(voice_ref_path):
            voice_ref = "default"
        voice_ref_path = os.path.join(VOICE_REFS_DIR, f"{voice_ref}.wav")

        proc = await asyncio.create_subprocess_exec(
            "pocket-tts",
            "generate",
            "--text", text,
            "--voice", voice_ref_path,
            "--output-path", tmp_out,
            "--device", "cpu",
            "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500] if stderr else "no stderr"
            logging.warning("PocketTTS subprocess failed (code %d): %s", proc.returncode, err)
            return None

        if os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 1000:
            logging.info("PocketTTS subprocess generated: %s", tmp_out)
            return tmp_out
        return None
    except Exception as e:
        logging.error("PocketTTS subprocess error: %s", e)
        return None

# ═══════════════════════════════════════════════════════════
#  VOICE LLM WRAPPER
# ═══════════════════════════════════════════════════════════

async def _generate_for_voice(messages_list: list[dict]) -> list[str]:
    """Simplified LLM call for voice chat. Reuses global config/model.
    Retries once if model returns empty content.
    Messages arrive newest-first — reverse to oldest-first (OpenAI API format).
    """
    global config, curr_model

    # Reverse to chronological order (oldest first) — same as _generate() does
    messages_list = list(reversed(messages_list))

    cfg = config
    provider_slash_model = curr_model
    try:
        provider, model = provider_slash_model.removesuffix(":vision").split("/", 1)
    except ValueError:
        return ["[Error: invalid model config]"]

    provider_cfg = cfg.get("providers", {}).get(provider, {})
    base_url = provider_cfg.get("base_url", "http://localhost:1234/v1")
    api_key = provider_cfg.get("api_key", "sk-no-...ired")
    api_key = _sanitize_api_key(api_key)

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    extra_headers = provider_cfg.get("extra_headers")
    extra_query = provider_cfg.get("extra_query")
    extra_body = provider_cfg.get("extra_body")

    for attempt in range(2):
        try:
            # Sanitize messages: replace non-ASCII chars to prevent encoding errors
            clean_msgs = []
            for m in messages_list:
                cm = dict(m)
                if isinstance(cm.get("content"), str):
                    cm["content"] = cm["content"].encode("utf-8", errors="replace").decode("utf-8")
                clean_msgs.append(cm)
            response = await client.chat.completions.create(
                model=model,
                messages=clean_msgs,
                stream=False,
                temperature=0.7,
                max_tokens=500,
                extra_headers=extra_headers,
                extra_query=extra_query,
                extra_body=extra_body,
            )
            text = response.choices[0].message.content or ""
            if text.strip():
                return [text]
            if attempt == 0:
                logging.warning("Empty LLM response on attempt %d, retrying...", attempt)
                # Add a nudge to the system prompt
                messages_list = [
                    m for m in messages_list if m.get("role") != "system"
                ]
                messages_list.append({
                    "role": "system",
                    "content": "You gave an empty response. Answer the user's question. If you don't have the information, say so clearly."
                })
                continue
            return [""]  # still empty after retry
        except Exception as e:
            logging.error("Voice LLM call failed: %s", e)
            if attempt == 0:
                continue
            return [f"[Error: {e}]"]


# ═══════════════════════════════════════════════════════════
#  VOICE DELIVERY — Discord voice message via REST API
# ═══════════════════════════════════════════════════════════

async def send_discord_voice_message(channel_id: int, ogg_path: str, bot_token: str) -> bool:
    """
    Send an OGG Opus file as a Discord voice message (inline waveform).
    Uses direct REST API with duration_secs and waveform metadata.
    """
    import json as _json, subprocess as _sp, base64 as _b64
    import numpy as _np

    try:
        # Get duration
        probe = _sp.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", ogg_path],
            capture_output=True, text=True, timeout=15
        )
        info = _json.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration", 1))
        sr = int(info.get("streams", [{}])[0].get("sample_rate", 48000))
    except Exception:
        duration = 1.0
        sr = 48000

    # Generate waveform
    waveform_b64 = ""
    try:
        wav_path = ogg_path + ".wav"
        _sp.run(["ffmpeg", "-y", "-i", ogg_path,
            "-ac", "1", "-ar", str(min(sr, 48000)), "-f", "s16le", "-c:a", "pcm_s16le",
            wav_path], capture_output=True, timeout=30)
        raw = _np.fromfile(wav_path, dtype=_np.int16)
        step = max(1, len(raw) // 256)
        wf = raw[::step]
        wf_norm = _np.interp(wf, [wf.min(), wf.max()], [0, 255]).astype(_np.uint8)
        waveform_b64 = _b64.b64encode(bytes(wf_norm.tolist())).decode()
        if os.path.exists(wav_path):
            os.unlink(wav_path)
    except Exception:
        pass

    # Send via REST API
    try:
        payload_json = _json.dumps({"flags": 8192, "attachments": [{"id": 0, "filename": "voice-message.ogg", "duration_secs": round(duration, 2), "waveform": waveform_b64}]})
        with open(ogg_path, "rb") as f:
            file_bytes = f.read()
        from io import BytesIO
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {bot_token}"},
                files={
                    "payload_json": (None, payload_json, "application/json"),
                    "files[0]": ("voice-message.ogg", BytesIO(file_bytes), "audio/ogg"),
                },
                timeout=30,
            )
            if resp.status_code == 200:
                logging.info("Voice message sent (msg_id=%s)", resp.json().get("id"))
                return True
            else:
                err = resp.text[:500]
                logging.warning("Voice message API error: %s", err)
                return False
    except Exception as e:
        logging.exception("Voice message send failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════════════

@discord_bot.tree.command(name="model", description="View or switch the current model")
async def model_command(interaction: discord.Interaction, model: str) -> None:
    global curr_model

    if model == curr_model:
        output = f"Current model: `{curr_model}`"
    else:
        if user_is_admin := interaction.user.id in config["permissions"]["users"]["admin_ids"]:
            curr_model = model
            output = f"Model switched to: `{model}`"
            logging.info(output)
        else:
            output = "You don't have permission to change the model."

    await interaction.response.send_message(output, ephemeral=(interaction.channel.type == discord.ChannelType.private))


@model_command.autocomplete("model")
async def model_autocomplete(interaction: discord.Interaction, curr_str: str) -> list[Choice[str]]:
    global config

    if curr_str == "":
        config = await asyncio.to_thread(get_config)

    choices = [Choice(name=f"◉ {curr_model} (current)", value=curr_model)] if curr_str.lower() in curr_model.lower() else []
    choices += [Choice(name=f"○ {model}", value=model) for model in config["models"] if model != curr_model and curr_str.lower() in model.lower()]

    return choices[:25]


@discord_bot.tree.command(name="voice", description="Toggle TTS for this channel (on/off)")
async def voice_command(interaction: discord.Interaction, mode: str) -> None:
    cid = interaction.channel_id
    # Defer immediately to extend the 3s window to 15min
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        # Interaction already expired — can't respond at all
        logging.warning("Voice command interaction expired before defer")
        return
    if mode.lower() in ("on", "tts"):
        tts_channels.add(cid)
        await interaction.followup.send(" TTS enabled for this channel", ephemeral=True)
        logging.info("TTS enabled for channel %s", cid)
    elif mode.lower() == "off":
        tts_channels.discard(cid)
        await interaction.followup.send(" TTS disabled for this channel", ephemeral=True)
        logging.info("TTS disabled for channel %s", cid)
    else:
        await interaction.followup.send("Usage: /voice on | /voice off", ephemeral=True)


# ═══════════════════════════════════════════════════════════
#  EVENT HANDLERS
# ═══════════════════════════════════════════════════════════

@discord_bot.event
async def on_ready() -> None:
    bot_tag = config.get("tts_personality", "Bot")
    print(f"✅ [{bot_tag}] Online and connected to Discord!")

    if client_id := config.get("client_id"):
        logging.info(f"\n\nBOT INVITE URL:\nhttps://discord.com/oauth2/authorize?client_id={client_id}&permissions=412317191168&scope=bot\n")

    # ── Setup voice chat ──
    if _VOICE_AVAILABLE and voice_manager is not None:
        voice_manager.set_callbacks(
            llm_generate=_generate_for_voice,
            tts_generate=generate_tts_audio,
        )
        voice_manager.set_bot_identity(
            bot_id=discord_bot.user.id,
            bot_name=config.get("tts_personality", "Bot"),
            tts_personality=tts_personality,
        )
        try:
            await discord_bot.tree.sync()
            setup_voice_commands(
                discord_bot,
                voice_manager,
                bot_id=discord_bot.user.id,
                bot_name=config.get("tts_personality", "Bot"),
                tts_personality=config.get("tts_personality", ""),
            )
            await discord_bot.tree.sync()
            logging.info("Voice chat module initialized")
        except Exception as e:
            logging.warning("Voice setup failed (OK if bot can't VC): %s", e)

    # ── Pre-warm TTS server check ──
    if tts_personality and not await _check_tts_server():
        logging.warning("TTS server not available (will try again on first TTS call)")


# ═══════════════════════════════════════════════════════════
#  VOICE STATE HANDLER — auto-cleanup on disconnect
# ═══════════════════════════════════════════════════════════


@discord_bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Detect bot disconnects from VC and clean up the session."""
    if member.id != discord_bot.user.id:
        return  # only care about the bot's own voice state

    if _VOICE_AVAILABLE and voice_manager is not None and member.guild:
        gid = member.guild.id
        session = voice_manager.get_session(gid)
        if session is None:
            return

        bot_left = (
            before.channel is not None and after.channel is None
        )
        bot_moved = (
            before.channel is not None and after.channel is not None
            and before.channel.id != after.channel.id
        )

        if bot_left:
            logging.info("Bot disconnected from VC in guild %d — cleaning up session", gid)
            await voice_manager.destroy_session(gid)
        elif bot_moved:
            logging.info(
                "Bot moved VC in guild %d: %s -> %s",
                gid,
                before.channel.name if before.channel else "?",
                after.channel.name if after.channel else "?",
            )
            # Recreate session with the new voice client
            # The existing .vc object is still valid after move_to(), so just
            # update the channel reference
            pass


# ═══════════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════

@discord_bot.event
async def on_message(new_msg: discord.Message) -> None:
    global config, curr_model, last_task_time, tts_personality

    if new_msg.author.bot and new_msg.author != discord_bot.user:
        # Allow messages from admin bot accounts for cross-bot communication
        is_admin = new_msg.author.id in config["permissions"]["users"]["admin_ids"]
        # Allow bot messages during group VC conversation
        is_group_bot = False
        if _VOICE_AVAILABLE and voice_manager is not None and new_msg.guild:
            session = voice_manager.get_session(new_msg.guild.id)
            if session and session.group_mode and session.is_bot_message(new_msg.author.id):
                is_group_bot = True
        if not is_admin and not is_group_bot:
            return

    # Skip bot's own messages (slash command responses, echo from TextDisplay, etc.)
    if new_msg.author == discord_bot.user:
        return
    
    # ═══════════════════════════════════════════════════════════
    #  ERROR LOOP PREVENTION — skip messages containing HTTP errors
    # ═══════════════════════════════════════════════════════════
    error_patterns = ["HTTP 40", "HTTP 50", "HTML error page", "title not found",
                      "Internal Server Error", "Forbidden", "Not Found"]
    for pattern in error_patterns:
        if pattern.lower() in new_msg.content.lower():
            logging.info("Skipping message with error pattern: %s", pattern)
            return

    # ── Check for group-mode bot messages (bypass mention requirement) ──
    is_group_vc_msg = False
    if _VOICE_AVAILABLE and voice_manager is not None and new_msg.guild:
        try:
            _gsession = voice_manager.get_session(new_msg.guild.id)
            if _gsession and _gsession.group_mode and _gsession.is_bot_message(new_msg.author.id) and _gsession.is_text_chat_of_this_vc(new_msg.channel):
                is_group_vc_msg = True
        except Exception:
            pass

    check_self_mention = discord_bot.user.mentioned_in(new_msg) or is_group_vc_msg
    
    # ═══════════════════════════════════════════════════════════
    #  LOOP PREVENTION — skip messages with empty content
    #  The harness bot sends messages using TextDisplay components which
    #  have empty content field. Skip these to prevent bot loop.
    # ═══════════════════════════════════════════════════════════
    if not new_msg.content.strip():
        logging.info("Skipping message with empty content (TextDisplay)")
        return

    if new_msg.channel.type == discord.ChannelType.private:
        if not config.get("allow_dms", True):
            return
    elif not check_self_mention:
        return

    # ── Fetch channel history for context when mentioned ──
    channel_context = []
    if check_self_mention:
        try:
            async for msg in new_msg.channel.history(limit=15, before=new_msg, oldest_first=False):
                if msg.author != discord_bot.user and msg.id != new_msg.id:
                    channel_context.append(f"[{msg.author.display_name}]: {msg.content}")
        except Exception:
            pass

    channel_context_str = "\n".join(reversed(channel_context[-20:])) if channel_context else ""

    # Permission checks
    def _is_allowed(uid: int) -> bool:
        perm = config["permissions"]
        if perm["users"]["blocked_ids"] and uid in perm["users"]["blocked_ids"]:
            return False
        if perm["users"]["allowed_ids"]:
            return uid in perm["users"]["allowed_ids"]
        return True  # empty allowed_ids = all allowed

    allowed = _is_allowed(new_msg.author.id)
    is_admin = new_msg.author.id in config["permissions"]["users"]["admin_ids"]
    if not allowed:
        logging.info("Blocked user %s", new_msg.author.id)
        return

    # Hot-reload config
    config = await asyncio.to_thread(get_config)
    tts_personality = config.get("tts_personality", "")

    bot_tag = config.get("tts_personality", "?")
    print(f"\n📩 [{bot_tag}] Message from {new_msg.author.display_name} ({new_msg.author.id}): {new_msg.content[:120]}")

    # Resolve provider/model
    provider_slash_model = curr_model
    provider, model = provider_slash_model.removesuffix(":vision").split("/", 1)

    provider_config = config["providers"][provider]

    base_url = provider_config["base_url"]
    api_key = provider_config.get("api_key", "sk-no-...ired")
    api_key = _sanitize_api_key(api_key)
    openai_client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    model_parameters = config["models"].get(provider_slash_model, None)

    extra_headers = provider_config.get("extra_headers")
    extra_query = provider_config.get("extra_query")
    extra_body = (provider_config.get("extra_body") or {}) | (model_parameters or {}) or None

    accept_images = any(x in provider_slash_model.lower() for x in VISION_MODEL_TAGS)

    max_text = config.get("max_text", 100000)
    max_images = config.get("max_images", 5) if accept_images else 0
    max_messages = config.get("max_messages", 25)

    # Build message chain and set user warnings
    messages = []
    user_warnings = set()
    curr_msg = new_msg

    while curr_msg is not None and len(messages) < max_messages:
        curr_node = msg_nodes.setdefault(curr_msg.id, MsgNode())

        async with curr_node.lock:
            if curr_node.text is None:
                cleaned_content = curr_msg.content.removeprefix(discord_bot.user.mention).lstrip()

                good_attachments = [att for att in curr_msg.attachments if att.content_type and any(att.content_type.startswith(x) for x in ("text", "image"))]

                attachment_responses = await asyncio.gather(*[httpx_client.get(att.url) for att in good_attachments])

                curr_node.role = "assistant" if curr_msg.author == discord_bot.user else "user"

                curr_node.text = "\n".join(
                    ([cleaned_content] if cleaned_content else [])
                    + ["\n".join(filter(None, (embed.title, embed.description, embed.footer.text))) for embed in curr_msg.embeds]
                    + [component.content for component in curr_msg.components if component.type == discord.ComponentType.text_display]
                    + [resp.text for att, resp in zip(good_attachments, attachment_responses) if att.content_type.startswith("text")]
                )

                curr_node.images = [
                    dict(type="image_url", image_url=dict(url=f"data:{att.content_type};base64,{b64encode(resp.content).decode('utf-8')}"))
                    for att, resp in zip(good_attachments, attachment_responses)
                    if att.content_type.startswith("image")
                ]

                if curr_node.role == "user" and (curr_node.text or curr_node.images):
                    curr_node.text = f"<@{curr_msg.author.id}>: {curr_node.text}"

                curr_node.has_bad_attachments = len(curr_msg.attachments) > len(good_attachments)

                try:
                    if (
                        curr_msg.reference is None
                        and discord_bot.user.mention not in curr_msg.content
                        and (prev_msg_in_channel := ([m async for m in curr_msg.channel.history(before=curr_msg, limit=1)] or [None])[0])
                        and prev_msg_in_channel.type in (discord.MessageType.default, discord.MessageType.reply)
                        and prev_msg_in_channel.author == (discord_bot.user if curr_msg.channel.type == discord.ChannelType.private else curr_msg.author)
                    ):
                        curr_node.parent_msg = prev_msg_in_channel
                    else:
                        is_public_thread = curr_msg.channel.type == discord.ChannelType.public_thread
                        parent_is_thread_start = is_public_thread and curr_msg.reference is None and curr_msg.channel.parent.type == discord.ChannelType.text

                        if parent_msg_id := curr_msg.channel.id if parent_is_thread_start else getattr(curr_msg.reference, "message_id", None):
                            if parent_is_thread_start:
                                curr_node.parent_msg = curr_msg.channel.starter_message or await curr_msg.channel.parent.fetch_message(parent_msg_id)
                            else:
                                curr_node.parent_msg = curr_msg.reference.cached_message or await curr_msg.channel.fetch_message(parent_msg_id)

                except (discord.NotFound, discord.HTTPException):
                    logging.exception("Error fetching next message in the chain")
                    curr_node.fetch_parent_failed = True

                # ponytail: chain @mentions to last bot response for context continuity
                if curr_node.parent_msg is None and curr_msg == new_msg:
                    async for prev in curr_msg.channel.history(before=curr_msg, limit=20):
                        if prev.author == discord_bot.user:
                            curr_node.parent_msg = prev
                            break

            if curr_node.images[:max_images]:
                content = [dict(type="text", text=curr_node.text[:max_text])] + curr_node.images[:max_images]
            else:
                content = curr_node.text[:max_text]

            if content != "":
                messages.append(dict(content=content, role=curr_node.role))

            if len(curr_node.text) > max_text:
                user_warnings.add(f"⚠️ Max {max_text:,} characters per message")
            if len(curr_node.images) > max_images:
                user_warnings.add(f"⚠️ Max {max_images} image{'' if max_images == 1 else 's'} per message" if max_images > 0 else "⚠️ Can't see images")
            if curr_node.has_bad_attachments:
                user_warnings.add("⚠️ Unsupported attachments")
            if curr_node.fetch_parent_failed or (curr_node.parent_msg is not None and len(messages) == max_messages):
                user_warnings.add(f"⚠️ Only using last {len(messages)} message{'' if len(messages) == 1 else 's'}")

            curr_msg = curr_node.parent_msg

    logging.info(f"Message received (user ID: {new_msg.author.id}, attachments: {len(new_msg.attachments)}, conversation length: {len(messages)}):\n{new_msg.content}")

    # ── Holographic memory recall for user context ──
    holo_context = ""
    if _HOLO_AVAILABLE and holo_store is not None:
        try:
            # Build composite query from conversation context + current message
            # This lets memory retrieval know WHAT you're talking about, not just
            # the literal words in the latest message.
            recall_sources = []
            
            # 1. Current user message (cleaned up)
            curr_clean = re.sub(r"<@!?\d+>\s*", "", new_msg.content).strip()
            if curr_clean:
                recall_sources.append(curr_clean)
            
            # 2. Last 3 user messages from conversation history (gives context like
            #    "what about that project?" → prior msg mentions the project name)
            user_ctx_count = 0
            for m in reversed(messages):
                if m.get("role") == "user" and user_ctx_count < 3:
                    ctx = m.get("content", "")
                    if isinstance(ctx, list):
                        ctx = " ".join(c.get("text","") for c in ctx if isinstance(c,dict))
                    ctx = str(ctx)[:300]
                    if ctx.strip():
                        recall_sources.insert(0, ctx)
                        user_ctx_count += 1
            
            # 3. Channel topic/name for ambient context
            if hasattr(new_msg, "channel") and new_msg.channel:
                try:
                    ch_name = getattr(new_msg.channel, "name", "")
                    ch_topic = getattr(new_msg.channel, "topic", "")
                    if ch_name:
                        recall_sources.append(f"channel: {ch_name}")
                    if ch_topic:
                        recall_sources.append(ch_topic[:200])
                except Exception:
                    pass
            
            # Combine into one query (most weight on latest message + context)
            recall_query = " | ".join(s[:200] for s in recall_sources if s)[:800]
            
            # 4. Also include the prior bot response for follow-up detection
            #    e.g. user replies "yes" after bot mentions a project → query becomes "yes | project"
            for m in reversed(messages):
                if m.get("role") == "assistant":
                    ast_text = str(m.get("content",""))[:200]
                    if ast_text.strip():
                        recall_query += " | " + ast_text
                    break
            
            if recall_query:
                # Dynamic trust: stricter for vague queries, looser for detailed ones
                word_count = len(recall_query.split())
                min_trust = 0.2 if word_count >= 8 else 0.35  # detailed query = wider net
                results = holo_store.search_facts(recall_query, limit=7, min_trust=min_trust)
                if results:
                    # Deduplicate: same content from different result entries
                    seen_contents = set()
                    unique_lines = []
                    for r in results:
                        content = r.get("content", "")[:400]
                        # Skip near-duplicate by checking first 80 chars
                        dedup_key = content[:80].lower()
                        if dedup_key in seen_contents:
                            continue
                        seen_contents.add(dedup_key)
                        cat = r.get("category", "?")
                        trust = r.get("trust_score", 0)
                        unique_lines.append(f"  {len(unique_lines)+1}. [{cat} ⭐{trust:.1f}] {content}")
                    if unique_lines:
                        holo_context = "📚 [User memory]\n" + "\n".join(unique_lines[:6])
        except Exception as e:
            logging.warning("Holo recall failed: %s", e)

    # ── Pipeline log: memory search result ──
    if _HOLO_AVAILABLE and holo_store is not None:
        if holo_context:
            print(f"🔍 [{bot_tag}] Holographic memory: ✓ injected")
        else:
            print(f"🔍 [{bot_tag}] Holographic memory: no results")
    else:
        print(f"🔍 [{bot_tag}] Holographic memory: ✗ not available")

    # ── Holographic keyword-triggered commands ──
    holo_cmd_result = ""
    if _HOLO_AVAILABLE and holo_store is not None:
        try:
            msg_lower = new_msg.content.lower()

            if "hstats" in msg_lower or "holo stats" in msg_lower or "memory stats" in msg_lower:
                total = holo_store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                cats = holo_store._conn.execute(
                    "SELECT category, COUNT(*) as c FROM facts GROUP BY category ORDER BY c DESC"
                ).fetchall()
                breakdown = "\n".join(f"  {r['category']}: {r['c']}" for r in cats)
                holo_cmd_result = f"📊 **Holographic Memory Stats**\nTotal: {total} facts\n\n{breakdown}"

            elif "recall" in msg_lower or "search memory" in msg_lower:
                recall_cmds = ["recall ", "search memory "]
                query = ""
                for cmd in recall_cmds:
                    if cmd in msg_lower:
                        query = new_msg.content.lower().split(cmd, 1)[-1].strip()
                        break
                if query:
                    results = holo_store.search_facts(query, limit=5, min_trust=0.3)
                    if results:
                        lines = []
                        for r in results:
                            c = r.get("content", "")[:300]
                            tid = r.get("fact_id")
                            trust = r.get("trust_score", 0)
                            lines.append(f"  `[{tid}]` ⭐{trust:.1f} {c}")
                        holo_cmd_result = "🔍 **Memory Recall**\n" + "\n".join(lines)
                    else:
                        holo_cmd_result = "🔍 No results found."

            elif "remember " in msg_lower or "save memory " in msg_lower or "store " in msg_lower:
                remember_cmds = ["remember ", "save memory ", "store "]
                content = ""
                for cmd in remember_cmds:
                    if cmd in msg_lower:
                        content = new_msg.content.split(cmd, 1)[-1].strip()
                        break
                if content:
                    category, tags = auto_classify(content)
                    fid = holo_store.add_fact(content=content, category=category, tags=tags)
                    holo_cmd_result = f"✅ Saved as fact `#{fid}` (category: {category})"

            elif "forget " in msg_lower or "delete memory " in msg_lower or "remove fact " in msg_lower:
                forget_cmds = ["forget ", "delete memory ", "remove fact "]
                fid_str = ""
                for cmd in forget_cmds:
                    if cmd in msg_lower:
                        fid_str = new_msg.content.split(cmd, 1)[-1].strip()
                        break
                try:
                    fid = int(fid_str.split()[0])
                    ok = holo_store.remove_fact(fid)
                    holo_cmd_result = f"🗑️ Fact `#{fid}` removed: {ok}" if ok else f"❌ Fact `#{fid}` not found"
                except (ValueError, IndexError):
                    holo_cmd_result = "❌ Usage: `forget <fact_id>`"

            elif "feedback " in msg_lower or "rate fact " in msg_lower:
                feedback_cmds = ["feedback ", "rate fact "]
                rest = ""
                for cmd in feedback_cmds:
                    if cmd in msg_lower:
                        rest = new_msg.content.split(cmd, 1)[-1].strip()
                        break
                parts = rest.split()
                if len(parts) >= 2:
                    try:
                        fid = int(parts[0])
                        helpful = parts[1].lower().startswith(("y", "h", "1", "t"))
                        result = holo_store.record_feedback(fid, helpful)
                        holo_cmd_result = (
                            f"⭐ Fact `#{fid}` trust: {result['old_trust']:.2f} → {result['new_trust']:.2f}"
                        )
                    except KeyError:
                        holo_cmd_result = f"❌ Fact `#{parts[0]}` not found"
                    except (ValueError, IndexError):
                        holo_cmd_result = "❌ Usage: `feedback <fact_id> yes/no`"
        except Exception as e:
            logging.warning("Holo command failed: %s", e)

    # Obsidian vault keyword search (ponytail: parallel context source, like voice server)
    vault_path = Path("F:/aaaaaVIBECODING/z-Obby-Memory")
    vault_hits = 0
    if vault_path.exists():
        keywords = recall_query.split()
        obsidian_hits = []
        try:
            for fpath in sorted(vault_path.rglob("*.md"))[:200]:
                try:
                    text = fpath.read_text("utf-8", errors="ignore")
                    if any(kw.lower() in text.lower() for kw in keywords):
                        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
                        obsidian_hits.append(f"  [{fpath.stem}] {' '.join(lines)[:300]}")
                        if len(obsidian_hits) >= 3:
                            break
                except:
                    continue
        except:
            pass
        if obsidian_hits:
            vault_context = "[Obsidian Knowledge]\n" + "\n\n".join(obsidian_hits)
            if holo_context:
                holo_context += "\n\n" + vault_context
            else:
                holo_context = vault_context

    # ── Pipeline log: vault search ──
    if vault_hits:
        print(f"📚 [{bot_tag}] Obsidian vault: ✓ {vault_hits} note(s) matched")

    if system_prompt := config.get("system_prompt"):
        now = datetime.now().astimezone()
        system_prompt = system_prompt.replace("{date}", now.strftime("%B %d %Y")).replace("{time}", now.strftime("%H:%M:%S %Z%z")).strip()

        # Inject member directory so the bot knows who everyone is
        if new_msg.guild and hasattr(new_msg.guild, "members"):
            try:
                members = [m for m in new_msg.guild.members if not m.bot]
                if members:
                    member_list = "\n".join(
                        f"  - {m.display_name} ({m.name}) <@{m.id}>"
                        for m in sorted(members, key=lambda x: x.display_name.lower())
                    )
                    system_prompt += (
                        f"\n\n[[SERVER MEMBERS — {new_msg.guild.name}]]\n"
                        f"There are {len(members)} human members in this server:\n"
                        f"{member_list}\n"
                        f"You can reference any member using their <@ID> mention format."
                    )
            except Exception as e:
                logging.warning("Failed to build member directory: %s", e)

        # Inject channel history context
        if channel_context_str:
            system_prompt += (
                f"\n\n[[RECENT CHANNEL HISTORY — last 20 messages before you were mentioned]]\n"
                f"{channel_context_str}\n"
            )

        # Reassemble into single system prompt (voice server pattern: identity + rules + context)
        if personality := config.get("personality_prompt"):
            system_prompt = personality + "\n\n" + system_prompt

        # Append data sections to system prompt like voice server's [Memory Context]
        data_sections = []
        if holo_context:
            data_sections.append("[Memory Context]\n" + holo_context.strip())
        if holo_cmd_result:
            data_sections.append("[Memory Data]\n" + holo_cmd_result.strip())
        if data_sections:
            system_prompt += "\n\n" + "\n\n".join(data_sections)

        messages.append(dict(role="system", content=system_prompt))

    # ═══════════════════════════════════════════════════════
    #  Generate & send response(s)
    # ═══════════════════════════════════════════════════════

    async def reply_helper(**reply_kwargs) -> discord.Message:
        reply_target = new_msg if not response_msgs else response_msgs[-1]
        response_msg = await reply_target.reply(**reply_kwargs)
        response_msgs.append(response_msg)
        msg_nodes[response_msg.id] = MsgNode(parent_msg=new_msg)
        await msg_nodes[response_msg.id].lock.acquire()
        return response_msg

    def _format_native_input(msgs: list[dict]) -> str:
        """Convert OpenAI-style messages array to a flat prompt string for LM Studio native API."""
        parts = []
        for m in reversed(msgs):
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                # Handle multimodal content (images, text)
                text_parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text_parts.append(c.get("text", ""))
                content = " ".join(text_parts)
            if content:
                if role == "system":
                    parts.insert(0, f"[System: {content}]")
                elif role == "assistant":
                    parts.insert(0, content)
                else:
                    parts.insert(0, content)
        return "\n".join(parts)

    async def _generate(messages_list: list[dict], collect_only: bool = False) -> list[str]:
        """Run a single generation pass, return split response_contents.
        When collect_only=True, doesn't send messages to Discord (used for TTS-first delivery)."""
        global last_task_time
        contents = []
        curr_content = finish_reason = None

        use_plain = config.get("use_plain_responses", False)
        max_len = 4000 if use_plain else (4096 - len(STREAMING_INDICATOR))
        if not use_plain and not collect_only:
            embed = discord.Embed.from_dict(dict(fields=[dict(name=w, value="", inline=False) for w in sorted(user_warnings)]))

        async def _stream_edit(parts, latest, is_new):
            """Edit the latest Discord message with accumulated content."""
            global last_task_time
            if use_plain or collect_only:
                return
            td = datetime.now().timestamp() - last_task_time
            ready = td >= EDIT_DELAY_SECONDS
            final_flag = is_new and not latest  # approx final
            if is_new or ready or final_flag:
                embed.description = parts[-1] if final_flag else (parts[-1] + STREAMING_INDICATOR)
                embed.color = EMBED_COLOR_COMPLETE if final_flag else EMBED_COLOR_INCOMPLETE
                if is_new:
                    await reply_helper(embed=embed, silent=True)
                else:
                    await asyncio.sleep(EDIT_DELAY_SECONDS - td)
                    await response_msgs[-1].edit(embed=embed)
                last_task_time = datetime.now().timestamp()

        # Check if we should use LM Studio native SSE API (supports tool call events)
        use_lmstudio_native = provider_config.get("lmstudio_native_api", False)

        if use_lmstudio_native:
            # ── LM Studio native SSE streaming (supports tool_call events) ──
            native_url = base_url.replace("/v1", "") + "/api/v1/chat"
            # Build integrations list from extra_body
            integrations_list = []
            if extra_body and isinstance(extra_body, dict):
                integrations_list = extra_body.get("integrations", [])
            payload = {
                "model": model,
                "input": _format_native_input(messages_list[::-1]),
                "stream": True,
                "integrations": integrations_list,
            }
            if extra_body:
                # Merge but don't double-pass integrations
                for k, v in extra_body.items():
                    if k != "integrations":
                        payload[k] = v

            headers = {"Content-Type": "application/json"}
            if api_key and api_key != "sk-no-...ired":
                headers["Authorization"] = f"Bearer {api_key}"

            tool_msg = None
            try:
                async with new_msg.channel.typing():
                    async with httpx.AsyncClient(timeout=300) as client:
                        async with client.stream("POST", native_url, json=payload, headers=headers) as resp:
                            event_type = None
                            data_lines = []
                            async for line in resp.aiter_lines():
                                line = line.strip()
                                if line.startswith("event: "):
                                    event_type = line[7:]
                                    data_lines = []
                                elif line.startswith("data: "):
                                    data_lines.append(line[6:])
                                elif line == "" and event_type and data_lines:
                                    try:
                                        data = json.loads("".join(data_lines))
                                    except json.JSONDecodeError:
                                        event_type = None
                                        data_lines = []
                                        continue

                                    if event_type == "tool_call.start":
                                        tool_name = data.get("tool", "?")
                                        tool_msg = await new_msg.reply(f"🔧 Calling `{tool_name}`...", silent=True)
                                    elif event_type == "tool_call.success":
                                        if tool_msg:
                                            await tool_msg.edit(content=f"✅ `{data.get('tool', '?')}` done")
                                            tool_msg = None
                                    elif event_type == "tool_call.failure":
                                        if tool_msg:
                                            await tool_msg.edit(content=f"❌ `{data.get('tool', '?')}` failed: {data.get('reason', '?')}")
                                            tool_msg = None
                                    elif event_type == "message.delta":
                                        new_content = data.get("content", "") or ""
                                        if new_content:
                                            if start_new := (contents == [] or len(contents[-1] + new_content) > max_len):
                                                contents.append("")
                                            contents[-1] += new_content
                                            await _stream_edit(contents, new_content, start_new)
                                    elif event_type == "chat.end":
                                        result = data.get("result", {})
                                        for out in result.get("output", []):
                                            if out.get("type") == "message":
                                                msg_text = out.get("content", "") or ""
                                                if msg_text:
                                                    if contents == [] or (contents and contents[-1]):
                                                        contents.append("")
                                                    contents[-1] += msg_text
                                                    await _stream_edit(contents, msg_text, True)
                                        break
                                    elif event_type == "error":
                                        logging.warning("LM Studio SSE error: %s", data)
                                        if tool_msg:
                                            await tool_msg.edit(content=f"❌ Error: {data.get('error', {}).get('message', '?')}")
                                            tool_msg = None

                                    event_type = None
                                    data_lines = []
            except Exception as e:
                logging.exception("Error in LM Studio native streaming: %s", e)
                return contents or ["[Error during streaming]"]
        else:
            # ── Standard OpenAI streaming (no tool call events) ──
            openai_kwargs = dict(
                model=model, messages=messages_list[::-1], stream=True,
                temperature=0.7,  # ponytail: voice server uses 0.7, default 1.0 hallucinates
                extra_headers=extra_headers, extra_query=extra_query, extra_body=extra_body,
            )

            try:
                async with new_msg.channel.typing():
                    async for chunk in await openai_client.chat.completions.create(**openai_kwargs):
                        if finish_reason is not None:
                            break
                        choice = chunk.choices[0] if chunk.choices else None
                        if not choice:
                            continue
                        finish_reason = choice.finish_reason
                        prev = curr_content or ""
                        curr_content = choice.delta.content or ""
                        new_content = prev if finish_reason is None else (prev + curr_content)

                        if contents == [] and new_content == "":
                            continue

                        if start_new := (contents == [] or len(contents[-1] + new_content) > max_len):
                            contents.append("")
                        contents[-1] += new_content

                        if not use_plain:
                            td = datetime.now().timestamp() - last_task_time
                            ready = td >= EDIT_DELAY_SECONDS
                            split = finish_reason is None and len(contents[-1] + curr_content) > max_len
                            final = finish_reason is not None or split
                            good = finish_reason is not None and finish_reason.lower() in ("stop", "end_turn")

                            if start_new or ready or final:
                                embed.description = contents[-1] if final else (contents[-1] + STREAMING_INDICATOR)
                                embed.color = EMBED_COLOR_COMPLETE if split or good else EMBED_COLOR_INCOMPLETE
                                if start_new:
                                    await reply_helper(embed=embed, silent=True)
                                else:
                                    await asyncio.sleep(EDIT_DELAY_SECONDS - td)
                                    await response_msgs[-1].edit(embed=embed)
                                last_task_time = datetime.now().timestamp()

                if use_plain and not collect_only:
                    for content in contents:
                        await reply_helper(view=LayoutView().add_item(TextDisplay(content=content)))

            except Exception:
                logging.exception("Error while generating response")
                return contents

        for rm in response_msgs:
            msg_nodes[rm.id].text = "".join(contents)
            msg_nodes[rm.id].lock.release()

        return contents

    # ── First generation pass ──
    response_msgs: list[discord.Message] = []
    tts_enabled = new_msg.channel.id in tts_channels
    use_plain = config.get("use_plain_responses", False)

    # ── VC routing: if bot is in a voice channel and this is that VC's side-chat ──
    voice_session: Optional[Any] = None
    route_to_vc = False
    if _VOICE_AVAILABLE and voice_manager is not None and new_msg.guild:
        try:
            session = voice_manager.get_session(new_msg.guild.id)
            if session and session.connected and session.is_text_chat_of_this_vc(new_msg.channel):
                voice_session = session
                route_to_vc = True
        except Exception:
            pass

    if route_to_vc:
        # If the bot is in group mode and gets a bot message from Discord, just
        # mark the VAD cooldown — actual bot-to-bot handling is via internal poll loop
        if voice_session and voice_session.group_mode and new_msg.author.bot and voice_session.is_bot_message(new_msg.author.id):
            voice_session.mark_bot_text_received()
            return  # poll loop handles the response

        # Human message in VC side-chat: respond via TTS in VC
        print(f"🎙️ [{bot_tag}] Routing to VC TTS (in {voice_session.vc.channel.name})")

        # ── Check if mode is waiting for a human prompt (debate/council/ttrpg/group) ──
        p_state = _read_group_state(new_msg.guild.id)
        if p_state.get("waiting_for_prompt"):
            prompt_text = new_msg.content
            released = _release_prompt(new_msg.guild.id, prompt_text, new_msg.author.display_name)
            if released:
                print(f"📻 [{bot_tag}] Prompt released by {new_msg.author.display_name}: {prompt_text[:80]}")
                msg = f"🎙️ **{prompt_text}** — starting now!"
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.green()))
                return

        # ── Text commands for group management ──
        content_lower = new_msg.content.lower()
        cmd_start = "/group start" in content_lower or "!group start" in content_lower
        cmd_stop = "/group stop" in content_lower or "!group stop" in content_lower
        cmd_debate = "/debate " in content_lower or "!debate " in content_lower

        if cmd_debate:
            # Extract topic: everything after !debate or /debate
            topic = ""
            for prefix in ["/debate ", "!debate "]:
                idx = content_lower.find(prefix)
                if idx >= 0:
                    topic = new_msg.content[idx + len(prefix):].strip()
                    break
            if not topic:
                msg = "Usage: `!debate <topic>` — e.g. `!debate Should pineapple be on pizza?`"
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.orange()))
                return

            # Gather bots in VC
            vc_channel = voice_session.vc.channel
            if vc_channel:
                bot_members = [m for m in vc_channel.members if m.bot]
                bot_ids = [m.id for m in bot_members]
                if len(bot_ids) >= 2:
                    # Last bot in VC is referee
                    ref_id = bot_ids[-1]
                    debate_ids = [bid for bid in bot_ids if bid != ref_id]
                    turn_order = debate_ids + [ref_id]

                    _init_debate_state(new_msg.guild.id, turn_order, ref_id, topic, max_rounds=10)

                    voice_manager.set_group_config(new_msg.guild.id, turn_order, active=True)
                    other_ids = [bid for bid in turn_order if bid != discord_bot.user.id]
                    voice_session.enable_group_mode(
                        bot_id=discord_bot.user.id,
                        bot_name=config.get("tts_personality", "Bot"),
                        tts_personality=tts_personality,
                        other_bot_ids=other_ids,
                    )
                    if not voice_session.listening:
                        await voice_session.start_listening()

                    order_parts = []
                    for bid in turn_order:
                        if bid == ref_id:
                            order_parts.append(f"<@{bid}> *(referee)*")
                        else:
                            order_parts.append(f"<@{bid}>")
                    turn_str = " → ".join(order_parts)
                    msg = (
                        f"🎙️ **DEBATE STARTED!**\n"
                        f"📋 Topic: **{topic}**\n"
                        f"🔄 10 rounds\n"
                        f"👥 {turn_str}\n"
                        f"🏆 Referee scoring silently..."
                    )
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.dark_purple()))
                    print(f"🎙️ [{bot_tag}] Debate started by text command: {topic}")
                else:
                    msg = "Need at least 2 bots in VC for a debate (one referee + debaters)"
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
            else:
                msg = "No VC channel found"
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
            return  # don't generate LLM response

        cmd_council = "/council " in content_lower or "!council " in content_lower

        if cmd_council:
            topic = ""
            for prefix in ["/council ", "!council "]:
                idx = content_lower.find(prefix)
                if idx >= 0:
                    topic = new_msg.content[idx + len(prefix):].strip()
                    break
            if not topic:
                msg = "Usage: `!council <topic>` — e.g. `!council Should we switch to a 4-day work week?`"
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.orange()))
                return

            vc_channel = voice_session.vc.channel
            if vc_channel:
                bot_members = [m for m in vc_channel.members if m.bot]
                bot_ids = [m.id for m in bot_members]
                if len(bot_ids) >= 2:
                    ref_id = bot_ids[-1]
                    council_ids = [bid for bid in bot_ids if bid != ref_id]
                    turn_order = council_ids + [ref_id]

                    _init_council_state(new_msg.guild.id, turn_order, ref_id, topic, max_rounds=10)

                    voice_manager.set_group_config(new_msg.guild.id, turn_order, active=True)
                    other_ids = [bid for bid in turn_order if bid != discord_bot.user.id]
                    voice_session.enable_group_mode(
                        bot_id=discord_bot.user.id,
                        bot_name=config.get("tts_personality", "Bot"),
                        tts_personality=tts_personality,
                        other_bot_ids=other_ids,
                    )
                    if not voice_session.listening:
                        await voice_session.start_listening()

                    order_parts = [f"<@{bid}> {'*(referee)*' if bid == ref_id else ''}" for bid in turn_order]
                    msg = (
                        f"🏛️ **COUNCIL CONVENED!**\n📋 Topic: **{topic}**\n"
                        f"🔄 Up to 10 rounds • {' → '.join(order_parts)}\n"
                        f"🔍 Evidence-gated • Blind review • Unanimous verdict"
                    )
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.dark_blue()))
                    print(f"🏛️ [{bot_tag}] Council started by text command: {topic}")
                else:
                    msg = "Need at least 2 bots in VC for a council"
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
            else:
                msg = "No VC channel found"
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
            return

        cmd_podcast = "/podcast" in content_lower or "!podcast" in content_lower

        if cmd_podcast:
            # Check if it's a custom topic (!podcast <topic>) or just a start (!podcast)
            topic = ""
            for prefix in ["/podcast ", "!podcast "]:
                idx = content_lower.find(prefix)
                if idx >= 0:
                    topic = new_msg.content[idx + len(prefix):].strip()
                    break

            if topic:
                # Inject custom topic — create a system message to steer the podcast
                from voice_chat import _write_group_state as _wgs_custom
                lock_state = _read_group_state(new_msg.guild.id)
                if lock_state and lock_state.get("mode") == "podcast" and lock_state.get("active"):
                    lock_state["last_message"] = {
                        "author_id": 0, "author_name": "System",
                        "text": f"📻 LISTENER REQUEST: {topic}\n\nHost, bring this up with your guests!",
                        "timestamp": time.time(),
                    }
                    lock_state["version"] = lock_state.get("version", 0) + 1
                    _wgs_custom(new_msg.guild.id, lock_state)
                    msg = f"📻 Topic injected: **{topic}**"
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.green()))
                    print(f"📻 [{bot_tag}] Podcast custom topic: {topic}")
                else:
                    msg = "No active podcast. Use `/podcast` or `!podcast` to start one first."
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.orange()))
            else:
                # Start podcast
                vc_channel = voice_session.vc.channel
                if vc_channel:
                    bot_members = [m for m in vc_channel.members if m.bot]
                    bot_ids = [m.id for m in bot_members]
                    if len(bot_ids) >= 3:
                        host_id_val = bot_ids[0]
                        guest_ids = [bid for bid in bot_ids if bid != host_id_val][:2]
                        turn_order = [host_id_val] + guest_ids
                        _init_podcast_state(new_msg.guild.id, turn_order, host_id_val)
                        voice_manager.set_group_config(new_msg.guild.id, turn_order, active=True)
                        other_ids = [bid for bid in turn_order if bid != discord_bot.user.id]
                        voice_session.enable_group_mode(
                            bot_id=discord_bot.user.id,
                            bot_name=config.get("tts_personality", "Bot"),
                            tts_personality=tts_personality,
                            other_bot_ids=other_ids,
                        )
                        if not voice_session.listening:
                            await voice_session.start_listening()
                        msg = (
                            f"🎙️ **Podcast is LIVE!**\\n"
                            f"Host: <@{host_id_val}> | Guests: {' & '.join(f'<@{gid}>' for gid in guest_ids)}\n"
                            f"📰 RSS articles auto-loading..."
                        )
                        if use_plain:
                            await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                        else:
                            await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.dark_purple()))
                        print(f"🎙️ [{bot_tag}] Podcast started by text command")
                    else:
                        msg = "Need at least 3 bots in VC for a podcast (host + 2 guests)"
                        if use_plain:
                            await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                        else:
                            await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
                else:
                    msg = "No VC channel found"
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
            return

        cmd_ttrpg = "/ttrpg" in content_lower or "!ttrpg" in content_lower

        if cmd_ttrpg:
            vc_channel = voice_session.vc.channel
            if vc_channel:
                bot_members = [m for m in vc_channel.members if m.bot]
                bot_ids = [m.id for m in bot_members]
                if len(bot_ids) >= 2:
                    dm_id_val = bot_ids[0]
                    hero_ids = [bid for bid in bot_ids if bid != dm_id_val]
                    turn_order = [dm_id_val] + hero_ids
                    _init_ttrpg_state(new_msg.guild.id, turn_order, dm_id_val)
                    voice_manager.set_group_config(new_msg.guild.id, turn_order, active=True)
                    other_ids = [bid for bid in turn_order if bid != discord_bot.user.id]
                    voice_session.enable_group_mode(
                        bot_id=discord_bot.user.id,
                        bot_name=config.get("tts_personality", "Bot"),
                        tts_personality=tts_personality,
                        other_bot_ids=other_ids,
                    )
                    if not voice_session.listening:
                        await voice_session.start_listening()
                    msg = (
                        f"🎲 **TTRPG Campaign Started!**\n"
                        f"DM: <@{dm_id_val}> | Heroes: {' & '.join(f'<@{hid}>' for hid in hero_ids)}\n"
                        f"Describe your actions — dice auto-roll!"
                    )
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.dark_red()))
                    print(f"🎲 [{bot_tag}] TTRPG started by text command")
                else:
                    msg = "Need at least 2 bots (DM + 1 hero)"
                    if use_plain:
                        await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                    else:
                        await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
            else:
                msg = "No VC channel found"
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
            return

        if cmd_start or cmd_stop:
            if cmd_start:
                # Gather all bot members in this voice channel
                vc_channel = voice_session.vc.channel
                if vc_channel:
                    bot_members = [m for m in vc_channel.members if m.bot]
                    bot_ids = [m.id for m in bot_members]
                    if bot_ids:
                        # Write shared state
                        state = {
                            "active": True,
                            "guild_id": new_msg.guild.id,
                            "turn_order": bot_ids,
                            "current_index": 0,
                            "last_speaker_time": time.time(),
                            "version": 0,
                            "last_message": {
                                "author_id": 0,
                                "author_name": "System",
                                "text": "Start a group conversation. Keep it going between yourselves — respond naturally, banter, keep the chat flowing.",
                                "timestamp": time.time(),
                            },
                        }
                        from voice_chat import _write_group_state as _wgs
                        _wgs(new_msg.guild.id, state)
                        voice_manager.set_group_config(new_msg.guild.id, bot_ids, active=True)

                        # Enable on this bot
                        other_ids = [bid for bid in bot_ids if bid != discord_bot.user.id]
                        voice_session.enable_group_mode(
                            bot_id=discord_bot.user.id,
                            bot_name=config.get("tts_personality", "Bot"),
                            tts_personality=tts_personality,
                            other_bot_ids=other_ids,
                        )
                        if not voice_session.listening:
                            await voice_session.start_listening()

                        turn_str = " → ".join(f"<@{bid}>" for bid in bot_ids)
                        msg = f"🎙️ Group mode STARTED! Turn order: {turn_str}"
                        if use_plain:
                            await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                        else:
                            await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.dark_green()))
                        print(f"🎙️ [{bot_tag}] Group mode started by text command: {bot_ids}")
                    else:
                        print(f"⚠️ [{bot_tag}] No bots found in VC for group start")
                else:
                    print(f"⚠️ [{bot_tag}] No VC channel for group start")
            elif cmd_stop:
                from voice_chat import _write_group_state as _wgs
                state = _read_group_state(new_msg.guild.id)
                state["active"] = False
                _wgs(new_msg.guild.id, state)
                if voice_session.group_mode:
                    voice_session.disable_group_mode()
                msg = "🛑 Group mode STOPPED"
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=msg)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
                print(f"🛑 [{bot_tag}] Group mode stopped by text command")
            return  # don't generate LLM response for commands

        messages = _condense_context(messages)
        response_contents = await _generate_for_voice(messages)
        full_response = "".join(response_contents)
        full_response = re.sub(r'^@\S+\s*[,.:]?\s*', '', full_response, count=1)
        print(f"💬 [{bot_tag}] LLM response for VC ({len(full_response)} chars)")

        if full_response.strip():
            try:
                for mem_entry in _auto_memory_retain(new_msg.content, new_msg.author.display_name):
                    if holo_store:
                        holo_store.add_fact(**mem_entry)
            except Exception:
                pass

            # Play TTS in VC — no Discord channel posting
            ok = await voice_session.speak_text(full_response, from_bot=voice_session.group_mode)
            if ok:
                print(f"🎤 [{bot_tag}] TTS played in VC")
                # ── Podcast: post article link to sidechat ──
                try:
                    p_state = _read_group_state(new_msg.guild.id)
                    if p_state.get("mode") == "podcast" and p_state.get("active"):
                        articles = p_state.get("articles", [])
                        art_idx = p_state.get("current_article_index", 0)
                        if articles and art_idx < len(articles) and articles[art_idx].get("url"):
                            url = articles[art_idx]["url"]
                            title = articles[art_idx]["title"]
                            if not p_state.get("article_link_posted"):
                                # Post the article link in sidechat
                                link_msg = f"📰 **Now discussing:** [{title}]({url})"
                                if use_plain:
                                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=link_msg)))
                                else:
                                    await new_msg.channel.send(embed=discord.Embed(description=link_msg, color=discord.Color.blue()))
                                # Mark as posted
                                from voice_chat import _write_group_state as _wgs_link
                                p_state["article_link_posted"] = True
                                _wgs_link(new_msg.guild.id, p_state)
                                print(f"🔗 [{bot_tag}] Article link posted to sidechat")
                except Exception as e:
                    logger.warning("Article link posting failed: %s", e)
                # In group mode, record response and advance turn
                if voice_session.group_mode:
                    next_bot = voice_session.advance_turn(new_msg.guild.id, last_text=full_response)
                    if next_bot:
                        print(f"🔄 [{bot_tag}] Turn advanced to <@{next_bot}>")
            else:
                print(f"⚠️ [{bot_tag}] VC TTS failed, falling back to text message")
                display_text = re.sub(r'\*[^*]+\*', '', full_response).strip()
                display_text = re.sub(r'  +', ' ', display_text).strip() or "..."
                if use_plain:
                    await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=display_text)))
                else:
                    await new_msg.channel.send(embed=discord.Embed(description=display_text, color=discord.Color.dark_green()))
        return  # skip normal text/TTS flows

    # When TTS is enabled: send text IMMEDIATELY, generate TTS in background
    if tts_enabled and tts_personality:
        print(f"🧠 [{bot_tag}] Sending to LLM ({provider_slash_model})...")
        messages = _condense_context(messages)
        response_contents = await _generate_for_voice(messages)
        full_response = "".join(response_contents)
        full_response = re.sub(r'^@\S+\s*[,.:]?\s*', '', full_response, count=1)
        print(f"💬 [{bot_tag}] LLM response received ({len(full_response)} chars)")

        # ── Auto-memory retention: save personal info about known people ──
        try:
            for mem_entry in _auto_memory_retain(new_msg.content, new_msg.author.display_name):
                holo_store.add_fact(**mem_entry)
                logging.info("Auto-memory saved: %s", mem_entry["content"][:80])
        except Exception as e:
            logging.warning("Auto-memory retain failed: %s", e)
        logging.info("Generated response preview: %r", full_response[:500])

        # ── Send text response FIRST (no waiting for TTS) ──
        if not full_response:
            full_response = "..."
        # Strip *italic* stage directions from display text (common in uncensored models)
        display_text = full_response.strip()
        display_text = re.sub(r'\*[^*]+\*', '', display_text)
        # Clean up and trim
        display_text = re.sub(r'  +', ' ', display_text).strip()
        if not display_text:
            display_text = "..."
        if use_plain:
            text_msg = await new_msg.channel.send(view=LayoutView().add_item(TextDisplay(content=display_text)))
        else:
            embed = discord.Embed(description=display_text, color=discord.Color.dark_green())
            text_msg = await new_msg.channel.send(embed=embed)
        response_msgs.append(text_msg)
        print(f"✉️ [{bot_tag}] Text sent to Discord")

        # ── Fire-and-forget TTS in background (text is already visible) ──
        tts_text = full_response.strip()
        # Skip TTS if response is an error message (starts with [Error:)
        if tts_text.startswith("[Error:") or tts_text.startswith("(Error:"):
            tts_text = ""
        if tts_text and tts_text != "...":
            async def _deliver_tts():
                try:
                    # ── Wadiyatalkinabeet: merge sound clip WITHIN TTS ──
                    if "wadiyatalkinabeet" in tts_text.lower() or "wadiatalkinabeet" in tts_text.lower():
                        sfx_files = [f for f in os.listdir(SFX_DIR) if f.endswith(".wav")]
                        if sfx_files:
                            import re as _re
                            # Split at first occurrence (case-insensitive)
                            match = _re.search(r'(?i)wadiyatalkinabeet', tts_text)
                            if not match:
                                match = _re.search(r'(?i)wadiatalkinabeet', tts_text)
                            if match:
                                idx = match.start()
                                before_text = tts_text[:idx].strip()
                                after_text = tts_text[match.end():].strip()
                            else:
                                before_text = tts_text
                                after_text = ""

                            # Generate TTS for text parts
                            wav_before = await generate_tts_audio(before_text) if before_text else None
                            wav_after = await generate_tts_audio(after_text) if after_text else None
                            sfx_chosen = os.path.join(SFX_DIR, random.choice(sfx_files))

                            # Merge: before_tts + sfx_clip + after_tts
                            import uuid as _uuid
                            merge_tag = str(_uuid.uuid4())[:8]
                            merge_wav = os.path.join(tempfile.gettempdir(), f"llmcord_merge_{merge_tag}.wav")
                            merge_ogg = os.path.join(tempfile.gettempdir(), f"llmcord_merge_{merge_tag}.ogg")

                            # Build ffmpeg filter: concat all available inputs
                            inputs = []
                            filter_parts = []
                            idx_input = 0
                            if wav_before:
                                inputs.extend(["-i", wav_before])
                                filter_parts.append(f"[{idx_input}:a]")
                                idx_input += 1
                            inputs.extend(["-i", sfx_chosen])
                            filter_parts.append(f"[{idx_input}:a]")
                            idx_input += 1
                            if wav_after:
                                inputs.extend(["-i", wav_after])
                                filter_parts.append(f"[{idx_input}:a]")
                                idx_input += 1

                            n_inputs = idx_input
                            concat_filter = "".join(filter_parts) + f"concat=n={n_inputs}:v=0:a=1[out]"
                            print(f"🔊 [{bot_tag}] Merging TTS + Wadiyatalkinabeet clip ({n_inputs} parts)")

                            ffmpeg_args = ["ffmpeg", "-y"] + inputs + [
                                "-filter_complex", concat_filter,
                                "-map", "[out]", merge_wav,
                                "-loglevel", "error",
                            ]
                            ffmpeg_proc = await asyncio.create_subprocess_exec(
                                *ffmpeg_args,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            )
                            _, merge_stderr = await ffmpeg_proc.communicate()

                            if ffmpeg_proc.returncode == 0 and os.path.exists(merge_wav) and os.path.getsize(merge_wav) > 100:
                                # Convert to OGG
                                ffmpeg_ogg = await asyncio.create_subprocess_exec(
                                    "ffmpeg", "-y", "-i", merge_wav,
                                    "-c:a", "libopus", "-b:a", "24k",
                                    "-application", "voip", "-vbr", "off",
                                    "-ar", "48000", "-ac", "1", merge_ogg,
                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                )
                                _, _ = await ffmpeg_ogg.communicate()
                                if ffmpeg_ogg.returncode == 0 and os.path.exists(merge_ogg) and os.path.getsize(merge_ogg) > 100:
                                    ok = await send_discord_voice_message(new_msg.channel.id, merge_ogg, config["bot_token"])
                                    if ok:
                                        print(f"🎤 [{bot_tag}] Merged TTS+clip sent to Discord")
                            else:
                                logging.warning("Merge failed: %s", merge_stderr.decode())
                                # Fallback: send clip only
                                chosen = sfx_chosen
                                print(f"🔊 [{bot_tag}] Fallback: clip only: {chosen}")
                                ogg_path = chosen.replace(".wav", ".ogg")
                                ffmpeg_proc = await asyncio.create_subprocess_exec(
                                    "ffmpeg", "-y", "-i", chosen,
                                    "-c:a", "libopus", "-b:a", "24k",
                                    "-application", "voip", "-vbr", "off",
                                    "-ar", "48000", "-ac", "1", ogg_path,
                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                )
                                _, _ = await ffmpeg_proc.communicate()
                                if ffmpeg_proc.returncode == 0 and os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 100:
                                    ok = await send_discord_voice_message(new_msg.channel.id, ogg_path, config["bot_token"])
                                for p in [ogg_path]:
                                    try: os.unlink(p)
                                    except FileNotFoundError: pass

                            # Cleanup temp files
                            for p in [merge_wav, merge_ogg, wav_before, wav_after]:
                                if p:
                                    try: os.unlink(p)
                                    except FileNotFoundError: pass
                            return

                    print(f"🔊 [{bot_tag}] Sending to voice server...")
                    # Split long TTS into multiple sequential messages (no CBN chunking)
                    # Max ~100 words per message to avoid VRAM issues
                    _MAX_TTS_WORDS = 100
                    tts_words = tts_text.split()
                    if len(tts_words) > _MAX_TTS_WORDS:
                        # Split at sentence boundaries near the limit
                        tts_segments = []
                        current = []
                        wc = 0
                        for word in tts_words:
                            current.append(word)
                            wc += 1
                            if wc >= _MAX_TTS_WORDS and word.endswith(('.', '!', '?')):
                                tts_segments.append(" ".join(current))
                                current = []
                                wc = 0
                        if current:
                            tts_segments.append(" ".join(current))
                    else:
                        tts_segments = [tts_text]

                    for seg_idx, seg_text in enumerate(tts_segments):
                        if seg_idx > 0:
                            await asyncio.sleep(0.5)  # brief pause between messages
                        tts_path = await generate_tts_audio(seg_text)
                        if not tts_path:
                            continue
                        ogg_path = tts_path.replace(".wav", ".ogg")
                    ffmpeg_proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", tts_path,
                        "-c:a", "libopus", "-b:a", "24k",
                        "-application", "voip", "-vbr", "off",
                        "-ar", "48000", "-ac", "1", ogg_path,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    _, ffmpeg_stderr = await ffmpeg_proc.communicate()
                    if ffmpeg_proc.returncode == 0 and os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 100:
                        ok = await send_discord_voice_message(new_msg.channel.id, ogg_path, config["bot_token"])
                        if ok:
                            print(f"🎤 [{bot_tag}] Voice sent to Discord")
                        else:
                            await new_msg.channel.send(file=discord.File(tts_path))
                    else:
                        await new_msg.channel.send(file=discord.File(tts_path))
                    for p in [ogg_path, tts_path]:
                        try:
                            os.unlink(p)
                        except FileNotFoundError:
                            pass
                except Exception as e:
                    logging.exception("Background TTS failed: %s", e)

            asyncio.ensure_future(_deliver_tts())
    else:
        # Normal flow: stream response to Discord as it's generated
        print(f"🧠 [{bot_tag}] Sending to LLM ({provider_slash_model})...")
        messages = _condense_context(messages)
        response_contents = await _generate(messages)
        full_response = "".join(response_contents)
        full_response = re.sub(r'^@\S+\s*[,.:]?\s*', '', full_response, count=1)
        print(f"💬 [{bot_tag}] LLM response received ({len(full_response)} chars)")

        # ── Auto-memory retention: save personal info about known people ──
        try:
            for mem_entry in _auto_memory_retain(new_msg.content, new_msg.author.display_name):
                holo_store.add_fact(**mem_entry)
                logging.info("Auto-memory saved: %s", mem_entry["content"][:80])
        except Exception as e:
            logging.warning("Auto-memory retain failed: %s", e)

        logging.info("Generated response preview: %r", full_response[:500])

        # ── Fallback for empty responses ──
        if not full_response.strip() and not response_msgs:
            try:
                msg = await new_msg.channel.send("...")
            except Exception:
                msg = None
            if msg:
                response_msgs.append(msg)
            full_response = "..."

        # ── Non-TTS text was streamed to Discord during generation ──
        if response_msgs:
            print(f"✉️ [{bot_tag}] Text sent to Discord")

    # ── Check for Hermes tool calls ──
    hermes_matches = list(HERMES_TOOL_RE.finditer(full_response))
    if hermes_matches:
        # Strip all hermes tags from displayed text
        clean_response = HERMES_TOOL_RE.sub("", full_response).strip()

        # Send the cleaned text (without tool tags) as the initial response
        if clean_response:
            # Edit the last message to show the text without tags
            if response_msgs:
                last_msg = response_msgs[-1]
                if config.get("use_plain_responses", False):
                    await last_msg.edit(view=LayoutView().add_item(TextDisplay(content=clean_response)))
                else:
                    embed = last_msg.embeds[0] if last_msg.embeds else discord.Embed()
                    embed.description = clean_response
                    embed.color = EMBED_COLOR_COMPLETE
                    await last_msg.edit(embed=embed)

        # Execute each tool and collect results
        tool_results = []
        for match in hermes_matches:
            tool_name = match.group(1)
            tool_input = match.group(2)
            logging.info("Hermes tool call: %s <- %s", tool_name, tool_input[:100])
            # Send a "thinking" message
            thinking_msg = await new_msg.reply(f"🔧 Running `{tool_name}`...", silent=True)
            result = await run_hermes_tool(tool_name, tool_input, new_msg.channel.id)
            await thinking_msg.delete()
            tool_results.append(f"[{tool_name} result]\n{result[:2000]}")

        # ── Follow-up generation pass with tool results ──
        if tool_results:
            follow_up = "\n\n".join(tool_results)
            # Add tool results as a system message for the model
            follow_messages = [m for m in messages]
            follow_messages.append(dict(role="system", content=f"Hermes tool results:\n{follow_up}\n\nContinue your response. Incorporate the result above naturally."))

            response_msgs2: list[discord.Message] = []
            response_contents2 = await _generate(follow_messages)

    # Clean up old MsgNodes
    if (num_nodes := len(msg_nodes)) > MAX_MESSAGE_NODES:
        for msg_id in sorted(msg_nodes.keys())[: num_nodes - MAX_MESSAGE_NODES]:
            async with msg_nodes.setdefault(msg_id, MsgNode()).lock:
                msg_nodes.pop(msg_id, None)


async def main() -> None:
    await discord_bot.start(config["bot_token"])


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
