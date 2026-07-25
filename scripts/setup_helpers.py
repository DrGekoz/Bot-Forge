#!/usr/bin/env python3
"""
Bot-Forge Setup Helper
Handles the heavy lifting: dependency installation, PocketTTS setup,
voice cloning, config generation, and invite URL creation.
"""

import os
import sys
import json
import subprocess
import shutil
import platform
import re
from pathlib import Path
from string import Template

ROOT_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = ROOT_DIR / "core"
BOTS_DIR = ROOT_DIR / "bots"
VOICE_REFS_DIR = ROOT_DIR / "voice-refs"
POCKETTTS_DIR = ROOT_DIR / "pockettts"
POCKETTTS_REPO = POCKETTTS_DIR / "PocketTTS"
POCKETTTS_MODELS = POCKETTTS_DIR / "models"
POCKETTTS_VOICES = POCKETTTS_DIR / "voices"


# ── Utility ──

def cyan(text): return f"\033[96m{text}\033[0m"
def green(text): return f"\033[92m{text}\033[0m"
def yellow(text): return f"\033[93m{text}\033[0m"
def red(text): return f"\033[91m{text}\033[0m"


def run(cmd, cwd=None, capture=False):
    """Run a shell command, print output."""
    if capture:
        try:
            result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120)
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""
        except Exception as e:
            print(red(f"  Command failed: {e}"))
            return ""
    else:
        print(f"  Running: {cmd}")
        result = subprocess.run(cmd, shell=True, cwd=cwd)
        return result.returncode == 0


def prompt(text, default=None):
    """Ask user for input with optional default."""
    if default:
        result = input(f"{text} [{default}]: ").strip()
        return result if result else default
    return input(f"{text}: ").strip()


def confirm(text):
    """Ask for yes/no."""
    result = input(f"{text} (y/N): ").strip().lower()
    return result in ("y", "yes")


# ── Step 1: Install Python Dependencies ──

def install_dependencies():
    """Install Python packages for the core framework."""
    print(f"\n{cyan}━━━ Step 1: Installing Python Dependencies ━━━{reset}")

    req_files = [
        ROOT_DIR / "requirements.txt",
        CORE_DIR / "requirements.txt",
    ]

    for req_file in req_files:
        if req_file.exists():
            print(f"  Installing from {req_file.name}...")
            run(f'"{sys.executable}" -m pip install -r "{req_file}"', capture=False)

    # Ensure base deps
    deps = ["discord.py>=2.6.0", "httpx>=0.27.0", "openai>=1.0.0", "pyyaml>=6.0"]
    for dep in deps:
        run(f'"{sys.executable}" -m pip install "{dep}"', capture=False)

    print(green("  ✅ Dependencies installed"))


# ── Step 2: Discord Bot Token Helpers ──

DISCORD_DEV_URL = "https://discord.com/developers/applications"


def print_bot_creation_guide():
    """Print instructions for creating a Discord bot."""
    print(f"""
{cyan}How to create a Discord Bot:{reset}
  1. Go to {DISCORD_DEV_URL}
  2. Click {yellow}"New Application"{reset} → give it a name
  3. Go to {yellow}"Bot"{reset} in the left sidebar
  4. Click {yellow}"Reset Token"{reset} → {red}COPY THE TOKEN NOW{reset}
  5. Under {yellow}"Privileged Gateway Intents"{reset}, enable:
     - {yellow}MESSAGE CONTENT INTENT{reset}
     - {yellow}SERVER MEMBERS INTENT{reset} (if you want member-aware bots)
  6. Go to {yellow}"OAuth2"{reset} → {yellow}"URL Generator"{reset}
  7. Check {yellow}"bot"{reset} and {yellow}"applications.commands"{reset}
  8. Check these bot permissions:
     - {yellow}Send Messages{reset}
     - {yellow}Connect{reset}
     - {yellow}Speak{reset}
     - {yellow}Use Voice Activity{reset}
     - {yellow}Read Message History{reset}
  9. Copy the generated URL — you'll use it to invite the bot
""")


