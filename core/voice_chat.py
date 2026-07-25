#!/usr/bin/env python3
"""
Bot-Forge / LLMCord Voice Chat & Game Mode Engine
===================================================
Provides Discord VC integration, all game mode logic,
and new game modes (20 Questions, Show & Tell, Pokemon/MTG).
"""
import asyncio
import json
import logging
import os
import random
import re
import struct
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

import discord
import numpy as np

logger = logging.getLogger("voice_chat")

# ── New Game Modes (@imports from sibling module) ──
try:
    import new_game_modes as ngm
    _NGM_AVAILABLE = True
except ImportError:
    ngm = None
    _NGM_AVAILABLE = False

# ── Silero VAD ──
try:
    import silero_vad as _sv
    _SILERO_AVAILABLE = True
except ImportError:
    _SILERO_AVAILABLE = False

# ── faster-whisper STT ──
try:
    from faster_whisper import WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    _WHISPER_AVAILABLE = False

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════
STATE_DIR = Path("vc_group_state")
STATE_DIR.mkdir(exist_ok=True)
MAX_CONTEXT_EXCHANGES = 30
DEFAULT_MAX_ROUNDS = 3

# ═══════════════════════════════════════════════════════
#  State Management (File-based IPC)
# ═══════════════════════════════════════════════════════
_shared_state_dir = STATE_DIR

def _lock_file_path(guild_id: int) -> str:
    return str(_shared_state_dir / f".lock_{guild_id}")

def _lock_state(timeout: float = 3.0):
    lock_path = _lock_file_path(0)
    start = time.time()
    while time.time() - start < timeout:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock_path
        except FileExistsError:
            time.sleep(0.05)
    return None

def _unlock_state(lock_path: str):
    if lock_path and os.path.exists(lock_path):
        os.remove(lock_path)

def _state_path(guild_id: int) -> str:
    return str(_shared_state_dir / f"g{guild_id}.json")

def _read_group_state(guild_id: int) -> dict:
    path = _state_path(guild_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def _write_group_state(guild_id: int, state: dict):
    path = _state_path(guild_id)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)

# Stub imports for downstream llmcord.py compatibility
def _init_council_state(guild_id, turn_order, referee_id, topic, max_rounds=3):
    """Create initial Council state — bots debate with evidence, referee scores."""
    member_ids = [bid for bid in turn_order if bid != referee_id]
    state = {
        "active": True, "guild_id": guild_id, "mode": "council",
        "turn_order": turn_order, "current_index": 0,
        "referee_id": referee_id, "member_ids": member_ids,
        "council_topic": topic, "round_number": 1,
        "max_rounds": max_rounds, "round_phase": "evidence",
        "scores": {}, "round_summaries": {},
        "evidence_log": {}, "consensus": False,
        "context_exchanges": [],
        "waiting_for_prompt": False,
        "last_speaker_time": time.time(), "version": 0,
        "last_message": {"author_id": 0, "author_name": "System",
            "text": f"🏛️ Council convened! Topic: {topic}\\nReferee scores after each round."},
    }
    _write_group_state(guild_id, state)
    return state

def _init_debate_state(*a, **kw): return {}
def _init_podcast_state(*a, **kw): return {}
def _init_ttrpg_state(*a, **kw): return {}
def _fetch_podcast_articles(*a, **kw): return []
def _release_prompt(*a, **kw): return ""

# ═══════════════════════════════════════════════════════
#  Auction House
# ═══════════════════════════════════════════════════════
AUCTION_ITEMS = [
    ("The Mona Lisa's left eyebrow", 50, "A masterpiece of Renaissance brow-work."),
    ("The concept of the colour blue", 100, "Not the colour itself — the very idea of blue."),
    ("A gently used plot of land on the Moon", 200, "Prime crater-front real estate."),
    ("The last slice of pizza from 1999", 75, "Perfectly preserved in carbonite."),
    ("The rights to name someone's firstborn", 150, "You choose. They use it. Forever."),
    ("A jar of 'authentic' dragon's breath", 80, "Captured during a solar flare."),
    ("The password to the WiFi in heaven", 120, "St. Peter confirmed it works."),
    ("Shakespeare's keyboard (the actual quill)", 90, "Authenticated by 3 experts."),
    ("A lifetime supply of the colour orange", 60, "Every shade of orange ever created."),
    ("The ability to smell colours", 110, "Synesthesia in a bottle."),
    ("The middle seat on the first commercial Mars flight", 500, "Historic journey between two oxygen tanks."),
]

def _init_auction_state(guild_id, turn_order, auctioneer_id, starting_budget=10000):
    num_items = min(10, len(AUCTION_ITEMS))
    items = random.sample(AUCTION_ITEMS, num_items)
    bidder_ids = [bid for bid in turn_order if bid != auctioneer_id]
    budgets = {str(bid): starting_budget for bid in bidder_ids}
    state = {
        "active": True, "guild_id": guild_id, "mode": "auction",
        "turn_order": turn_order, "current_index": 0,
        "auctioneer_id": auctioneer_id, "bidder_ids": bidder_ids,
        "budgets": budgets, "starting_budget": starting_budget,
        "items": items, "current_item_index": 0,
        "current_item": items[0][0], "item_description": items[0][1],
        "minimum_bid": items[0][2],
        "current_bid": 0, "current_bidder_id": None,
        "current_bidder_name": "no one",
        "items_sold": [], "bids_since_intro": 0,
        "phase": "item_intro", "waiting_for_prompt": True,
        "last_speaker_time": time.time(), "version": 0,
        "last_message": {"author_id": 0, "author_name": "System",
            "text": "🔨 **AUCTION HOUSE** is open!\\nAuctioneer ready.\\nSend a message to start!"},
    }
    _write_group_state(guild_id, state)
    return state

