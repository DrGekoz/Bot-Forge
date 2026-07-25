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

    async def start_listening(self):
        self.listening = True
        logger.info("Voice listening started")

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
def setup_commands(bot, voice_manager, bot_id=0, bot_name="", tts_personality=""):
    _BOT_ID = bot_id or (bot.user.id if bot.user else 0)
    _BOT_NAME = bot_name or "Bot"
    _TTS_PERSONALITY = tts_personality

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
        await interaction.response.defer(ephemeral=True)
        try:
            vc = await channel.connect()
            await voice_manager.create_session(vc, gid)
            await interaction.followup.send(f"Joined {channel.name}.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)

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

    @bot.tree.command(name="group", description="Group VC conversation mode")
    @discord.app_commands.describe(action="start, stop, status")
    async def group_command(interaction: discord.Interaction, action: str):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.followup.send("Not in VC.", ephemeral=True)
        if action.lower() == "stop":
            session.disable_group_mode()
            voice_manager.set_group_config(gid, [], active=False)
            await interaction.followup.send("Group mode stopped.", ephemeral=True)
        elif action.lower() == "status":
            state = _read_group_state(gid)
            await interaction.followup.send(f"{'Active' if state.get('active') else 'Inactive'}. Mode: {state.get('mode','none')}", ephemeral=True)

    # ── New Game Mode: 20 Questions ──
    @bot.tree.command(name="20_questions", description="20 Questions — one bot picks, others guess, referee scores")
    @discord.app_commands.describe(category="person, place, or thing (default: thing)")
    async def twenty_questions_command(interaction: discord.Interaction, category: Optional[str] = "thing"):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        picker_id = bot_ids[0]; ref_id = bot_ids[-1]
        await interaction.response.defer(ephemeral=True)
        ngm._init_20questions_state(gid, bot_ids, picker_id, ref_id, category)
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"❓ 20 Questions! Picker: <@{picker_id}> | Referee: <@{ref_id}> | Category: {category}", ephemeral=True)

    # ── New Game Mode: Show & Tell ──
    @bot.tree.command(name="show_tell", description="Show & Tell — bots present items from inventory, roast each other")
    async def show_tell_command(interaction: discord.Interaction):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        ref_id = bot_ids[-1]
        await interaction.response.defer(ephemeral=True)
        ngm._init_showtell_state(gid, bot_ids, ref_id)
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🎤 Show & Tell! Referee: <@{ref_id}> | Bots present from inventory!", ephemeral=True)

    # ── New Game Mode: Pokemon Battle ──
    @bot.tree.command(name="pokemon_battle", description="Pokemon Battle — bots fight with stats, level up over time")
    async def pokemon_battle_command(interaction: discord.Interaction):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        ref_id = bot_ids[-1]
        await interaction.response.defer(ephemeral=True)
        ngm._init_battle_state(gid, bot_ids, ref_id, mode="pokemon")
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"⚡ Pokemon Battle! Referee: <@{ref_id}> | Bots' stats loaded from memory!", ephemeral=True)

    # ── New Game Mode: MTG Battle ──
    @bot.tree.command(name="mtg_battle", description="Magic the Gathering Battle — bots duel with spells, level up over time")
    async def mtg_battle_command(interaction: discord.Interaction):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)

        bot_ids, err = _check_bots_in_vc(interaction, session, 2)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return

        ref_id = bot_ids[-1]
        await interaction.response.defer(ephemeral=True)
        ngm._init_battle_state(gid, bot_ids, ref_id, mode="mtg")
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"⚔️ MTG Battle! Referee: <@{ref_id}> | Spells & counters prepared!", ephemeral=True)

    # ── New Game Mode: Debate ──
    @bot.tree.command(name="debate", description="Start a debate with referee scoring")
    @discord.app_commands.describe(topic="Debate topic", rounds="Number of rounds")
    async def debate_command(interaction: discord.Interaction, topic: str, rounds: Optional[int] = 3):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = bot_ids[-1]
        await interaction.response.defer(ephemeral=True)
        _init_debate_state(gid, bot_ids, ref_id, topic, max(rounds or 3, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🎙️ Debate: {topic} | Referee: <@{ref_id}> | {max(rounds or 3, 1)} rounds", ephemeral=True)

    # ── Council ──
    @bot.tree.command(name="council", description="Council — bots debate with evidence, referee scores consensus")
    @discord.app_commands.describe(topic="Council topic", rounds="Number of rounds")
    async def council_command(interaction: discord.Interaction, topic: str, rounds: Optional[int] = 3):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = bot_ids[-1]
        await interaction.response.defer(ephemeral=True)
        _init_council_state(gid, bot_ids, ref_id, topic, max(rounds or 3, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🏛️ Council: {topic} | Referee: <@{ref_id}> | {max(rounds or 3, 1)} rounds", ephemeral=True)

# ═══════════════════════════════════════════════════════
#  Legacy compatibility stubs for original LLMCord
# ═══════════════════════════════════════════════════════
_shared_state_dir = STATE_DIR  # alias for original imports
