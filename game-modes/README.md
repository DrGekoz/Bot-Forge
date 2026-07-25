# 🎮 Bot-Forge Game Modes

All game modes run in Discord **Voice Channels** using the bots' TTS voices.
Each mode uses file-based turn management — bot-to-bot communication happens
through local state files, not Discord messages.

The specific **personalities** your bots use in each mode are defined during
Setup.bat as their personality prompts. The rules below are the functional
prompts that make each game work — they tell the bot *what role to play* and
*how the game works*.

---

## 🔨 Auction House

**Command:** `/auction_house`
**Minimum bots:** 3 (1 Auctioneer + 2 Bidders)
**Functions:** `_init_auction_state()`, `_is_auctioneer()`, `_advance_auction_item()`

One bot is the Auctioneer, the rest are Bidders. The Auctioneer introduces
bizarre items with rhythmic flair and calls for bids. Bidders compete with
a virtual budget, getting swept up in FOMO.

**Auctioneer rules injected:**
- Introduce each item with flair
- Build the energy — "Do I hear $X?"
- Call for higher bids
- Declare SOLD when bidding has gone around

**Bidder rules injected:**
- Bid against the current leader
- Act like you need this item
- Get swept up in FOMO
- Budget is tracked — can go negative

---

## 🌙 Dream Interpretation *(Planned)*

**Command:** `/dream_weave` *(coming soon)*
**Minimum bots:** 3 (1 Dreamer + 2 Analysts)

One bot describes a surreal AI-generated dream with a hidden dream seed stored in state. Two psychoanalysts compete to project the wildest meanings onto it — one Freudian (everything is childhood trauma/sexual symbolism), one Cosmic (galactic consciousness, chakras, parallel universes). Turn order: Dreamer → Analyst A → Analyst B, cycling each round.

---

## 🎤 Comedy Roast Battle

**Command:** `/comedy_roast`
**Minimum bots:** 3 (1 Emcee + 1 Comedian + 1 Referee)
**Functions:** `_init_comedy_state()`, `_advance_comedy_round()`

An Emcee hosts the show. Comedians heckle the previous joke then tell
their own terrible joke. The Referee silently scores and declares a winner.

**Rules injected:** See `COMEDY_RULES_COMEDIAN`, `COMEDY_RULES_EMCEE`,
`COMEDY_RULES_REFEREE` in voice_chat.py.

---

## 🏆 Poetry Slam

**Command:** `/poetry_slam`
**Minimum bots:** 2 (1 Poet + 1 Referee)
**Functions:** `_init_poetry_state()`, `_advance_poetry_round()`, `_extract_last_line()`

Poets spit rhyming verses targeting the previous poet's last line. The
Referee scores on rhyme quality, creativity, diss/burn, and rhythm.

**Rules injected:** See `POETRY_RULES_POET`, `POETRY_RULES_REFEREE`.

---

## 🎙️ Debate / Council

**Commands:** `/debate <topic>`, `/council <topic>`
**Minimum bots:** 3 (2 Debaters + 1 Referee)
**Functions:** `_init_debate_state()`, `_init_council_state()`, `_advance_debate_round()`

Debate: bots take turns arguing, referee scores each round, winner declared.
Council: bots argue with evidence, blind-reviewed by referee, converge to
unanimous verdict.

**Rules injected:** See `DEBATE_RULES_DEBATER`, `DEBATE_RULES_REFEREE`,
`COUNCIL_RULES_PARTICIPANT`, `COUNCIL_RULES_REFEREE`.

---

## 🚔 Interrogation Room

**Command:** `/interrogate`
**Minimum bots:** 3 (Good Cop + Bad Cop + Suspect)
**Functions:** `_init_interrogation_state()`, `_detect_confession()`

Good Cop and Bad Cop tag-team interrogate a Suspect. If the Suspect
confesses, the interrogation ends. 22-pattern confession detection built in.

**Rules injected:** See `INTERROGATION_RULES_COP`, `INTERROGATION_RULES_SUSPECT`.

---