def validate_token(token):
    """Quick validation: token should start with 'sk-' or similar Discord format."""
    token = token.strip()
    # Discord bot tokens: MTEx... or the new format starting with sk-
    if len(token) < 50:
        return False
    parts = token.split(".")
    return len(parts) == 3 if "." in token else len(token) > 50


def get_bot_token(name, num):
    """Get a valid bot token from the user."""
    print(f"\n{cyan}Bot #{num}: {name}{reset}")
    while True:
        token = prompt(f"  Paste the bot token for {name}", "")
        if validate_token(token):
            return token
        print(red("  That doesn't look like a valid Discord bot token. Try again."))
        print(yellow("  (It should be a long string like MTExNjExMjYw... or sk-...)"))


# ── Step 3: AI Provider Configuration ──

PROVIDERS = {
    "lmstudio": {"name": "LM Studio", "base_url": "http://localhost:1234/v1", "needs_key": False, "model": "llama-3.2-3b-instruct"},
    "ollama": {"name": "Ollama", "base_url": "http://localhost:11434/v1", "needs_key": False, "model": "llama3.2:3b"},
    "vllm": {"name": "vLLM", "base_url": "http://localhost:8000/v1", "needs_key": False, "model": "mistral-7b-instruct"},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "needs_key": True, "model": "gpt-4o-mini"},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "needs_key": True, "model": "anthropic/claude-3-haiku"},
    "gemini": {"name": "Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "needs_key": True, "model": "gemini-2.0-flash"},
    "opencode_go": {"name": "OpenCode GO", "base_url": "", "needs_key": True, "model": "deepseek-v4-flash"},
    "opencode_zen": {"name": "OpenCode Zen", "base_url": "", "needs_key": True, "model": "deepseek-v4-flash"},
    "custom": {"name": "Custom", "base_url": "", "needs_key": False, "model": ""},
}


def configure_provider():
    """Ask user for AI provider details."""
    print(f"\n{cyan}━━━ AI Provider Configuration ━━━{reset}")
    print("Choose your AI provider for bot responses:")

    provider_keys = list(PROVIDERS.keys())
    for i, (key, info) in enumerate(PROVIDERS.items(), 1):
        print(f"  {i}. {info['name']}")

    choice = prompt(f"  Select provider (1-{len(provider_keys)})", "1")
    try:
        idx = int(choice) - 1
        provider_key = provider_keys[idx]
    except (ValueError, IndexError):
        print(yellow("  Invalid choice, using LM Studio"))
        provider_key = "lmstudio"

    provider = PROVIDERS[provider_key]

    if not provider["base_url"] and provider_key != "custom":
        base_url = prompt(f"  Enter {provider['name']} base URL", "https://api.example.com/v1")
    elif provider_key == "custom":
        base_url = prompt("  Enter provider base URL", "http://localhost:1234/v1")
    else:
        base_url = provider["base_url"]

    api_key = ""
    if provider["needs_key"]:
        api_key = prompt(f"  Enter your {provider['name']} API key", "")
        while not api_key:
            print(red("  API key is required for this provider"))
            api_key = prompt(f"  Enter your {provider['name']} API key", "")

    model = prompt(f"  Model name", provider["model"])

    return {
        "provider": provider_key,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }


# ── Step 4: PocketTTS Setup ──

def check_pockettts_installed():
    """Check if PocketTTS is already installed."""
    if POCKETTTS_REPO.exists():
        return True
    # Check if pocket-tts CLI is available
    result = run("pocket-tts --version 2>nul", capture=True) if os.name == "nt" else run("pocket-tts --version 2>/dev/null", capture=True)
    return bool(result)


def check_pockettts_server_running():
    """Quick check if PocketTTS server is running on port 8769."""
    if os.name == "nt":
        result = run("netstat -an | findstr :8769", capture=True)
    else:
        result = run("ss -tln | grep :8769", capture=True)
    return bool(result)


