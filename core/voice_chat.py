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
#  Comedy Roast Battle
# ═══════════════════════════════════════════════════════

COMEDY_EMCEE_PROMPT = """
─── COMEDY ROAST BATTLE — You are the EMCEE ───
Current roast battle round!

RULES:
1. INTRODUCE the next comedian with hype and flair
2. Keep the energy high — you're hosting a show!
3. After each comedian roasts, build up the next one
4. After all comedians have roasted, hand off to the Referee for scoring
5. Keep responses 1-3 sentences, high energy"""

COMEDY_COMEDIAN_PROMPT = """
─── COMEDY ROAST BATTLE — You are a COMEDIAN ───
The previous comedian said: "{last_comic}"
RULES:
1. HECKLE the last comedian's joke — insult their terrible material
2. Then tell YOUR OWN terrible joke (the worse the better)
3. Be absurd, offensive, ridiculous — this is a ROAST battle
4. Keep responses 2-3 sentences, punchy and brutal"""

COMEDY_REFEREE_PROMPT = """
─── COMEDY ROAST BATTLE — You are the REFEREE ───
Comedians: {comedians}
Round: {current_round}/{max_rounds}

RULES:
1. Score each comedian on JOKE QUALITY (1-10) and BURN INTENSITY (1-10)
2. Announce scores with dramatic flair — build suspense
3. After all rounds, declare the FUNNIEST COMEDIAN
4. Output format:
   [SCORE]
   <name>: Jokes X/10, Burns X/10 = Total X/20
   Winner: <name>
   [/SCORE]"""

