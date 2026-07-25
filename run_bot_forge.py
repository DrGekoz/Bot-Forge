#!/usr/bin/env python3
"""
Bot-Forge Unified Server Launcher
Starts PocketTTS (if installed) + all bot instances + memory server
in one terminal with colored, labeled logs.
"""

import asyncio
import os
import sys
import re
import signal
import time
import json
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BOTS_DIR = ROOT_DIR / "bots"
CORE_DIR = ROOT_DIR / "core"
POCKETTTS_DIR = ROOT_DIR / "pockettts"

# ── ANSI Colors ──
class Color:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    CLEAR_LINE = "\033[K"


# ── Logger ──
def log(label: str, msg: str, color: str = Color.GREEN):
    timestamp = datetime.now().strftime("%H:%M:%S")
    label_padded = f"{label:>12}"
    print(f"{Color.DIM}{timestamp}{Color.RESET} {color}{label_padded}{Color.RESET} │ {msg}")


def log_error(label: str, msg: str):
    log(label, msg, Color.RED)


def log_warn(label: str, msg: str):
    log(label, msg, Color.YELLOW)


class BotProcess:
    """Manages one subprocess with streaming log output."""

    def __init__(self, name: str, cmd: list, cwd: str, color: str = Color.CYAN):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.color = color
        self.process = None
        self.running = False

    async def start(self):
        log(self.name, f"Starting: {' '.join(self.cmd)}", self.color)
        self.process = await asyncio.create_subprocess_exec(
            *self.cmd,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        self.running = True

    async def stream_output(self):
        """Read and print stdout/stderr as it arrives."""
        async def read_stream(stream, is_error=False):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    c = Color.RED if is_error else self.color
                    log(self.name, text, c)

        await asyncio.gather(
            read_stream(self.process.stdout),
            read_stream(self.process.stderr, is_error=True),
        )
        self.running = False

    async def stop(self):
        if self.process and self.process.returncode is None:
            log(self.name, "Shutting down...", self.color)
            try:
                self.process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    log_warn(self.name, "Killing (timeout)")
                    self.process.kill()
                    await self.process.wait()
            except Exception as e:
                log_error(self.name, f"Stop error: {e}")
            log(self.name, "Stopped", self.color)


def find_bot_configs() -> list[dict]:
    """Find all bot configs in the bots/ directory."""
    bots = []
    if not BOTS_DIR.exists():
        return bots
    for entry in sorted(BOTS_DIR.iterdir()):
        if entry.is_dir():
            config_file = entry / "config.yaml"
            if config_file.exists():
                # Extract bot name from config
                name = entry.name
                bots.append({
                    "name": name,
                    "config": str(config_file),
                    "dir": str(entry),
                })
    return bots


def check_pockettts() -> bool:
    """Check if PocketTTS is available."""
    try:
        import pocket_tts
        return True
    except ImportError:
        return False


async def start_pockettts() -> BotProcess:
    """Start the PocketTTS server."""
    python = sys.executable
    proc = BotProcess(
        "PocketTTS",
        [python, "-m", "pocket_tts.server", "--port", "8769", "--quantize"],
        cwd=str(POCKETTTS_DIR) if POCKETTTS_DIR.exists() else str(ROOT_DIR),
        color=Color.MAGENTA,
    )
    await proc.start()
    # Wait a moment for server to initialize
    await asyncio.sleep(2)
    # Check if it's actually running
    if proc.process and proc.process.returncode is None:
        log("PocketTTS", "Server running on port 8769", Color.GREEN)
    return proc


async def start_memory_server() -> BotProcess:
    """Optional lightweight memory API server."""
    python = sys.executable
    server_script = CORE_DIR / "memory_server.py"
    if server_script.exists():
        proc = BotProcess(
            "Memory",
            [python, str(server_script), "--port", "8888"],
            cwd=str(ROOT_DIR),
            color=Color.BLUE,
        )
        await proc.start()
        await asyncio.sleep(1)
        log("Memory", "Memory server running on port 8888", Color.GREEN)
        return proc
    return None


async def start_bots(bot_configs: list[dict]) -> list[BotProcess]:
    """Start all bot instances."""
    python = sys.executable
    llmcord_path = CORE_DIR / "llmcord.py"
    colors = [Color.CYAN, Color.GREEN, Color.YELLOW, Color.MAGENTA, Color.BLUE,
              Color.CYAN, Color.GREEN, Color.YELLOW, Color.MAGENTA, Color.BLUE]
    
    procs = []
    for i, bot in enumerate(bot_configs):
        color = colors[i % len(colors)]
        name = bot["name"]
        
        proc = BotProcess(
            name,
            [python, "-u", str(llmcord_path)],
            cwd=bot["dir"],
            color=color,
        )
        await proc.start()
        procs.append(proc)
    
    return procs


def print_banner(bots: list[dict], has_tts: bool):
    """Print the startup banner."""
    print()
    print(f"{Color.CYAN}{Color.BOLD}╔═══════════════════════════════════════════════╗{Color.RESET}")
    print(f"{Color.CYAN}{Color.BOLD}║         Bot-Forge Server Launcher            ║{Color.RESET}")
    print(f"{Color.CYAN}{Color.BOLD}╚═══════════════════════════════════════════════╝{Color.RESET}")
    print()
    log("System", f"Found {len(bots)} bot(s) configured")
    for bot in bots:
        log("System", f"  {bot['name']} → {bot['config']}", Color.DIM)
    log("System", f"PocketTTS: {'AVAILABLE' if has_tts else 'NOT INSTALLED'}")
    log("System", f"Memory DB: {CORE_DIR.parent / 'memory_store.db'}")
    print()
    log("System", "Starting services...", Color.GREEN)
    print()


def print_shutdown():
    print()
    log("System", "All services stopped. Goodbye!", Color.GREEN)
    print()


async def main():
    # Find bots
    bot_configs = find_bot_configs()
    has_tts = check_pockettts()
    
    if not bot_configs:
        log_error("System", "No bot configs found in bots/ directory!")
        log_error("System", "Run Setup.bat first to configure your bots.")
        print()
        return
    
    print_banner(bot_configs, has_tts)

    # Clean stale .llmcord.lock files from crashed bot runs
    for bot in bot_configs:
        lock = Path(bot["dir"]) / ".llmcord.lock"
        if lock.exists():
            try:
                lock.unlink()
                log("System", f"Cleaned stale lock: {bot['name']}", Color.YELLOW)
            except Exception as e:
                log_warn("System", f"Lock cleanup for {bot['name']}: {e}")

    # Collect all processes
    all_procs = []
    
    # 1. Start memory server
    memory_proc = await start_memory_server()
    if memory_proc:
        all_procs.append(memory_proc)
    else:
        log_warn("Memory", "No memory_server.py found — skipping (bots run without memory)")
    
    # 2. Start PocketTTS
    if has_tts:
        tts_proc = await start_pockettts()
        all_procs.append(tts_proc)
    else:
        log_warn("PocketTTS", "Not installed — bots will use subprocess TTS fallback")
    
    # 3. Start all bots
    bot_procs = await start_bots(bot_configs)
    all_procs.extend(bot_procs)
    
    # 4. Stream output until interrupted
    log("System", f"{'─'*40}", Color.DIM)
    log("System", "All services running! Press Ctrl+C to stop.", Color.BOLD)
    log("System", f"{'─'*40}", Color.DIM)
    print()
    
    try:
        # Stream all outputs concurrently
        await asyncio.gather(*[p.stream_output() for p in all_procs])
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        # Shutdown in reverse order (bots first, then TTS, then memory)
        log("System", "Shutting down all services...", Color.YELLOW)
        print()
        
        for proc in reversed(bot_procs):
            await proc.stop()
        
        if has_tts:
            await tts_proc.stop()
        
        if memory_proc:
            await memory_proc.stop()
        
        print_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