def install_pockettts():
    """Install PocketTTS."""
    print(f"\n{cyan}━━━ Installing PocketTTS ━━━{reset}")

    if check_pockettts_installed():
        print(green("  ✅ PocketTTS is already installed"))
        if not check_pockettts_server_running():
            print(yellow("  ⚠️  PocketTTS server doesn't seem to be running"))
            if confirm("  Start PocketTTS server now?"):
                start_pockettts_server()
        return

    print("  Cloning PocketTTS...")
    POCKETTTS_DIR.mkdir(parents=True, exist_ok=True)
    run(f'git clone https://github.com/Kyutai/PocketTTS.git "{POCKETTTS_REPO}"')

    print("  Installing PocketTTS...")
    run(f'"{sys.executable}" -m pip install -e "{POCKETTTS_REPO}"')

    print(yellow("  PocketTTS needs to download the voice cloning model (~2GB on first run)"))
    if confirm("  Download model now?"):
        run(f'pocket-tts download-model')
    else:
        print(yellow("  You can download it later with: pocket-tts download-model"))

    print(green("  ✅ PocketTTS installed"))


def start_pockettts_server():
    """Start PocketTTS server."""
    print(f"\n  Starting PocketTTS server on port 8769...")
    server_script = ROOT_DIR / "run_pockettts.bat"
    server_content = f"""@echo off
echo Starting PocketTTS Server...
start /B "" "{sys.executable}" -m pocket_tts.server --port 8769 --quantize
echo PocketTTS server starting on port 8769
echo Close this window to stop the server.
timeout /t 5 >nul
"""
    server_script.write_text(server_content)
    print(green(f"  ✅ Created {server_script.name}"))
    print(yellow("  Run run_pockettts.bat to start the server"))
    return str(server_script)


def create_voice_clone(bot_name, ref_path):
    """Create a voice clone from a reference WAV file."""
    print(f"\n  Creating voice clone for {bot_name}...")
    output_path = POCKETTTS_VOICES / f"{bot_name}.pt"
    POCKETTTS_VOICES.mkdir(parents=True, exist_ok=True)
    run(f'pocket-tts export-voice "{ref_path}" "{output_path}"')
    if output_path.exists():
        print(green(f"  ✅ Voice clone saved: {output_path}"))
        return str(output_path)
    else:
        print(red("  ❌ Voice cloning failed"))
        return ""


def get_voice_ref_file(bot_name, index):
    """Get a voice reference file from the user."""
    print(f"\n{cyan}  Voice Reference for {bot_name}{reset}")
    print(yellow("  Requirements: 8-15 seconds, WAV/MP3/OGG, clear speech, under 5MB"))

    # Check if user wants to skip
    if confirm("  Do you want to add a voice reference? (Recommended)"):
        while True:
            ref_path = prompt(f"  Path to audio file (e.g., C:\\Users\\you\\voice.wav)", "")
            ref_path = ref_path.strip('"').strip("'")

            p = Path(ref_path)
            if not p.exists():
                print(red(f"  File not found: {ref_path}"))
                continue

            if p.suffix.lower() not in (".wav", ".mp3", ".ogg", ".flac", ".m4a"):
                print(yellow(f"  Warning: {p.suffix} may not be supported. WAV recommended."))

            # Copy to voice-refs directory
            dest = VOICE_REFS_DIR / f"{bot_name}{p.suffix}"
            VOICE_REFS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(p), str(dest))
                print(green(f"  ✅ Copied to {dest}"))
                return str(dest)
            except Exception as e:
                print(red(f"  Error copying file: {e}"))
                continue

    return ""


# ── Step 5: Config Generation ──