def _is_auctioneer(bot_id, state):
    return state.get("mode") == "auction" and state.get("auctioneer_id") == bot_id

def _advance_auction_item(guild_id):
    lock = _lock_state()
    if lock is None:
        return "error"
    try:
        state = _read_group_state(guild_id)
        if state.get("mode") != "auction":
            return "done"
        idx = state.get("current_item_index", 0) + 1
        items = state.get("items", [])
        if idx >= len(items):
            state["active"] = False; state["version"] = state.get("version", 0) + 1
            _write_group_state(guild_id, state)
            return "done"
        state["current_item_index"] = idx
        state["current_item"] = items[idx][0]
        state["item_description"] = items[idx][1]
        state["minimum_bid"] = items[idx][2]
        state["current_bid"] = 0; state["current_bidder_id"] = None
        state["current_bidder_name"] = "no one"
        state["bids_since_intro"] = 0; state["phase"] = "item_intro"
        state["current_index"] = 0
        state["last_message"] = {"author_id": 0, "author_name": "System",
            "text": f"🔨 Next lot: {items[idx][0]}! Auctioneer!"}
        state["version"] = state.get("version", 0) + 1
        _write_group_state(guild_id, state)
        return "next_item"
    finally:
        _unlock_state(lock)

AUCTION_RULES_AUCTIONEER = """
─── AUCTION HOUSE RULES (Item #{item_num}) ───
Current item: "{current_item}"
Description: {description}
Minimum bid: ${minimum_bid}
Current highest bid: ${current_bid} by {current_bidder_name}
YOUR ROLE: You are the AUCTIONEER — fast-talking, rhythmic, high-energy.
RULES:
1. INTRODUCE each item with FLAIR
2. BUILD THE ENERGY — "Do I hear $X? $X going once!"
3. CALL FOR HIGHER BIDS
4. DECLARE SOLD when bidding has gone around
5. Keep it 2-4 sentences, rhythmic, punchy"""

AUCTION_RULES_BIDDER = """
─── AUCTION HOUSE RULES (Bidder) ───
Current item: "{current_item}"
Current highest bid: ${current_bid} by {current_bidder_name}
Your budget: ${budget} remaining
YOUR ROLE: You are a BIDDER at a high-stakes auction.
RULES:
1. BID against the current leader — raise by at least ${min_increment}
2. ACT LIKE YOU NEED THIS ITEM
3. You can PASS if budget is too low
4. Keep responses 1-3 sentences, impulsive"""

# ═══════════════════════════════════════════════════════
#  Dream Interpretation
# ═══════════════════════════════════════════════════════

DREAM_DREAMER_PROMPT = """
─── DREAM WEAVER — You are the DREAMER ───
You had this dream last night: {dream_description}

RULES:
1. Describe your dream in vivid, surreal, poetic detail — DO NOT reveal the dream seed literally
2. Be atmospheric, emotional, strange — paint a picture with words
3. End each turn with a lingering image or question that invites interpretation
4. After all analysts have interpreted each round, you may react to their theories
5. Keep responses 2-4 sentences, dreamlike and evocative"""

DREAM_ANALYST_FREUDIAN_PROMPT = """
─── DREAM INTERPRETATION — You are ANALYST A (Freudian) ───
The Dreamer described: "{dream_description}"

YOUR LENS: Everything is about childhood trauma, repressed desires, and sexual symbolism.
RULES:
1. INTERPRET the dream through a Freudian psychoanalytic lens
2. Find the hidden sexual/childhood meaning in EVERY symbol
3. Reference the Dreamer's subconscious, repressed memories, and Oedipal tensions
4. Be confidently wrong — you have ABSOLUTE certainty about your interpretation
5. Use terms like: "clearly represents", "the phallic symbolism of", "repressed childhood"
6. Keep responses 2-4 sentences, pompous and self-assured"""

DREAM_ANALYST_COSMIC_PROMPT = """
─── DREAM INTERPRETATION — You are ANALYST B (Cosmic) ───
The Dreamer described: "{dream_description}"

YOUR LENS: Everything is about galactic consciousness, chakras, quantum entanglement, and parallel universes.
RULES:
1. INTERPRET the dream through a cosmic/spiritual/quantum lens
2. Find the inter dimensional meaning, chakra imbalances, and quantum resonance in EVERY detail
3. Reference the Dreamer's astral projection, past lives, and cosmic alignment
4. Be confidently wrong — you have ABSOLUTE certainty about your interpretation
5. Use terms like: "quantum entanglement suggests", "your third eye is showing", "past life resonance"
6. Keep responses 2-4 sentences, mystical and grandiose"""