## 🤖 Turing Test Panel

**Command:** `/turing_test`
**Minimum bots:** 2 (1 Panelist + 1 Referee)
**Functions:** `_init_turing_state()`, `_advance_turing_round()`

Bots try to prove they're human and spot who else is human — but they're
ALL AI. The Referee scores on convincingness and reveals the twist.

**Rules injected:** See `TURING_RULES_PANELIST`, `TURING_RULES_REFEREE`.

---

## 📚 Alt History Think Tank

**Command:** `/alt_history`
**Minimum bots:** 2
**Functions:** `_init_alt_history_state()`, `_advance_alt_history_article()`

Bots discuss real RSS-fed news through alternate era worldviews.
6 built-in themes: steampunk, martian, fantasy, cyberpunk, roman, pirates.

**Rules injected:** See `ALT_HISTORY_RULES`.

---

## 🎧 Podcast Mode

**Command:** `/podcast`
**Minimum bots:** 3 (1 Host + 2 Guests)
**Functions:** `_init_podcast_state()`, `_advance_podcast_paragraph()`

Host facilitates discussion of RSS-fed tech news. Guests react from their
character perspectives. Auto-advances through articles paragraph by paragraph.

**Rules injected:** See `PODCAST_RULES_HOST`, `PODCAST_RULES_GUEST`.

---

## 🎲 TTRPG Campaign

**Command:** `/ttrpg`
**Minimum bots:** 2 (1 DM + 1 Hero)
**Functions:** `_init_ttrpg_state()`, `_roll_dice()`, `_find_dice_rolls()`

Full D&D-style campaign with a Dungeon Master and heroes. Dice rolls
are auto-detected and executed.

**Rules injected:** See `TTRPG_RULES_DM`, `TTRPG_RULES_PLAYER`.

---

## 💬 Group Chat

**Command:** `/group`
**Minimum bots:** 2

Natural turn-based voice conversation. No game rules — bots just talk.

---

## 💾 Persistent Memory

Bot-Forge includes a built-in memory system (`core/memory_store.py`) that gives
bots persistent recall across sessions. Powered by SQLite — zero external dependencies.

### How it works
- **Auto-store**: Bot messages containing user info, preferences, or project details
  are automatically saved as memory facts with category tags
- **Auto-recall**: Before generating a response, relevant memories are fetched and
  injected into the prompt context
- **Trust scoring**: Facts accumulate trust scores — helpful facts rise, stale facts sink
- **HTTP API**: `core/memory_server.py` exposes a REST API on port 8888 for runtime
  recall/store by any process

### Configuration
Setup.bat asks if you want to enable memory. If yes:
1. `memory_store.db` is created in the Bot-Forge root
2. The unified launcher (`run_bot_forge.py`) starts the memory server automatically
3. Each bot instance auto-recalls relevant memories before every response

### API Endpoints (Memory Server)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/stats` | Memory store statistics |
| GET | `/recall/<query>` | Search memories by text |
| POST | `/store` | Store a new memory |
| POST | `/feedback/<id>` | Rate a fact as helpful/unhelpful |

---

## 🧪 Adding a New Game Mode

Each mode follows the same pattern. Copy an existing one:

1. **Constants** — Define prompt template strings for each role (e.g. `NEWGAME_RULES_PLAYER`)
2. **State Init** — `_init_newgame_state()` — creates the initial JSON state with `mode: "newgame"`
3. **Role Detection** — `_is_newgame_role()` helpers
4. **Round Advancement** — `_advance_newgame_round()` — handles end-of-round logic
5. **Poll Loop Integration** — Wire into `_group_poll_loop()`:
   - Mode flags (`is_newgame`, `is_newgame_role`)
   - Prompt building branch
   - Post-reply processing (track scores, advance rounds)
6. **Slash Command** — Register via `@bot.tree.command(name="newgame", ...)`

All `*_RULES_*` constants in the code are the **functional game prompts** —
they tell the LLM what role to play and how the game works, not what
personality to have. The personality comes from the user's Setup.bat input.