def generate_config(bot_name, bot_token, provider_config, personality_prompt, voice_ref_path=None, use_separate_model=False):
    """Generate a config.yaml for a bot."""
    config_path = BOTS_DIR / bot_name / "config.yaml"
    BOTS_DIR.mkdir(parents=True, exist_ok=True)
    (BOTS_DIR / bot_name).mkdir(parents=True, exist_ok=True)

    # Determine client_id from token
    client_id = ""
    if bot_token.startswith("MT"):
        try:
            import base64
            # Discord tokens: base64 encoded client_id
            client_id = base64.b64decode(bot_token.split(".")[0].encode()).decode()
        except:
            client_id = "YOUR_CLIENT_ID"
    else:
        client_id = "YOUR_CLIENT_ID"

    # Escape YAML special characters in prompt
    prompt_escaped = personality_prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    api_key = provider_config.get("api_key", "")
    if api_key and api_key != "***":
        api_key_escaped = api_key
    else:
        api_key_escaped = ""

    config = f"""# Bot-Forge — {bot_name}
bot_token: "{bot_token}"
client_id: "{client_id}"
status_message: "{bot_name} 🎮"

max_text: 100000
max_images: 5
max_messages: 3
use_plain_responses: true
allow_dms: false
message_content_intent: true

permissions:
  users:
    admin_ids: []
    allowed_ids: []
    blocked_ids: []

providers:
  main:
    base_url: "{provider_config['base_url']}"
    api_key: "{api_key_escaped}"
    model: "{provider_config['model']}"

tts_personality: "{bot_name}"

system_prompt: |-
  IMPORTANT: You are a bot. You are NOT the user.
  Never claim to be the user.
  You are {bot_name}.

personality_prompt: |-
{personality_prompt}
"""
    config_path.write_text(config)
    print(green(f"  ✅ Config written: {config_path}"))
    return str(config_path)


def generate_start_script(bot_name):
    """Generate a start.bat for a bot."""
    # We need to reference the core llmcord.py
    python_exe = sys.executable
    script_path = BOTS_DIR / bot_name / "start.bat"

    bot_dir = BOTS_DIR / bot_name
    core_path = CORE_DIR / "llmcord.py"

    content = f"""@echo off
cd /d "{bot_dir}"
echo Starting {bot_name}...
"{python_exe}" -u "{core_path}" >> "{bot_dir}\\stdout.log" 2>> "{bot_dir}\\stderr.log"
echo {bot_name} stopped.
pause
"""
    script_path.write_text(content)
    print(green(f"  ✅ Start script: {script_path}"))
    return str(script_path)


def generate_run_all_script(bot_names):
    """Generate a script that starts all bots."""
    script_path = ROOT_DIR / "run_all_bots.bat"
    lines = ["@echo off", "echo Starting all Bot-Forge bots...", ""]
    for name in bot_names:
        lines.append(f'start "Bot-Forge - {name}" cmd /c "{BOTS_DIR / name / "start.bat"}"')
    lines.append("")
    lines.append("echo All bots started! Check their windows for status.")
    lines.append("pause")
    script_path.write_text("\n".join(lines))
    print(green(f"  ✅ Created run_all_bots.bat"))
    return str(script_path)


def generate_invite_url(client_id):
    """Generate a Discord OAuth2 invite URL."""
    permissions = 277025770560  # Send Messages + Connect + Speak + Use VAD + Read History + etc
    return f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions={permissions}&scope=bot+applications.commands"


def generate_invite_urls(configs):
    """Print invite URLs for all bots."""
    print(f"\n{cyan}━━━ Bot Invite Links ━━━{reset}")
    print(f"\n{yellow}Click each link to invite the bots to your server:{reset}\n")
    for bot_name, client_id in configs:
        url = generate_invite_url(client_id)
        print(f"  {green(bot_name)}{reset}:")
        print(f"  {url}")
        print()
    print(yellow("Make sure all bots can see the same server before using game modes.\n"))


# ── Step 6: Main Setup Flow ──