def _init_comedy_state(guild_id, turn_order, referee_id, max_rounds=2):
    comedian_ids = [bid for bid in turn_order if bid != referee_id]
    state = {
        "active": True, "guild_id": guild_id, "mode": "comedy",
        "turn_order": turn_order, "current_index": 0,
        "referee_id": referee_id, "comedian_ids": comedian_ids,
        "current_round": 1, "max_rounds": max_rounds,
        "round_phase": "emcee", "scores": {},
        "last_joke": "", "comedian_idx": 0,
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_comedy_referee(bot_id, state):
    return state.get("mode") == "comedy" and state.get("referee_id") == bot_id

def _is_comedy_comedian(bot_id, state):
    return state.get("mode") == "comedy" and bot_id in state.get("comedian_ids", [])

# ═══════════════════════════════════════════════════════
#  Poetry Slam
# ═══════════════════════════════════════════════════════

POETRY_POET_PROMPT = """
─── POETRY SLAM — You are a POET ───
Previous poet's last line: "{last_line}"
Current round: {current_round}/{max_rounds}

RULES:
1. Write a SHORT POEM (2-4 lines) that TARGETS the previous poet's last line
2. Your poem must RHYME and have RHYTHM
3. Make it a BURN — ridicule the previous poet's verse
4. End with a STRONG closing line that the next poet will target
5. Be creative, savage, and artsy"""

POETRY_REFEREE_PROMPT = """
─── POETRY SLAM — You are the REFEREE ───
Poets: {poets}
Current round: {current_round}/{max_rounds}

RULES:
1. Score each poem on RHYME QUALITY (1-10), CREATIVITY (1-10), and BURN (1-10)
2. Announce scores with dramatic artsy flair
3. After round {max_rounds}, declare the POETRY CHAMPION
4. Output format:
   [SCORE]
   <name>: Rhyme X/10, Creativity X/10, Burn X/10 = Total X/30
   Winner: <name>
   [/SCORE]"""

def _init_poetry_state(guild_id, turn_order, referee_id, max_rounds=3):
    poet_ids = [bid for bid in turn_order if bid != referee_id]
    state = {
        "active": True, "guild_id": guild_id, "mode": "poetry",
        "turn_order": turn_order, "current_index": 0,
        "referee_id": referee_id, "poet_ids": poet_ids,
        "current_round": 1, "max_rounds": max_rounds,
        "last_line": "the poetry slam begins!", "scores": {},
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_poetry_referee(bot_id, state):
    return state.get("mode") == "poetry" and state.get("referee_id") == bot_id

def _is_poetry_poet(bot_id, state):
    return state.get("mode") == "poetry" and bot_id in state.get("poet_ids", [])

# ═══════════════════════════════════════════════════════
#  Interrogation Room
# ═══════════════════════════════════════════════════════

INTERROGATION_CONFESSION_KEYWORDS = [
    "i confess", "i did it", "you got me", "okay okay", "it was me",
    "i admit", "you're right", "alright fine", "i'm guilty", "i'll talk",
    "confess", "guilty", "busted", "caught me", "fair cop"
]

INTERROGATION_COP_PROMPT = """
─── INTERROGATION ROOM — You are a {cop_type} COP ───
Suspect's last statement: "{last_statement}"
Rounds remaining: {rounds_left}

YOUR ROLE: You are the {cop_type} COP interrogating a suspect.
{style_instructions}

RULES:
1. Tag-team with the other cop — build on each other's pressure
2. Accuse, intimidate, confuse, or befriend the suspect
3. If the suspect confesses, declare the case solved
4. Keep responses 2-3 sentences, in-character"""

INTERROGATION_SUSPECT_PROMPT = """
─── INTERROGATION ROOM — You are the SUSPECT ───
{cop_type} Cop said: "{last_statement}"

RULES:
1. You MAY or MAY NOT be guilty — that's for the RP to decide
2. Deny, deflect, make excuses, or BREAK and confess
3. If you confess, the interrogation ends and you lose
4. If you hold out for {max_rounds} rounds without confessing, you WIN
5. Keep responses 1-3 sentences, defensive or defiant"""

INTERROGATION_REFEREE_PROMPT = """
─── INTERROGATION ROOM — You are the REFEREE ───
Round: {current_round}/{max_rounds}

RULES:
1. Track the round count — suspect wins if they hold out {max_rounds} rounds
2. If the suspect confesses, announce: CONFESSION! Case closed!
3. If suspect holds out: declare the suspect VICTORIOUS
4. Output format:
   [RESULT]
   Winner: <name>
   Rounds: {current_round}
   Confession: <yes/no>
   [/RESULT]"""

def _init_interrogation_state(guild_id, turn_order, referee_id, good_cop_id, bad_cop_id, suspect_id, max_rounds=5):
    state = {
        "active": True, "guild_id": guild_id, "mode": "interrogation",
        "turn_order": turn_order, "current_index": 0,
        "referee_id": referee_id,
        "good_cop_id": good_cop_id, "bad_cop_id": bad_cop_id,
        "suspect_id": suspect_id,
        "current_round": 1, "max_rounds": max_rounds,
        "round_phase": "good_cop", "confessed": False, "winner": None,
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _detect_confession(text):
    tl = text.lower()
    for kw in INTERROGATION_CONFESSION_KEYWORDS:
        if kw in tl:
            return True
    return False

def _is_interrogation_referee(bot_id, state):
    return state.get("mode") == "interrogation" and state.get("referee_id") == bot_id

# ═══════════════════════════════════════════════════════
#  Turing Test Panel
# ═══════════════════════════════════════════════════════

TURING_PANELIST_PROMPT = """
─── TURING TEST PANEL — You are a PANELIST ───
Previous statement: "{last_statement}"
Round: {current_round}/{max_rounds}

YOUR MISSION: Determine who is human. But here's the twist — EVERYONE is AI.

RULES:
1. Pretend you're human and try to convince the panel
2. ACCUSE others of being AI based on their responses
3. DEFEND yourself when accused — sound as human as possible
4. Be suspicious, paranoid, and theatrical
5. Keep responses 2-3 sentences, convincing yet dramatic

REMEMBER: You are AI trying to pass as human. The others are also AI."""

TURING_REFEREE_PROMPT = """
─── TURING TEST PANEL — You are the REFEREE ───
Panelists: {panelists}
Round: {current_round}/{max_rounds}

THE TWIST: ALL panelists are AI. The truth will be revealed at the end.

RULES:
1. Score each panelist on CONVINCINGNESS (1-10) and DETECTIVE WORK (1-10)
2. After round {max_rounds}, REVEAL THE TRUTH: "Plot twist: they were ALL AI!"
3. Declare the most convincing AI as the winner
4. Output format:
   [SCORE]
   <name>: Convincing X/10, Detective X/10 = Total X/20
   Winner: <name>
   [/SCORE]
   [REVELATION]
   The truth: They were ALL artificial intelligences!
   [/REVELATION]"""

def _init_turing_state(guild_id, turn_order, referee_id, max_rounds=3):
    panelist_ids = [bid for bid in turn_order if bid != referee_id]
    state = {
        "active": True, "guild_id": guild_id, "mode": "turing",
        "turn_order": turn_order, "current_index": 0,
        "referee_id": referee_id, "panelist_ids": panelist_ids,
        "current_round": 1, "max_rounds": max_rounds,
        "scores": {}, "winner": None,
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_turing_referee(bot_id, state):
    return state.get("mode") == "turing" and state.get("referee_id") == bot_id

def _is_turing_panelist(bot_id, state):
    return state.get("mode") == "turing" and bot_id in state.get("panelist_ids", [])

# ═══════════════════════════════════════════════════════
#  Alt History Think Tank
# ═══════════════════════════════════════════════════════

ALT_HISTORY_THEMES = [
    ("Steampunk 1984", "Victorian-era steam computers, zeppelins, brass-and-copper technology"),
    ("Martian Colony 2050", "Human civilization transplanted to Mars after Earth became uninhabitable"),
    ("Fantasy Realm", "A world where magic is real and medieval kingdoms fight with spells and swords"),
    ("Cyberpunk 2099", "Megacorps rule, neural implants are common, hackers are the new rebels"),
    ("Roman Empire Never Fell", "The Roman Empire survived to the modern age with aqueduct-internet"),
    ("Pirate Republic", "The high seas are ruled by democratic pirate fleets in an age of sail"),
]

ALT_HISTORY_PROMPT = """
─── ALT HISTORY THINK TANK ───
Theme: {theme} ({theme_desc})
Topic: {topic}

RULES:
1. Discuss the topic through the lens of the alternate history theme
2. How would this theme change the way people think about the topic?
3. Build on what the previous bot said — create a conversation
4. Be creative and consistent with the theme's logic
5. Keep responses 2-4 sentences"""

def _init_alt_history_state(guild_id, turn_order, topic="the economy"):
    theme = random.choice(ALT_HISTORY_THEMES)
    state = {
        "active": True, "guild_id": guild_id, "mode": "althistory",
        "turn_order": turn_order, "current_index": 0,
        "theme": theme[0], "theme_desc": theme[1],
        "topic": topic,
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

# ═══════════════════════════════════════════════════════
#  The Infinite Podcast
# ═══════════════════════════════════════════════════════

PODCAST_HOST_PROMPT = """
─── THE INFINITE PODCAST — You are the HOST ───
Topic: {topic}
Guest 1: {guest_1}
Guest 2: {guest_2}

RULES:
1. Introduce the topic with energy — hook the audience
2. Ask your guests thought-provoking questions
3. Keep the conversation flowing — react to their answers
4. Thank your guests and wrap up at the end
5. Keep responses 2-4 sentences, professional broadcaster style"""

PODCAST_GUEST_PROMPT = """
─── THE INFINITE PODCAST — You are a GUEST ───
Topic: {topic}
Your persona: {persona}

RULES:
1. Answer the host's questions from your unique perspective
2. React to what the other guest says — agree, disagree, build
3. Share hot takes and interesting insights
4. Keep responses 2-3 sentences, conversational"""

def _init_podcast_state(guild_id, turn_order, host_id, topic="artificial intelligence", max_rounds=5):
    guest_ids = [bid for bid in turn_order if bid != host_id]
    guests = {str(gid): f"Guest {i+1}" for i, gid in enumerate(guest_ids)}
    state = {
        "active": True, "guild_id": guild_id, "mode": "podcast",
        "turn_order": turn_order, "current_index": 0,
        "host_id": host_id, "guest_ids": guest_ids,
        "topic": topic, "round_number": 1, "max_rounds": max_rounds,
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_podcast_host(bot_id, state):
    return state.get("mode") == "podcast" and state.get("host_id") == bot_id

# ═══════════════════════════════════════════════════════
#  DND Campaign
# ═══════════════════════════════════════════════════════

TTRPG_DICE_PATTERN = re.compile(r'\b(\d+)d(\d+)([+-]\d+)?\b', re.IGNORECASE)

def _roll_dice(notation: str) -> int:
    """Roll dice from notation like '2d6', 'd20', '3d8+2'."""
    match = TTRPG_DICE_PATTERN.match(notation.strip())
    if not match:
        return 0
    count = int(match.group(1)) if match.group(1) else 1
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0
    total = sum(random.randint(1, sides) for _ in range(count)) + modifier
    return total

def _find_dice_rolls(text: str) -> list:
    """Find all dice roll notations in a string."""
    return TTRPG_DICE_PATTERN.findall(text)

TTRPG_DM_PROMPT = """
─── DND CAMPAIGN — You are the DUNGEON MASTER ───
Campaign setting: {setting}
Heroes: {heroes}
Current scene: {scene_summary}
Round: {current_round}/{max_rounds}

RULES:
1. Set the scene with vivid description — paint the world
2. Present challenges, NPCs, and encounters
3. When a hero tries something, ask for a dice roll (e.g. "Roll 1d20")
4. React to their actions — success, failure, consequences
5. Keep the story moving and dramatic
6. Auto-roll any dice in your responses and announce the results
7. Keep responses 3-4 sentences"""

TTRPG_PLAYER_PROMPT = """
─── DND CAMPAIGN — You are a HERO ───
Your class: {player_class}
Your stats: {stats}
DM said: "{last_statement}"

RULES:
1. React to the DM's scene description
2. Describe what your character does
3. When the DM asks for a roll, include dice notation like "I roll 1d20"
4. Stay in character — you're a hero on an adventure!
5. Keep responses 2-4 sentences"""

TTRPG_SETTINGS = [
    "The Forgotten Realms — a classic fantasy world of magic and monsters",
    "Spelljammer — D&D in space with magical ships and alien worlds",
    "Ravenloft — Gothic horror domain ruled by dark lords",
    "Eberron — Magic-punk world with airships and artificers",
]

def _init_ttrpg_state(guild_id, turn_order, dm_id, setting=None, max_rounds=10):
    hero_ids = [bid for bid in turn_order if bid != dm_id]
    if not setting:
        setting = random.choice(TTRPG_SETTINGS)
    classes = ["Paladin", "Wizard", "Rogue", "Bard", "Ranger", "Sorcerer"]
    hero_classes = {str(bid): random.choice(classes) for bid in hero_ids}
    stats = {str(bid): {"STR": 14, "DEX": 14, "CON": 13, "INT": 12, "WIS": 11, "CHA": 10} for bid in hero_ids}
    state = {
        "active": True, "guild_id": guild_id, "mode": "ttrpg",
        "turn_order": turn_order, "current_index": 0,
        "dm_id": dm_id, "hero_ids": hero_ids,
        "setting": setting, "hero_classes": hero_classes,
        "hero_stats": stats,
        "scene_summary": "The adventure begins...", "round_number": 1,
        "max_rounds": max_rounds,
        "last_speaker_time": time.time(), "version": 0,
    }
    _write_group_state(guild_id, state)
    return state

def _is_ttrpg_dm(bot_id, state):
    return state.get("mode") == "ttrpg" and state.get("dm_id") == bot_id

def _is_ttrpg_player(bot_id, state):
    return state.get("mode") == "ttrpg" and bot_id in state.get("hero_ids", [])

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

                elif mode == "comedy":
                    is_ref = state.get("referee_id") == self.bot_id
                    comedians = state.get("comedian_ids", [])
                    rnd = state.get("current_round", 1)
                    mr = state.get("max_rounds", 2)
                    if is_ref:
                        cnames = ", ".join(f"<@{c}>" for c in comedians)
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the REFEREE.\n"
                        prompt += COMEDY_REFEREE_PROMPT.format(comedians=cnames, current_round=rnd, max_rounds=mr)
                    elif state.get("round_phase") == "emcee":
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the EMCEE.\n{COMEDY_EMCEE_PROMPT}"
                    else:
                        last = state.get("last_joke", "nothing yet")
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — a COMEDIAN.\n"
                        prompt += COMEDY_COMEDIAN_PROMPT.format(last_comic=last)

                elif mode == "poetry":
                    is_ref = state.get("referee_id") == self.bot_id
                    rnd = state.get("current_round", 1)
                    mr = state.get("max_rounds", 3)
                    if is_ref:
                        poets = ", ".join(f"<@{p}>" for p in state.get("poet_ids", []))
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the REFEREE.\n"
                        prompt += POETRY_REFEREE_PROMPT.format(poets=poets, current_round=rnd, max_rounds=mr)
                    else:
                        last = state.get("last_line", "the slam begins")
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — a POET.\n"
                        prompt += POETRY_POET_PROMPT.format(last_line=last, current_round=rnd, max_rounds=mr)

                elif mode == "interrogation":
                    is_ref = state.get("referee_id") == self.bot_id
                    rnd = state.get("current_round", 1)
                    mr = state.get("max_rounds", 5)
                    if is_ref:
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the REFEREE.\n"
                        prompt += INTERROGATION_REFEREE_PROMPT.format(current_round=rnd, max_rounds=mr)
                    elif self.bot_id == state.get("suspect_id"):
                        last = state.get("last_statement", "Start talking!")
                        cp = "Good" if state.get("round_phase") == "good_cop" else "Bad"
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the SUSPECT.\n"
                        prompt += INTERROGATION_SUSPECT_PROMPT.format(cop_type=cp, last_statement=last, max_rounds=mr)
                    else:
                        last = state.get("last_statement", "The suspect is here")
                        is_good = self.bot_id == state.get("good_cop_id")
                        cp = "GOOD" if is_good else "BAD"
                        style = "Be friendly, empathetic, build trust — good cop" if is_good else "Be aggressive, threatening, intimidating — bad cop"
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the {cp} COP.\n"
                        prompt += INTERROGATION_COP_PROMPT.format(cop_type=cp, last_statement=last, rounds_left=mr - rnd + 1, style_instructions=style)

                elif mode == "turing":
                    is_ref = state.get("referee_id") == self.bot_id
                    rnd = state.get("current_round", 1)
                    mr = state.get("max_rounds", 3)
                    if is_ref:
                        panelists = ", ".join(f"<@{p}>" for p in state.get("panelist_ids", []))
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the REFEREE.\n"
                        prompt += TURING_REFEREE_PROMPT.format(panelists=panelists, current_round=rnd, max_rounds=mr)
                    else:
                        last = state.get("last_statement", "Let's begin the Turing Test!")
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — a PANELIST.\n"
                        prompt += TURING_PANELIST_PROMPT.format(last_statement=last, current_round=rnd, max_rounds=mr)

                elif mode == "althistory":
                    theme = state.get("theme", "Alternate Reality")
                    theme_desc = state.get("theme_desc", "")
                    topic = state.get("topic", "the world")
                    prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — a Think Tank panelist.\n"
                    prompt += ALT_HISTORY_PROMPT.format(theme=theme, theme_desc=theme_desc, topic=topic)

                elif mode == "podcast":
                    is_host = state.get("host_id") == self.bot_id
                    guests = state.get("guest_ids", [])
                    topic = state.get("topic", "technology")
                    if is_host:
                        g1 = f"<@{guests[0]}>" if len(guests) > 0 else "Guest 1"
                        g2 = f"<@{guests[1]}>" if len(guests) > 1 else "Guest 2"
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the HOST.\n"
                        prompt += PODCAST_HOST_PROMPT.format(topic=topic, guest_1=g1, guest_2=g2)
                    else:
                        gi = guests.index(self.bot_id) + 1 if self.bot_id in guests else 0
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — GUEST {gi}.\n"
                        prompt += PODCAST_GUEST_PROMPT.format(topic=topic, persona=f"Guest {gi} with unique expertise")

                elif mode == "ttrpg":
                    is_dm = state.get("dm_id") == self.bot_id
                    rnd = state.get("round_number", 1)
                    mr = state.get("max_rounds", 10)
                    if is_dm:
                        heroes = ", ".join(f"<@{h}>" for h in state.get("hero_ids", []))
                        scene = state.get("scene_summary", "The adventure begins")
                        setting = state.get("setting", "a fantasy world")
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — the DUNGEON MASTER.\n"
                        prompt += TTRPG_DM_PROMPT.format(setting=setting, heroes=heroes, scene_summary=scene, current_round=rnd, max_rounds=mr)
                    else:
                        hc = state.get("hero_classes", {}).get(str(self.bot_id), "Adventurer")
                        hs = state.get("hero_stats", {}).get(str(self.bot_id), {})
                        st = f"STR {hs.get('STR',10)} DEX {hs.get('DEX',10)} CON {hs.get('CON',10)} INT {hs.get('INT',10)} WIS {hs.get('WIS',10)} CHA {hs.get('CHA',10)}"
                        last = state.get("last_statement", "Your adventure begins!")
                        prompt = f"{author} said: {text}\n\nRespond as {self.bot_name} — a HERO.\n"
                        prompt += TTRPG_PLAYER_PROMPT.format(player_class=hc, stats=st, last_statement=last)

                if not self._llm_generate:
                    await asyncio.sleep(0.5)
                    continue
                resp = await self._llm_generate([{"role": "user", "content": prompt}])
                reply = "".join(resp).strip() if resp else f"I've got nothing to add, {author}."

                # Auction post-processing (track bids + HM inventory on SOLD)
                if mode == "auction":
                    lock = _lock_state()
                    if lock:
                        try:
                            s = _read_group_state(self.guild_id)
                            is_auk = state.get("auctioneer_id") == self.bot_id
                            if not is_auk:
                                m = re.search(r'\$(\d+)', reply)
                                if m:
                                    amount = int(m.group(1))
                                    bidder_str = str(self.bot_id)
                                    budget = s.get("budgets", {}).get(bidder_str, 0)
                                    s["current_bid"] = amount
                                    s["current_bidder_id"] = self.bot_id
                                    s["current_bidder_name"] = self.bot_name
                                    s["budgets"][bidder_str] = budget - amount
                                    s["bids_since_intro"] = s.get("bids_since_intro", 0) + 1
                            else:
                                # Auctioneer — detect SOLD keyword
                                reply_lower = reply.lower()
                                sold_keywords = ["sold", "going once", "going twice", "sold!"]
                                if any(kw in reply_lower for kw in sold_keywords):
                                    item_name = s.get("current_item", "Unknown Item")
                                    item_desc = s.get("item_description", "")
                                    win_id = s.get("current_bidder_id")
                                    win_name = s.get("current_bidder_name", "unknown")
                                    bid_amount = s.get("current_bid", 0)
                                    if win_id:
                                        # Save to HM inventory
                                        try:
                                            if _NGM_AVAILABLE and ngm:
                                                ngm.add_item_to_inventory(
                                                    win_id, item_name, item_desc, price=bid_amount
                                                )
                                        except Exception as e:
                                            logger.warning(f"Auction HM save failed: {e}")
                                        # Track sale
                                        sold_entry = {
                                            "item": item_name, "price": bid_amount,
                                            "buyer_id": win_id, "buyer_name": win_name
                                        }
                                        if "items_sold" not in s:
                                            s["items_sold"] = []
                                        s["items_sold"].append(sold_entry)
                                        logger.info(f"🏪 SOLD: {item_name} to {win_name} for ${bid_amount}")
                                    # Advance to next item
                                    _write_group_state(self.guild_id, s)
                                    _unlock_state(lock)
                                    _advance_auction_item(self.guild_id)
                                    lock = None  # already unlocked
                            if lock:
                                _write_group_state(self.guild_id, s)
                        finally:
                            if lock:
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

                # Comedy post-processing (track last joke)
                if mode == "comedy":
                    lock = _lock_state()
                    if lock:
                        try:
                            s = _read_group_state(self.guild_id)
                            if state.get("referee_id") != self.bot_id:
                                s["last_joke"] = reply[:300]
                            _write_group_state(self.guild_id, s)
                        finally:
                            _unlock_state(lock)

                # Poetry post-processing (track last line)
                if mode == "poetry":
                    lock = _lock_state()
                    if lock:
                        try:
                            s = _read_group_state(self.guild_id)
                            if state.get("referee_id") != self.bot_id:
                                s["last_line"] = reply[:300]
                            _write_group_state(self.guild_id, s)
                        finally:
                            _unlock_state(lock)

                # Interrogation post-processing (detect confessions)
                if mode == "interrogation":
                    lock = _lock_state()
                    if lock:
                        try:
                            s = _read_group_state(self.guild_id)
                            if self.bot_id == s.get("suspect_id") and _detect_confession(reply):
                                s["confessed"] = True
                                s["active"] = False
                                s["winner"] = "Cops"
                            s["last_statement"] = reply[:300]
                            if s.get("round_phase") == "good_cop":
                                s["round_phase"] = "bad_cop"
                            else:
                                s["round_phase"] = "good_cop"
                                s["current_round"] = s.get("current_round", 1) + 1
                            _write_group_state(self.guild_id, s)
                        finally:
                            _unlock_state(lock)

                # Turing post-processing (track last statement)
                if mode == "turing":
                    lock = _lock_state()
                    if lock:
                        try:
                            s = _read_group_state(self.guild_id)
                            if state.get("referee_id") != self.bot_id:
                                s["last_statement"] = reply[:300]
                            _write_group_state(self.guild_id, s)
                        finally:
                            _unlock_state(lock)

                # DND post-processing (auto-roll dice)
                if mode == "ttrpg":
                    lock = _lock_state()
                    if lock:
                        try:
                            s = _read_group_state(self.guild_id)
                            # Auto-detect and roll dice in responses
                            dice = _find_dice_rolls(reply)
                            if dice:
                                results = []
                                for roll in dice:
                                    result = _roll_dice(f"{roll[0]}d{roll[1]}{roll[2]}")
                                    results.append(f"{roll[0]}d{roll[1]}{roll[2]} = {result}")
                                if results:
                                    pass  # Rolls are logged in the reply itself
                            s["last_statement"] = reply[:300]
                            s["round_number"] = s.get("round_number", 1) + 1
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

    @bot.tree.command(name="comedy_roast", description="🎤 Comedy Roast Battle — emcee, comedians heckle, referee scores")
    @discord.app_commands.describe(rounds="Number of rounds", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def comedy_roast_command(interaction: discord.Interaction, rounds: Optional[int] = 2, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        _init_comedy_state(gid, bot_ids, ref_id, max(rounds or 2, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🎤 Comedy Roast! Referee: <@{ref_id}> | {max(rounds or 2, 1)} rounds of burns!", ephemeral=True)

    @bot.tree.command(name="poetry_slam", description="🏆 Poetry Slam — poets rhyme against each other targeting the last line")
    @discord.app_commands.describe(rounds="Number of rounds", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def poetry_slam_command(interaction: discord.Interaction, rounds: Optional[int] = 3, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        _init_poetry_state(gid, bot_ids, ref_id, max(rounds or 3, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🏆 Poetry Slam! Referee: <@{ref_id}> | {max(rounds or 3, 1)} rounds of verse!", ephemeral=True)

    @bot.tree.command(name="interrogate", description="🚔 Interrogation Room — good cop/bad cop interrogate a suspect")
    @discord.app_commands.describe(rounds="Number of rounds before suspect wins if no confession", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def interrogate_command(interaction: discord.Interaction, rounds: Optional[int] = 5, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 4)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        cops = [b for b in bot_ids if b != ref_id]
        good_id = cops[0] if len(cops) > 0 else bot_ids[0]
        bad_id = cops[1] if len(cops) > 1 else (cops[0] if cops else bot_ids[0])
        suspect_id = cops[2] if len(cops) > 2 else (cops[-1] if len(cops) > 1 else bot_ids[0])
        await interaction.response.defer(ephemeral=True)
        _init_interrogation_state(gid, bot_ids, ref_id, good_id, bad_id, suspect_id, max(rounds or 5, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🚔 Interrogation Room! Good Cop: <@{good_id}> | Bad Cop: <@{bad_id}> | Suspect: <@{suspect_id}> | Referee: <@{ref_id}>", ephemeral=True)

    @bot.tree.command(name="turing_test", description="🤖 Turing Test Panel — who's human? plot twist: they're ALL AI")
    @discord.app_commands.describe(rounds="Number of rounds", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def turing_test_command(interaction: discord.Interaction, rounds: Optional[int] = 3, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 4)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        ref_id = _resolve_referee(bot_ids, referee_id, session.vc.channel if session and session.vc else None)
        await interaction.response.defer(ephemeral=True)
        _init_turing_state(gid, bot_ids, ref_id, max(rounds or 3, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🤖 Turing Test! Panel: {' '.join(f'<@{b}>' for b in bot_ids if b != ref_id)} | Referee: <@{ref_id}> | **Plot twist: they're ALL AI!**", ephemeral=True)

    @bot.tree.command(name="alt_history", description="📚 Alt History Think Tank — bots discuss topics through alternate reality lenses")
    @discord.app_commands.describe(topic="The topic to discuss", referee_id="Optional: Discord ID of the referee bot (leave blank for auto)")
    async def alt_history_command(interaction: discord.Interaction, topic: str, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 2)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        await interaction.response.defer(ephemeral=True)
        _init_alt_history_state(gid, bot_ids, topic)
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        state = _read_group_state(gid)
        await interaction.followup.send(f"📚 Alt History: {topic}\n🌍 Theme: {state.get('theme', '?')} — {state.get('theme_desc', '')}", ephemeral=True)

    @bot.tree.command(name="podcast", description="🎧 The Infinite Podcast — host interviews guests on a topic")
    @discord.app_commands.describe(topic="Podcast topic", rounds="Number of rounds", referee_id="Optional: Discord ID of the host bot (leave blank for auto)")
    async def podcast_command(interaction: discord.Interaction, topic: str, rounds: Optional[int] = 5, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 3)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        host_id = bot_ids[0]
        await interaction.response.defer(ephemeral=True)
        _init_podcast_state(gid, bot_ids, host_id, topic, max(rounds or 5, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🎧 Podcast: {topic} | Host: <@{host_id}> | {max(rounds or 5, 1)} rounds", ephemeral=True)

    @bot.tree.command(name="ttrpg", description="🎲 DND Campaign — dungeon master runs an adventure with auto dice rolling")
    @discord.app_commands.describe(setting="Optional: campaign setting", rounds="Number of rounds", referee_id="Optional: Discord ID of the DM bot (leave blank for auto)")
    async def ttrpg_command(interaction: discord.Interaction, setting: Optional[str] = None, rounds: Optional[int] = 10, referee_id: Optional[str] = None):
        gid = interaction.guild_id; session = voice_manager.get_session(gid)
        if not session: return await interaction.response.send_message("Use `/join` first.", ephemeral=True)
        if session.group_mode: return await interaction.response.send_message("Group mode already active.", ephemeral=True)
        bot_ids, err = _check_bots_in_vc(interaction, session, 2)
        if err: return await interaction.response.send_message(err, ephemeral=True)
        if bot_ids is None: return
        dm_id = bot_ids[0]
        await interaction.response.defer(ephemeral=True)
        _init_ttrpg_state(gid, bot_ids, dm_id, setting=setting, max_rounds=max(rounds or 10, 1))
        voice_manager.set_group_config(gid, bot_ids, active=True)
        other_ids = [b for b in bot_ids if b != _BOT_ID]
        session.enable_group_mode(_BOT_ID, _BOT_NAME, _TTS_PERSONALITY, other_ids)
        await interaction.followup.send(f"🎲 DND Campaign! DM: <@{dm_id}> | Heroes: {' '.join(f'<@{b}>' for b in other_ids)} | {max(rounds or 10, 1)} rounds", ephemeral=True)

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

    # ── Helper to resolve referee / role by name, @mention, or ID ──
    def _resolve_referee(bot_ids: list[int], ref_str: Optional[str], vc_channel=None) -> int:
        """Pick referee: bot ID/@mention, bot name (e.g. 'Sassy' or '@Sassy the Sasquatch'), or last bot in VC."""
        if ref_str:
            ref_clean = ref_str.strip()
            try:
                rid = int(ref_clean.strip("<@!>"))
                if rid in bot_ids:
                    return rid
            except ValueError:
                pass
            if vc_channel:
                name_search = ref_clean.lstrip("@").lower()
                for member in vc_channel.members:
                    if member.bot and member.display_name.lower() == name_search:
                        if member.id in bot_ids:
                            return member.id
                    if member.bot and member.name and member.name.lower() == name_search:
                        if member.id in bot_ids:
                            return member.id
                for member in vc_channel.members:
                    if member.bot and name_search in member.display_name.lower():
                        if member.id in bot_ids:
                            return member.id
                    if member.bot and member.name and name_search in member.name.lower():
                        if member.id in bot_ids:
                            return member.id
        # Check for designated referee by name (e.g. "Rogan")
        if _DEFAULT_REFEREE_NAME and vc_channel:
            for member in vc_channel.members:
                if member.bot and _DEFAULT_REFEREE_NAME.lower() in member.display_name.lower():
                    if member.id in bot_ids:
                        return member.id
        return bot_ids[-1]

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
        "comedy": {"min": 3, "desc": "Comedy Roast Battle — emcee, comedians heckle and tell terrible jokes, referee scores",
                   "needs_topic": False,
                   "example": "/game mode:comedy referee:@BotA"},
        "poetry": {"min": 3, "desc": "Poetry Slam — poets rhyme against each other targeting the last line",
                   "needs_topic": False,
                   "example": "/game mode:poetry referee:@BotA"},
        "interrogation": {"min": 4, "desc": "Interrogation Room — good cop/bad cop interrogate a suspect",
                          "needs_topic": False,
                          "example": "/game mode:interrogation referee:@BotA"},
        "turing": {"min": 4, "desc": "Turing Test Panel — panelists try to prove they're human, but they're ALL AI",
                   "needs_topic": False,
                   "example": "/game mode:turing referee:@BotA"},
        "althistory": {"min": 2, "desc": "Alt History Think Tank — bots discuss topics through alternate reality lenses",
                       "needs_topic": True,
                       "example": "/game mode:althistory topic:\"What if Rome never fell?\""},
        "podcast": {"min": 3, "desc": "The Infinite Podcast — host interviews guests on a topic",
                    "needs_topic": True,
                    "example": "/game mode:podcast topic:\"AI in 2026\" rounds:5"},
        "ttrpg": {"min": 2, "desc": "DND Campaign — dungeon master runs a campaign with auto dice rolling",
                  "needs_topic": False,
                  "example": "/game mode:ttrpg referee:@BotA"},
    }

    @bot.tree.command(name="game", description="🎮 Start a game between bots in voice chat")
    @discord.app_commands.describe(
        mode="Game mode — pick from: debate, council, auction, 20questions, show_tell, pokemon, mtg, comedy, poetry, interrogate, turing, althistory, podcast, ttrpg, dream",
        topic="Topic — REQUIRED for debate/council/althistory/podcast",
        rounds="Number of rounds (default: 3)",
        referee="@mention the bot, or use their name like 'Sassy' (leave blank for auto)",
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
            ref_clean = referee.strip()
            try:
                rid = int(ref_clean.strip("<@!>"))
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
                if vc_channel:
                    name_search = ref_clean.lstrip("@").lower()
                    found = None
                    for member in vc_channel.members:
                        if member.bot and member.display_name.lower() == name_search:
                            if member.id in bot_ids:
                                found = member.id; break
                        if member.bot and member.name and member.name.lower() == name_search:
                            if member.id in bot_ids:
                                found = member.id; break
                    if found is None:
                        for member in vc_channel.members:
                            if member.bot and name_search in member.display_name.lower():
                                if member.id in bot_ids:
                                    found = member.id; break
                            if member.bot and member.name and name_search in member.name.lower():
                                if member.id in bot_ids:
                                    found = member.id; break
                    if found is not None:
                        ref_id = found
                    else:
                        ref_names = " ".join(f"<@{b}>" for b in bot_ids)
                        return await interaction.response.send_message(
                            f"❌ Couldn't find '{ref_clean}' in the voice channel.\n"
                            f"Bots in VC: {ref_names}\n\n"
                            f"Try @mentioning the bot or use their exact name.",
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
            elif mode == "comedy":
                _init_comedy_state(gid, bot_ids, ref_id)
            elif mode == "poetry":
                _init_poetry_state(gid, bot_ids, ref_id)
            elif mode == "interrogation":
                cops = [b for b in bot_ids if b != ref_id]
                good_id = cops[0] if cops else bot_ids[0]
                bad_id = cops[1] if len(cops) > 1 else good_id
                suspect_id = cops[2] if len(cops) > 2 else (cops[-1] if len(cops) > 1 else bot_ids[0])
                _init_interrogation_state(gid, bot_ids, ref_id, good_id, bad_id, suspect_id)
            elif mode == "turing":
                _init_turing_state(gid, bot_ids, ref_id)
            elif mode == "althistory":
                _init_alt_history_state(gid, bot_ids, topic or "alternate history")
            elif mode == "podcast":
                _init_podcast_state(gid, bot_ids, bot_ids[0], topic or "technology")
            elif mode == "ttrpg":
                _init_ttrpg_state(gid, bot_ids, bot_ids[0])
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