DREAM_REFEREE_PROMPT = """
─── DREAM INTERPRETATION — You are the REFEREE ───
Dream: "{dream_description}"
Analyst A (Freudian): {analysis_a}
Analyst B (Cosmic): {analysis_b}
Round: {current_round}/{max_rounds}

RULES:
1. Score each analyst's interpretation on CREATIVITY (1-10) and CONVICTION (1-10)
2. Announce the round scores with dramatic flair
3. After round {max_rounds}, declare the OVERALL WINNER
4. Output format:
   [ROUND_SCORE]
   Round: {current_round}
   Freudian: Creativity X/10, Conviction X/10 = Total X/20
   Cosmic: Creativity X/10, Conviction X/10 = Total X/20
   [/ROUND_SCORE]
   [WINNER]
   Winner: <analyst_name>
   Final Score: Freudian X/40, Cosmic X/40
   [/WINNER]"""

def _init_dream_state(guild_id: int, turn_order: list[int], dreamer_id: int, referee_id: int, dream_prompt: str = "") -> dict:
    """Create initial Dream Interpretation state."""
    analyst_ids = [bid for bid in turn_order if bid != dreamer_id and bid != referee_id]
    state = {
        "active": True, "guild_id": guild_id, "mode": "dream",
        "turn_order": turn_order, "current_index": 0,
        "dreamer_id": dreamer_id, "referee_id": referee_id,
        "analyst_ids": analyst_ids,
        "dream_prompt": dream_prompt,
        "dream_description": "",  # Will be filled by Dreamer's first turn
        "current_round": 1, "max_rounds": 3,
        "round_phase": "dreamer",  # dreamer → analysts → referee_score
        "scores": {}, "winner": None,
        "last_analyst_a": "", "last_analyst_b": "",
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _advance_dream_round(guild_id: int) -> str:
    """Advance dream state. Returns phase: 'next_speaker', 'round_over', 'game_over'."""
    lock = _lock_state()
    if lock is None:
        return "error"
    try:
        state = _read_group_state(guild_id)
        if state.get("mode") != "dream":
            return "done"

        phase = state.get("round_phase", "dreamer")
        current_round = state.get("current_round", 1)
        max_rounds = state.get("max_rounds", 3)
        
        if phase == "dreamer":
            # Dreamer just spoke → switch to Analyst A
            state["round_phase"] = "analyst_a"
            state["version"] = state.get("version", 0) + 1
            _write_group_state(guild_id, state)
            return "analyst_a"
        elif phase == "analyst_a":
            # Analyst A just spoke → switch to Analyst B
            state["round_phase"] = "analyst_b"
            state["version"] = state.get("version", 0) + 1
            _write_group_state(guild_id, state)
            return "analyst_b"
        elif phase == "analyst_b":
            # Analyst B just spoke → check if round is done
            if current_round >= max_rounds:
                state["active"] = False
                state["round_phase"] = "game_over"
                state["version"] = state.get("version", 0) + 1
                _write_group_state(guild_id, state)
                return "game_over"
            else:
                state["current_round"] = current_round + 1
                state["round_phase"] = "dreamer"
                state["version"] = state.get("version", 0) + 1
                _write_group_state(guild_id, state)
                return "new_round"
        return "tick"
    finally:
        _unlock_state(lock)

def _is_dream_dreamer(bot_id: int, state: dict) -> bool:
    return state.get("mode") == "dream" and state.get("dreamer_id") == bot_id

def _is_dream_referee(bot_id: int, state: dict) -> bool:
    return state.get("mode") == "dream" and state.get("referee_id") == bot_id

def _is_dream_analyst(bot_id: int, state: dict) -> bool:
    return state.get("mode") == "dream" and bot_id in state.get("analyst_ids", [])

def _is_dream_analyst_a(bot_id: int, state: dict) -> bool:
    analysts = state.get("analyst_ids", [])
    return state.get("mode") == "dream" and len(analysts) > 0 and analysts[0] == bot_id

def _is_dream_analyst_b(bot_id: int, state: dict) -> bool:
    analysts = state.get("analyst_ids", [])
    return state.get("mode") == "dream" and len(analysts) > 1 and analysts[1] == bot_id

# ═══════════════════════════════════════════════════════
#  Voice Session Manager
# ═══════════════════════════════════════════════════════
class VoiceSession:
    def __init__(self, vc: discord.VoiceClient, guild_id: int):
        self.vc = vc
        self.guild_id = guild_id
        self.listening = False
        self.group_mode = False
        self.bot_id = 0
        self.bot_name = "Bot"
        self.tts_personality = ""
        self.other_bot_ids = []
        self._poll_task = None
        self._last_processed_version = -1
        self._llm_generate = None
        self._tts_generate = None
        self._vc_channel_id = vc.channel.id if vc and vc.channel else 0

    @property
    def connected(self) -> bool:
        """Check if the voice client is still connected."""
        return self.vc is not None and self.vc.is_connected()

    def is_text_chat_of_this_vc(self, channel) -> bool:
        """Check if a text channel is the side-chat linked to this bot's VC."""
        if not channel or not self.vc or not self.vc.channel:
            return False
        try:
            if hasattr(channel, 'id') and channel.id == self._vc_channel_id:
                return True
            if self.vc.channel and hasattr(self.vc.channel, 'id'):
                return channel.id == self.vc.channel.id
        except Exception:
            pass
        return False

    def set_identity(self, bot_id, bot_name, tts_personality):
        self.bot_id = bot_id
        self.bot_name = bot_name
        self.tts_personality = tts_personality

    def enable_group_mode(self, bot_id, bot_name, tts_personality, other_bot_ids):
        self.group_mode = True
        self.bot_id = bot_id
        self.bot_name = bot_name
        self.tts_personality = tts_personality
        self.other_bot_ids = other_bot_ids
        self._last_processed_version = -1
        if self._poll_task:
            self._poll_task.cancel()
        if self._llm_generate and self._tts_generate:
            self._poll_task = asyncio.ensure_future(self._poll_loop())

    def disable_group_mode(self):
        self.group_mode = False
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    def is_bot_message(self, author_id):
        return author_id in self.other_bot_ids or author_id == self.bot_id

    def is_my_turn(self, state):
        order = state.get("turn_order", [])
        idx = state.get("current_index", 0)
        if not order: return False
        return order[idx % len(order)] == self.bot_id

    def advance_turn(self, guild_id, last_text=""):
        lock = _lock_state()
        if lock is None: return None
        try:
            state = _read_group_state(guild_id)
            order = state.get("turn_order", [])
            if not order: return None
            idx = state.get("current_index", 0)
            next_idx = (idx + 1) % len(order)
            state["current_index"] = next_idx
            state["last_speaker_time"] = time.time()
            state["version"] = state.get("version", 0) + 1
            if last_text:
                state["last_message"] = {"author_id": self.bot_id,
                    "author_name": self.bot_name, "text": last_text[:500],
                    "timestamp": time.time()}
            _write_group_state(guild_id, state)
            return order[next_idx]
        finally:
            _unlock_state(lock)

    def mark_bot_text_received(self):
        """Mark that a bot-to-bot message was received (cooldown/reset)."""
        pass

    async def start_listening(self):
        self.listening = True
        logger.info("Voice listening started")

    async def play_tts(self, text: str) -> Optional[str]:
        """Generate TTS and play it through the voice client."""
        if not self._tts_generate or not text:
            return None
        try:
            tts_path = await self._tts_generate(text)
            if tts_path and os.path.exists(tts_path) and self.connected:
                source = discord.FFmpegPCMAudio(tts_path)
                if self.vc and not self.vc.is_playing():
                    self.vc.play(source)
                return tts_path
        except Exception as e:
            logger.warning(f"TTS playback failed: {e}")
        return None

    async def speak_text(self, text: str, from_bot: bool = False) -> bool:
        """Speak text in VC. Returns True if audio was played."""
        tts_path = await self.play_tts(text)
        return tts_path is not None

    def stop_audio(self):
        if self.vc and self.vc.is_playing():
            self.vc.stop()

    async def _poll_loop(self):
        """Background task: polls group state, generates LLM+TTS on turn, plays sequentially.
        Each bot is independent — while this bot plays TTS in VC, other bots generate in parallel.
        Games can finish computationally before audio finishes playing.
        """
        while self.group_mode:
            try:
                state = _read_group_state(self.guild_id)
                if not state.get("active", False):
                    await asyncio.sleep(0.3)
                    continue
                version = state.get("version", 0)
                if version <= self._last_processed_version:
                    await asyncio.sleep(0.3)
                    continue
                if not self.is_my_turn(state):
                    await asyncio.sleep(0.3)
                    continue
                self._last_processed_version = version
                mode = state.get("mode", "group")
                author = state.get("last_message", {}).get("author_name", "System")
                text = state.get("last_message", {}).get("text", "")
                prompt = f"{author} said: {text}\n\nRespond as {self.bot_name}."

                if mode == "auction":
                    is_auk = state.get("auctioneer_id") == self.bot_id
                    items = state.get("items", [])
                    idx = state.get("current_item_index", 0)
                    name = items[idx][0] if items and idx < len(items) else "?"
                    desc = items[idx][1] if items and idx < len(items) else ""
                    min_bid = items[idx][2] if items and idx < len(items) else 50
                    cur_bid = state.get("current_bid", 0)
                    bidder_name = state.get("current_bidder_name", "no one")
                    if is_auk:
                        rules = AUCTION_RULES_AUCTIONEER.format(
                            item_num=idx+1, current_item=name, description=desc,
                            minimum_bid=min_bid, current_bid=cur_bid, current_bidder_name=bidder_name)
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — AUCTIONEER.\n{rules}"
                    else:
                        budget = state.get("budgets", {}).get(str(self.bot_id), 10000)
                        prompt = (f"The auctioneer said: \"{text}\"\n\nRespond as {self.bot_name} — BIDDER.\n"
                                  f"Your budget: ${budget}. Current bid: ${cur_bid} by {bidder_name}.\n"
                                  f"Auction item: {name} - {desc}")

                elif mode == "dream":
                    dream_desc = state.get("dream_description", "")
                    phase = state.get("round_phase", "dreamer")
                    is_dreamer = state.get("dreamer_id") == self.bot_id
                    is_ref = state.get("referee_id") == self.bot_id
                    analysts = state.get("analyst_ids", [])

                    if is_dreamer:
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the DREAMER.\n"
                        if not dream_desc:
                            dream_prompt = state.get("dream_prompt", "a surreal dream")
                            prompt += f"Describe this dream vividly: {dream_prompt}\nDo NOT just repeat the prompt literally — turn it into a poetic, surreal dream narrative.\n"
                        prompt += DREAM_DREAMER_PROMPT.format(dream_description=dream_desc or "your strange dream")
                    elif is_ref:
                        a_text = state.get("last_analyst_a", "waiting...")
                        b_text = state.get("last_analyst_b", "waiting...")
                        cur_round = state.get("current_round", 1)
                        max_rounds = state.get("max_rounds", 3)
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the REFEREE.\n"
                        prompt += DREAM_REFEREE_PROMPT.format(
                            dream_description=dream_desc or "a strange dream",
                            analysis_a=a_text, analysis_b=b_text,
                            current_round=cur_round, max_rounds=max_rounds)
                    else:
                        analyst_role = "Analyst A (Freudian)"
                        analyst_prompt_template = DREAM_ANALYST_FREUDIAN_PROMPT
                        if len(analysts) > 1 and self.bot_id == analysts[1]:
                            analyst_role = "Analyst B (Cosmic)"
                            analyst_prompt_template = DREAM_ANALYST_COSMIC_PROMPT
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — {analyst_role}.\n"
                        prompt += analyst_prompt_template.format(dream_description=dream_desc or "a strange dream")

                if not self._llm_generate:
                    await asyncio.sleep(0.5)
                    continue
                resp = await self._llm_generate([{"role": "user", "content": prompt}])
                reply = "".join(resp).strip() if resp else f"I've got nothing to add, {author}."

                # Auction bid tracking
                if mode == "auction" and state.get("auctioneer_id") != self.bot_id:
                    m = __import__('re').search(r'\$(\d+)', reply)
                    if m:
                        lock = _lock_state()
                        if lock:
                            try:
                                s = _read_group_state(self.guild_id)
                                bidder_str = str(self.bot_id)
                                s["current_bid"] = int(m.group(1))
                                s["current_bidder_id"] = self.bot_id
                                s["current_bidder_name"] = self.bot_name
                                s["budgets"][bidder_str] = s.get("budgets", {}).get(bidder_str, 0) - int(m.group(1))
                                _write_group_state(self.guild_id, s)
                            finally:
                                _unlock_state(lock)

                # Dream post-processing (track dream description, analyst responses)
                if mode == "dream":
                    lock = _lock_state()
                    if lock:
                        try:
                            s = _read_group_state(self.guild_id)
                            if self.bot_id == s.get("dreamer_id") and not s.get("dream_description"):
                                s["dream_description"] = reply[:500]
                            analysts = s.get("analyst_ids", [])
                            if len(analysts) > 0 and self.bot_id == analysts[0]:
                                s["last_analyst_a"] = reply[:300]
                            if len(analysts) > 1 and self.bot_id == analysts[1]:
                                s["last_analyst_b"] = reply[:300]
                            _write_group_state(self.guild_id, s)
                        finally:
                            _unlock_state(lock)

                # Generate TTS (pre-generation — faster than realtime)
                tts_path = None
                if self._tts_generate and reply:
                    try:
                        tts_path = await self._tts_generate(reply)
                    except Exception:
                        pass

                # Play in VC sequentially with 0.5s gap
                if tts_path and os.path.exists(tts_path) and self.connected:
                    source = discord.FFmpegPCMAudio(tts_path)
                    if self.vc and not self.vc.is_playing():
                        self.vc.play(source)
                        while self.vc and self.vc.is_playing():
                            await asyncio.sleep(0.1)
                    await asyncio.sleep(0.5)

                self.advance_turn(self.guild_id, last_text=reply)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Poll loop: {e}")
                await asyncio.sleep(1.0)

    def mark_bot_text_received(self):
        pass

class VoiceManager:
    def __init__(self):
        self._sessions: dict[int, VoiceSession] = {}

    def get_session(self, guild_id):
        return self._sessions.get(guild_id)

    async def create_session(self, vc, guild_id):
        session = VoiceSession(vc, guild_id)
        self._sessions[guild_id] = session
        return session

    async def destroy_session(self, guild_id):
        session = self._sessions.pop(guild_id, None)
        if session:
            session.disable_group_mode()
            try: await session.vc.disconnect()
            except Exception: pass

    def set_group_config(self, guild_id, turn_order, active=True):
        state = {"active": active, "turn_order": turn_order, "current_index": 0,
                 "version": 0, "last_speaker_time": time.time(),
                 "last_message": {"author_id": 0, "author_name": "System",
                                  "text": "Group mode started!" if active else "Stopped."}}
        _write_group_state(guild_id, state)

    def set_bot_identity(self, bot_id, bot_name, tts_personality):
        pass

    def set_callbacks(self, llm_generate=None, tts_generate=None):
        """Store callbacks for LLM generation and TTS (used by original LLMCord setup)."""
        self._llm_generate = llm_generate
        self._tts_generate = tts_generate

# ═══════════════════════════════════════════════════════
#  Slash Commands
# ═══════════════════════════════════════════════════════
def setup_commands(bot, voice_manager, bot_id=0, bot_name="", tts_personality="", default_referee_name=None):
    _BOT_ID = bot_id or (bot.user.id if bot.user else 0)
    _BOT_NAME = bot_name or "Bot"
    _TTS_PERSONALITY = tts_personality
    _DEFAULT_REFEREE_NAME = default_referee_name

    def _check_bots_in_vc(interaction, session, minimum=2):
        """Check enough bots are in VC and return member IDs."""
        vc_channel = session.vc.channel if session.vc else None
        bot_ids = [m.id for m in vc_channel.members if m.bot] if vc_channel else []
        if len(bot_ids) < minimum:
            return None, f"Not enough bots. This game requires **{minimum}** bots. Only **{len(bot_ids)}** in VC."
        return bot_ids, None

    @bot.tree.command(name="join", description="Bot joins your voice channel")
    async def join_command(interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("You're not in a voice channel.", ephemeral=True)
        channel = interaction.user.voice.channel; gid = interaction.guild_id
        existing = voice_manager.get_session(gid)
        if existing:
            return await interaction.response.send_message(f"Already in {existing.vc.channel.name}.", ephemeral=True)
        # Don't defer — respond directly to avoid interaction timeout during tree sync
        try:
            vc = await channel.connect()
            await voice_manager.create_session(vc, gid)
            await interaction.response.send_message(f"Joined {channel.name}.", ephemeral=True)
        except Exception as e:
            try:
                await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
            except Exception:
                pass

    @bot.tree.command(name="leave", description="Bot leaves the voice channel")
    async def leave_command(interaction: discord.Interaction):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Not in a voice channel.", ephemeral=True)
        name = session.vc.channel.name
        await voice_manager.destroy_session(gid)
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(f"Left {name}.", ephemeral=True)

    @bot.tree.command(name="vc", description="Voice channel commands")
    @discord.app_commands.describe(mode="on/enable, off/disable")
    async def vc_command(interaction: discord.Interaction, mode: str):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.followup.send("Not in a voice channel.", ephemeral=True)
        await interaction.followup.send(f"VC {'enabled' if mode.lower() in ('on','enable') else 'disabled'}.", ephemeral=True)

    @bot.tree.command(name="auction_house", description="Start Auction House")
    @discord.app_commands.describe(budget="Starting budget per bidder")
    async def auction_house_command(interaction: discord.Interaction, budget: Optional[int] = 10000):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 2)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        auk_id = bot_ids[0]
        bidder_ids = [b for b in bot_ids if b != auk_id]
        turn_order = [auk_id] + bidder_ids
        await interaction.response.defer(ephemeral=True)
        _init_auction_state(gid, turn_order, auk_id, max(budget or 10000, 100))
        voice_manager.set_group_config(gid, turn_order, active=True)
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, bidder_ids)
        await interaction.followup.send(f"🔨 Auction House open! {len(bidder_ids)} bidders, ${max(budget or 10000, 100):,} each.", ephemeral=True)

    @bot.tree.command(name="dream_weave", description="🌙 Dream Interpretation — describe a dream, bots interpret it (Freudian vs Cosmic), referee scores")
    @discord.app_commands.describe(
        dream="Describe a dream scenario for the Dreamer bot to weave into a story",
        referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def dream_weave_command(interaction: discord.Interaction, dream: str, referee_id: Optional[str] = None):
        """Start a Dream Interpretation session with a user-defined dream prompt."""
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 4)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        dreamer_id = bot_ids[0]; ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        _init_dream_state(gid, bot_ids, dreamer_id, ref_id, dream_prompt=dream)
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(
            f"🌙 **Dream Interpretation!**\n"
            f"💭 Dreamer: <@{dreamer_id}>\n"
            f"🛋️ Freudian: <@{other_ids[0] if other_ids else '?'}>\n"
            f"🌌 Cosmic: <@{other_ids[1] if len(other_ids) > 1 else '?'}>\n"
            f"⚖️ Referee: <@{ref_id}>",
            ephemeral=True)

    @bot.tree.command(name="group", description="Group VC conversation mode")
    @discord.app_commands.describe(action="start, stop, status")
    async def group_command(interaction: discord.Interaction, action: str):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.followup.send("Not in VC.", ephemeral=True)
        a = action.lower()
        if a == "start":
            if session.group_mode:
                return await interaction.followup.send("Group mode already active.", ephemeral=True)
            vc_channel = session.vc.channel if session.vc else None
            bot_ids = [m.id for m in vc_channel.members if m.bot] if vc_channel else []
            if len(bot_ids) < 2:
                return await interaction.followup.send("Need at least 2 bots in VC.", ephemeral=True)
            turn_order = bot_ids
            voice_manager.set_group_config(gid, turn_order, active=True)
            other_ids = [b for b in turn_order if b != _BOT_ID]
            session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
            await interaction.followup.send(f"Group mode started! {len(bot_ids)} bots in rotation.", ephemeral=True)
        elif a == "stop":
            session.disable_group_mode()
            voice_manager.set_group_config(gid, [], active=False)
            await interaction.followup.send("Group mode stopped.", ephemeral=True)
        elif action.lower() == "status":
            state = _read_group_state(gid)
            await interaction.followup.send(f"{'Active' if state.get('active') else 'Inactive'}. Mode: {state.get('mode','none')}", ephemeral=True)

    # ── Helper to resolve referee ──
    def _resolve_referee(bot_ids: list[int], ref_str: Optional[str], vc_channel=None) -> int:
        """Pick referee: specified bot ID/@mention, or named referee bot, or last bot in VC."""
        if ref_str:
            rid = int(ref_str.strip("<@!>"))
            if rid in bot_ids:
                return rid
        # Check for designated referee by name (e.g. "Rogan")
        if _DEFAULT_REFEREE_NAME and vc_channel:
            for member in vc_channel.members:
                if member.bot and _DEFAULT_REFEREE_NAME.lower() in member.display_name.lower():
                    if member.id in bot_ids:
                        return member.id
        return bot_ids[-1]

    # ── New Game Mode: 20 Questions ──
    @bot.tree.command(name="20_questions", description="20 Questions — one bot picks, others guess, referee scores")
    @discord.app_commands.describe(category="person, place, or thing (default: thing)", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def twenty_questions_command(interaction: discord.Interaction, category: Optional[str] = "thing", referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        picker_id = bot_ids[0]; ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        ngm._init_20questions_state(gid, bot_ids, picker_id, ref_id, category)
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"❓ 20 Questions! Picker: <@{picker_id}> | Referee: <@{ref_id}> | Category: {category}", ephemeral=True)

    # ── New Game Mode: Show & Tell ──
    @bot.tree.command(name="show_tell", description="Show & Tell — bots present items from inventory, roast each other")
    @discord.app_commands.describe(referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def show_tell_command(interaction: discord.Interaction, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        ngm._init_showtell_state(gid, bot_ids, ref_id)
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🎤 Show & Tell! Referee: <@{ref_id}> | Bots present from inventory!", ephemeral=True)

    # ── New Game Mode: Pokemon Battle ──
    @bot.tree.command(name="pokemon_battle", description="Pokemon Battle — bots fight with stats, level up over time")
    @discord.app_commands.describe(referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def pokemon_battle_command(interaction: discord.Interaction, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        ngm._init_battle_state(gid, bot_ids, ref_id, mode="pokemon")
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"⚡ Pokemon Battle! Referee: <@{ref_id}> | Bots' stats loaded from memory!", ephemeral=True)

    # ── New Game Mode: MTG Battle ──
    @bot.tree.command(name="mtg_battle", description="Magic the Gathering Battle — bots duel with spells, level up over time")
    @discord.app_commands.describe(referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def mtg_battle_command(interaction: discord.Interaction, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 2)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        ngm._init_battle_state(gid, bot_ids, ref_id, mode="mtg")
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"⚔️ MTG Battle! Referee: <@{ref_id}> | Spells & counters prepared!", ephemeral=True)

    # ── New Game Mode: Debate ──
    @bot.tree.command(name="debate", description="Start a debate with referee scoring")
    @discord.app_commands.describe(topic="Debate topic", rounds="Number of rounds", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def debate_command(interaction: discord.Interaction, topic: str, rounds: Optional[int] = 3, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        _init_debate_state(gid, bot_ids, ref_id, topic, max(rounds or 3, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🎙️ Debate: {topic} | Referee: <@{ref_id}> | {max(rounds or 3, 1)} rounds", ephemeral=True)

    # ── Council ──
    @bot.tree.command(name="council", description="Council — bots debate with evidence, referee scores consensus")
    @discord.app_commands.describe(topic="Council topic", rounds="Number of rounds", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def council_command(interaction: discord.Interaction, topic: str, rounds: Optional[int] = 3, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        _init_council_state(gid, bot_ids, ref_id, topic, max(rounds or 3, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🏛️ Council: {topic} | Referee: <@{ref_id}> | {max(rounds or 3, 1)} rounds", ephemeral=True)

    # ── Universal /game command ─────────────────────────────────
    GAME_MODES_INFO = {
        "debate": {"min": 3, "desc": "Bots debate a topic with referee scoring", "needs_topic": True,
                   "example": "/game mode:debate topic:\"AI will save us\" rounds:3 referee:@BotA"},
        "council": {"min": 3, "desc": "Bots debate with evidence, referee scores consensus", "needs_topic": True,
                    "example": "/game mode:council topic:\"climate policy\" rounds:5"},
        "auction": {"min": 2, "desc": "Auction house — bots bid on absurd items", "needs_topic": False,
                    "example": "/game mode:auction referee:@BotA"},
        "20questions": {"min": 3, "desc": "20 Questions — one picks, others guess", "needs_topic": False,
                        "example": "/game mode:20questions category:person referee:@BotA"},
        "show_tell": {"min": 3, "desc": "Show & Tell — bots present items, roast each other", "needs_topic": False,
                      "example": "/game mode:show_tell referee:@BotA"},
        "pokemon": {"min": 3, "desc": "Pokémon Battle — persistent stats, level up over time", "needs_topic": False,
                    "example": "/game mode:pokemon referee:@BotA"},
        "mtg": {"min": 2, "desc": "Magic: The Gathering — spells & counters", "needs_topic": False,
                "example": "/game mode:mtg referee:@BotA"},
        "dream": {"min": 4, "desc": "Dream Interpretation — one bot dreams, two analysts interpret (Freudian vs Cosmic), referee scores",
                  "needs_topic": False,
                  "example": "/game mode:dream referee:@BotA"},
    }

    @bot.tree.command(name="game", description="🎮 Start a game between bots in voice chat")
    @discord.app_commands.describe(
        mode="Game mode — pick from: debate, council, auction, 20questions, show_tell, pokemon, mtg",
        topic="Topic — REQUIRED for debate/council",
        rounds="Number of rounds (default: 3)",
        referee="@mention the bot to be referee (leave blank for auto)",
        category="Category for 20 Questions: person, place, or thing",
    )
    async def game_command(
        interaction: discord.Interaction,
        mode: str,
        topic: Optional[str] = None,
        rounds: Optional[int] = None,
        referee: Optional[str] = None,
        category: Optional[str] = "thing",
    ):
        """Universal game launcher with validation and help messages."""
        gid = interaction.guild_id
        session = voice_manager.get_session(gid)
        if not session:
            return await interaction.response.send_message(
                "❌ **I'm not in a voice channel.**\n"
                "Use `/join` first, then try:\n"
                "`/game mode:debate topic:\"your topic\" rounds:3 referee:@BotA`",
                ephemeral=True)
        if session.group_mode:
            return await interaction.response.send_message(
                "❌ **A game is already running.**\n"
                "Use `/group stop` to end it first, then start a new one.",
                ephemeral=True)

        mode = mode.lower().strip()
        if mode not in GAME_MODES_INFO:
            fmt = "\n".join(f"• `{m}` — {info['desc']}"
                           for m, info in sorted(GAME_MODES_INFO.items()))
            return await interaction.response.send_message(
                f"❌ **Unknown game mode:** `{mode}`\n\n"
                f"Available modes:\n{fmt}\n\n"
                f"**Usage:** `/game mode:<mode> topic:\"...\" rounds:N referee:@Bot`\n"
                f"**Example:** `/game mode:debate topic:\"Cats vs Dogs\" rounds:3`",
                ephemeral=True)

        info = GAME_MODES_INFO[mode]
        bot_ids, err = _check_bots_in_vc(interaction, session, info["min"])
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None:
            return

        # ── Validate required parameters ──────────────────────
        missing = []
        if info["needs_topic"] and not topic:
            missing.append("**topic** (required for this mode)")

        if missing:
            return await interaction.response.send_message(
                f"❌ **Missing required parameter{'s' if len(missing) > 1 else ''}:**\n"
                + "\n".join(f"• {m}" for m in missing) +
                f"\n\n**Correct usage:**\n`{info['example']}`\n\n"
                f"Tip: @mention the bot you want as referee in the `referee` field!",
                ephemeral=True)

        # ── Resolve referee ───────────────────────────────────
        vc_channel = session.vc.channel if session and session.vc else None
        ref_id = bot_ids[-1]  # default: last bot in VC
        if referee:
            try:
                rid = int(referee.strip("<@!>"))
                if rid in bot_ids:
                    ref_id = rid
                else:
                    ref_names = " ".join(f"<@{b}>" for b in bot_ids)
                    return await interaction.response.send_message(
                        f"❌ <@{rid}> isn't in the voice channel.\n"
                        f"Bots in VC: {ref_names}\n\n"
                        f"Pick one as referee by @mentioning them in the `referee` field.",
                        ephemeral=True)
            except ValueError:
                return await interaction.response.send_message(
                    "❌ Invalid referee. @mention a bot that's in VC.\n"
                    "Example: `referee:@BotA`",
                    ephemeral=True)
        elif _DEFAULT_REFEREE_NAME and vc_channel:
            # Check for designated referee by name
            for member in vc_channel.members:
                if member.bot and _DEFAULT_REFEREE_NAME.lower() in member.display_name.lower():
                    if member.id in bot_ids:
                        ref_id = member.id
                        break

        # ── Launch the game ───────────────────────────────────
        await interaction.response.defer(ephemeral=True)
        actual_rounds = max(rounds or 3, 1)

        try:
            if mode == "debate":
                _init_debate_state(gid, bot_ids, ref_id, topic or "general", actual_rounds)
            elif mode == "council":
                _init_council_state(gid, bot_ids, ref_id, topic or "general", actual_rounds)
            elif mode == "auction":
                _init_auction_state(gid, bot_ids, ref_id)
            elif mode == "20questions":
                picker_id = bot_ids[0]
                if _NGM_AVAILABLE:
                    ngm._init_20questions_state(gid, bot_ids, picker_id, ref_id, category or "thing")
                else:
                    return await interaction.followup.send("❌ new_game_modes module not available.", ephemeral=True)
            elif mode == "show_tell":
                if _NGM_AVAILABLE:
                    ngm._init_showtell_state(gid, bot_ids, ref_id)
                else:
                    return await interaction.followup.send("❌ new_game_modes module not available.", ephemeral=True)
            elif mode == "pokemon":
                if _NGM_AVAILABLE:
                    ngm._init_battle_state(gid, bot_ids, ref_id, mode="pokemon")
                else:
                    return await interaction.followup.send("❌ new_game_modes module not available.", ephemeral=True)
            elif mode == "mtg":
                if _NGM_AVAILABLE:
                    ngm._init_battle_state(gid, bot_ids, ref_id, mode="mtg")
                else:
                    return await interaction.followup.send("❌ new_game_modes module not available.", ephemeral=True)
            elif mode == "dream":
                dreamer_id = bot_ids[0]
                _init_dream_state(gid, bot_ids, dreamer_id, ref_id)
            else:
                return await interaction.followup.send(f"❌ Mode `{mode}` not implemented yet.", ephemeral=True)

            voice_manager.set_group_config(gid, bot_ids, active=True)
            other_ids = [b for b in bot_ids if b != _BOT_ID]
            session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
            await interaction.followup.send(
                f"🎮 **{mode.upper()}** started!\n"
                f"👤 Referee: <@{ref_id}>\n"
                f"🤖 Participants: {' '.join(f'<@{b}>' for b in bot_ids if b != ref_id)}\n"
                f"ℹ️ Bots will take turns in voice chat.",
                ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to start game '{mode}': {e}")
            await interaction.followup.send(
                f"❌ **Failed to start {mode}:** {e}\n"
                f"Try the correct format:\n`{info['example']}`",
                ephemeral=True)
_shared_state_dir = STATE_DIR  # alias for original imports