def main():
    """Run the full setup wizard."""
    # Reset ANSI codes
    global reset
    reset = "\033[0m"

    print(f"""
{cyan}╔═══════════════════════════════════════════════╗
║         Bot-Forge — Discord AI Games          ║
║         Interactive Setup Wizard               ║
╚═══════════════════════════════════════════════╝{reset}
""")

    # 1. Install dependencies
    if confirm("Step 1: Install Python dependencies?"):
        install_dependencies()

    # 2. How many bots?
    print(f"\n{cyan}━━━ Bot Configuration ━━━{reset}")
    num_bots = prompt("How many bots do you want?", "3")
    try:
        num_bots = int(num_bots)
        num_bots = max(2, min(10, num_bots))
    except ValueError:
        print(yellow("  Invalid number, using 3 bots"))
        num_bots = 3

    # 3. Bot creation guide
    print_bot_creation_guide()

    # 4. Configure AI provider (ask once, or per-bot)
    same_provider = not confirm("Use a different AI provider/model for each bot?")
    if same_provider:
        provider_config = configure_provider()

    # 5. PocketTTS
    install_pockettts()

    # 6. For each bot
    bot_configs = []
    for i in range(1, num_bots + 1):
        print(f"\n{cyan}═══════════════════════════════════════{reset}")
        print(f"{cyan}  Bot #{i}{reset}")
        print(f"{cyan}═══════════════════════════════════════{reset}")

        name = prompt(f"  Bot name", f"Bot{i}")

        print_bot_creation_guide()
        token = get_bot_token(name, i)

        print(f"\n  {cyan}Personality Prompt for {name}:{reset}")
        print(yellow("  Describe how this bot should behave. E.g.:"))
        print(yellow('  "You are a fast-talking auctioneer. You sell bizarre items'))
        print(yellow('   with high energy and rhythmic flair."'))
        print(yellow("  Type DONE on a blank line when finished.\n"))
        lines = []
        while True:
            line = input("  ")
            if line.strip().upper() == "DONE":
                break
            lines.append(line)
        personality = "\n".join(lines)

        if not same_provider:
            provider_config = configure_provider()

        voice_ref = get_voice_ref_file(name, i)

        # Create voice clone
        clone_path = ""
        if voice_ref:
            if confirm(f"  Create voice clone for {name} now?"):
                clone_path = create_voice_clone(name, voice_ref)
                if not clone_path:
                    print(yellow("  Voice clone failed — bot will use default TTS"))

        # Generate config
        config_path = generate_config(name, token, provider_config, personality, voice_ref)
        start_script = generate_start_script(name)

        # Extract client_id from token
        cid = ""
        if token.startswith("MT"):
            try:
                import base64
                cid = base64.b64decode(token.split(".")[0].encode()).decode()
            except:
                cid = ""
        bot_configs.append((name, cid))

    # 7. Generate run-all script
    bot_names = [c[0] for c in bot_configs]
    generate_run_all_script(bot_names)

    # 8. PocketTTS run script
    start_pockettts_server()

    # 9. Print invite URLs
    generate_invite_urls(bot_configs)

    # 10. Summary
    print(f"\n{green}╔═══════════════════════════════════════════╗")
    print(f"║         Setup Complete! 🎉                ║")
    print(f"╚═══════════════════════════════════════════╝{reset}")
    print(f"""
{cyan}Next steps:{reset}
  1. Click each invite link above to add bots to your Discord server
  2. Run {green}run_pockettts.bat{reset} to start the TTS server
  3. Run {green}run_all_bots.bat{reset} to start all bots
  4. Join a voice channel in Discord
  5. Type {green}/join{reset} to have a bot join
  6. Start a game mode:
     - {green}/auction_house{reset} — Auction House game
     - {green}/group{reset} — Group conversation
     - {green}/debate{reset} — Structured debate

{cyan}Need help?{reset}
  - Check the README.md for full documentation
  - DM me on Discord: {yellow}drgekoz{reset}
""")

    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
