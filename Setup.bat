@echo off
chcp 65001 >nul
title Bot-Forge — Discord AI Bot Games Setup

:: ── Color support ──
set "ESC="
for /f "tokens=1,2 delims=#" %%a in ('"prompt #$H#$E# & echo on & for %%b in (1) do rem"') do set "ESC=%%b"

:start
cls
echo.
echo %ESC%[96m╔═══════════════════════════════════════════════╗
echo ║     Bot-Forge — Discord AI Bot Games         ║
echo ║     Interactive Setup Wizard                  ║
echo ╚═══════════════════════════════════════════════╝%ESC%[0m
echo.
echo Created by DrGekoz — Ship fast then refine.
echo.

:: ── Check for existing installation ──
if exist "bots\*.yaml" (
    echo %ESC%[93m⚠️  Existing bot configs detected!%ESC%[0m
    echo.
    echo Running Setup.bat again will overwrite your current
    echo configuration including bot tokens, personalities,
    echo voice refs, and provider settings.
    echo.
    choice /c:ON /n /m "%ESC%[93m[O]verwrite existing configs or create [N]ew ones alongside? (O/N):%ESC%[0m "
    if errorlevel 2 (
        echo %ESC%[92m✅ Existing configs preserved. New configs will be added.%ESC%[0m
        set APPEND_MODE=1
    ) else (
        echo %ESC%[91m⚠️  Existing configs will be overwritten!%ESC%[0m
        choice /c:YN /n /m "Are you sure? (Y/N): "
        if errorlevel 2 goto :start
        set APPEND_MODE=0
    )
) else (
    set APPEND_MODE=0
)
echo.

:: ── Check Python ──
echo %ESC%[96m[1/7] Checking prerequisites...%ESC%[0m
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo %ESC%[91m❌ Python is not installed or not on PATH.%ESC%[0m
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PY_VER=%%i
echo %ESC%[92m✅ Python %PY_VER% detected%ESC%[0m

git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo %ESC%[93m⚠️  Git not found. PocketTTS installation will be skipped.%ESC%[0m
    set GIT_AVAIL=0
) else (
    echo %ESC%[92m✅ Git detected%ESC%[0m
    set GIT_AVAIL=1
)

:: ── Install Dependencies ──
echo.
echo %ESC%[96m[2/7] Installing Python dependencies...%ESC%[0m
python -m pip install discord.py httpx openai pyyaml --quiet
if %errorlevel% neq 0 (
    echo %ESC%[93m⚠️  Some dependencies had issues. Continuing anyway...%ESC%[0m
) else (
    echo %ESC%[92m✅ Dependencies installed%ESC%[0m
)

:: ── Number of Bots ──
echo.
echo %ESC%[96m[3/7] Bot count%ESC%[0m
echo.
set /p NUM_BOTS="How many bots do you want? (minimum 2 for games, default 3): "
if "%NUM_BOTS%"=="" set NUM_BOTS=3
if %NUM_BOTS% LSS 2 (
    echo %ESC%[93mMinimum 2 bots. Using 2.%ESC%[0m
    set NUM_BOTS=2
)
if %NUM_BOTS% GTR 10 (
    echo %ESC%[93mMaximum 10 bots. Using 10.%ESC%[0m
    set NUM_BOTS=10
)
echo %ESC%[92m✅ %NUM_BOTS% bots will be created%ESC%[0m

:: ── Discord Bot Creation Instructions ──
echo.
echo %ESC%[96m[4/9] Discord Bot Setup%ESC%[0m
echo.
echo You need to create %NUM_BOTS% Discord Bot applications.
echo Open this link:  https://discord.com/developers/applications
echo.
echo For EACH bot you need:
echo   1. Click "New Application" - give it any name
echo   2. Go to "Bot" in left sidebar
echo   3. Click "Reset Token" - COPY THE TOKEN
echo   4. Under Privileged Gateway Intents:
echo      - Enable MESSAGE CONTENT INTENT *** CRITICAL — without this the bot will
echo        crash immediately with a PrivilegedIntentsRequired error!
echo      - Enable SERVER MEMBERS INTENT (recommended)
echo   5. Go to OAuth2 - URL Generator
echo   6. Check "bot" and "applications.commands"
echo   7. Check permissions: Send Messages, Connect, Speak,
echo      Use Voice Activity, Read Message History
echo   8. Copy the generated URL to invite later
echo.
pause

:: ── AI Provider ──
echo.
echo %ESC%[96m[5/7] AI Provider Configuration%ESC%[0m
echo.
echo Choose the AI provider that powers your bots' responses.
echo.
echo   1. LM Studio          (http://localhost:1234/v1)
echo   2. Ollama             (http://localhost:11434/v1)
echo   3. vLLM               (http://localhost:8000/v1)
echo   4. OpenAI             (api.openai.com)
echo   5. OpenRouter         (openrouter.ai)
echo   6. Gemini             (Google AI Studio)
echo   7. OpenCode GO / Zen  (custom endpoint)
echo   8. Custom provider    (enter your own)
echo.
set /p PCH="Provider [1-8, default 1]: "
if "%PCH%"=="" set PCH=1

if "%PCH%"=="1" set PNAME=LM Studio& set BURL=http://localhost:1234/v1& set DEFMOD=llama-3.2-3b-instruct
if "%PCH%"=="2" set PNAME=Ollama& set BURL=http://localhost:11434/v1& set DEFMOD=llama3.2:3b
if "%PCH%"=="3" set PNAME=vLLM& set BURL=http://localhost:8000/v1& set DEFMOD=mistral-7b-instruct
if "%PCH%"=="4" set PNAME=OpenAI& set BURL=https://api.openai.com/v1& set NEEDKEY=1& set DEFMOD=gpt-4o-mini
if "%PCH%"=="5" set PNAME=OpenRouter& set BURL=https://openrouter.ai/api/v1& set NEEDKEY=1& set DEFMOD=anthropic/claude-3-haiku
if "%PCH%"=="6" set PNAME=Gemini& set BURL=https://generativelanguage.googleapis.com/v1beta& set NEEDKEY=1& set DEFMOD=gemini-2.0-flash
if "%PCH%"=="7" set PNAME=OpenCode& set NEEDKEY=1& set DEFMOD=deepseek-v4-flash
if "%PCH%"=="8" set PNAME=Custom& set NEEDKEY=0& set DEFMOD=

if "%PCH%"=="7" (
    set /p BURL="Enter your OpenCode endpoint URL: "
)
if "%PCH%"=="8" (
    set /p BURL="Enter your provider's base URL: "
)
if "%DEFMOD%"=="" (
    set /p MODEL_NAME="Enter model name: "
) else (
    set /p MODEL_NAME="Model name (default: %DEFMOD%): "
    if "!MODEL_NAME!"=="" set MODEL_NAME=%DEFMOD%
)
if "%NEEDKEY%"=="1" (
    set /p API_KEY="Enter API key: "
) else (
    set API_KEY=
)

echo %ESC%[92m✅ Provider: %PNAME% (%MODEL_NAME%)%ESC%[0m

:: ── Per-bot models? ──
echo.
choice /c:YN /n /m "Use a different AI provider/model for each bot? (Y/N, default N): "
if errorlevel 2 (
    set PER_BOT_MODEL=0
    echo %ESC%[92mAll bots will use the same provider.%ESC%[0m
) else (
    set PER_BOT_MODEL=1
    echo %ESC%[92mEach bot will have its own provider.%ESC%[0m
)

:: ── Silero VAD + faster-whisper ──
echo.
echo %ESC%[96m[7/9] Voice Listening (Optional)%ESC%[0m
echo.
echo Bot-Forge supports voice listening mode: bots hear humans in VC and respond.
echo This uses Silero VAD (voice detection) + faster-whisper (speech-to-text).
echo.
echo These are OPTIONAL — bots work without them, but voice listening won't be available.
echo.
choice /c:YN /n /m "Install Silero VAD + faster-whisper for voice listening? (Y/N, default Y): "
if not errorlevel 2 (
    echo Installing Silero VAD...
    python -m pip install silero-vad --quiet
    if %errorlevel% equ 0 (
        echo %ESC%[92m✅ Silero VAD installed%ESC%[0m
    ) else (
        echo %ESC%[93m⚠️  Silero VAD install had issues (non-critical)%ESC%[0m
    )
    echo Installing faster-whisper...
    python -m pip install faster-whisper --quiet
    if %errorlevel% equ 0 (
        echo %ESC%[92m✅ faster-whisper installed%ESC%[0m
    ) else (
        echo %ESC%[93m⚠️  faster-whisper install had issues (non-critical)%ESC%[0m
    )
) else (
    echo %ESC%[93mSkipping voice listening. Bots won't hear humans in VC.%ESC%[0m
)

:: ── Text Channel Mode ──
echo.
echo %ESC%[96m[7.5/9] Text Channel Mode%ESC%[0m
echo.
echo By default, Bot-Forge runs games in voice channels with TTS.
echo You can also run games in TEXT channels only — bots post messages instead.
echo.
choice /c:TN /n /m "Run games in TEXT channels instead of voice? (T)ext / Voice (N), default N): "
if errorlevel 2 (
    set TEXT_MODE=0
    echo %ESC%[92mVoice mode (TTS in VC)%ESC%[0m
) else (
    set TEXT_MODE=1
    echo %ESC%[92mText mode (messages in channel)%ESC%[0m
)

:: ── PocketTTS ──
echo.
echo %ESC%[96m[6/9] Voice Cloning (PocketTTS)%ESC%[0m
echo.
echo PocketTTS gives each bot a unique voice in Discord VC.
echo You provide a 10-second audio clip per bot, and it creates a voice clone.
echo.
choice /c:YN /n /m "Set up PocketTTS voice cloning? (Y/N, default Y): "
if not errorlevel 2 (
    if "%GIT_AVAIL%"=="1" (
        if not exist "pockettts\PocketTTS" (
            echo Cloning PocketTTS...
            if not exist "pockettts" mkdir pockettts
            cd pockettts
            git clone https://github.com/Kyutai/PocketTTS.git
            cd PocketTTS
            python -m pip install -e . --quiet
            cd ..\..
        ) else (
            echo %ESC%[92m✅ PocketTTS already installed%ESC%[0m
        )
        echo.
        choice /c:YN /n /m "Download the ~2GB voice model now? (takes a few minutes) (Y/N): "
        if not errorlevel 2 (
            python -m pocket_tts.download_model
        )
    ) else (
        echo %ESC%[93mGit not available. Skipping PocketTTS installation.%ESC%[0m
        echo You can install it manually later from https://github.com/Kyutai/PocketTTS
    )
    if not exist "pockettts\voices" mkdir pockettts\voices
    if not exist "voice-refs" mkdir voice-refs
    echo %ESC%[92m✅ PocketTTS ready%ESC%[0m
) else (
    echo %ESC%[93mSkipping voice cloning. Bots will use default TTS.%ESC%[0m
    set SKIP_TTS=1
)

:: ── Memory Setup ──
echo.
echo %ESC%[96m[8/9] Persistent Bot Memory%ESC%[0m
echo.
echo Bot-Forge includes a built-in memory system that lets bots
echo remember conversations across sessions - users' preferences,
echo past topics, and important facts.
echo.
echo It runs as a lightweight SQLite database — no external services needed.
echo.
choice /c:YN /n /m "Enable persistent memory? (Y/N, default Y): "
if not errorlevel 2 (
    echo %ESC%[92m✅ Memory store will be initialized at memory_store.db%ESC%[0m
    echo.
    python -c "from core.memory_store import MemoryStore; s = MemoryStore('memory_store.db'); print(f'Created {s.db_path}')"
    if %errorlevel% equ 0 (
        echo %ESC%[92m✅ Memory database created%ESC%[0m
    ) else (
        echo %ESC%[93m⚠️  Could not initialize memory database — will be created on first use.%ESC%[0m
    )
) else (
    echo %ESC%[93mSkipping memory setup. Bots will have no persistent memory.%ESC%[0m
    set SKIP_MEMORY=1
)

:: ── Bot Details ──
echo.
echo %ESC%[96m[9/9] Configuring Your Bots%ESC%[0m
echo.
echo Now we'll set up each bot. You'll need:
echo   - A name for each bot
echo   - The bot token from Discord Developer Portal
echo   - A personality prompt (describe how it behaves)
echo   - An optional voice reference WAV file (8-15 seconds)
echo.

setlocal enabledelayedexpansion

:: If not appending, clean existing bots
if "%APPEND_MODE%"=="0" (
    if exist "bots" (
        echo Cleaning existing bot configs...
        rmdir /s /q bots >nul 2>&1
        mkdir bots
    )
)

if not exist "bots" mkdir bots

:: Reset config tracking
set BOT_INDEX=1
set BOT_NAMES=
set CONFIG_COUNT=0

:: Count existing configs for numbering
if "%APPEND_MODE%"=="1" (
    set NEXT_NUM=1
    for /d %%d in (bots\*) do set /a NEXT_NUM+=1
) else (
    set NEXT_NUM=1
)

:bootstrap_loop
if %BOT_INDEX% gtr %NUM_BOTS% goto :bootstrap_done

echo.
echo %ESC%[96m────────── Bot #%BOT_INDEX% ──────────%ESC%[0m
echo.

set /p BOT_NAME="Enter a name for this bot: "
if "!BOT_NAME!"=="" set BOT_NAME=Bot%NEXT_NUM%

echo.
echo %ESC%[93mOpen https://discord.com/developers/applications%ESC%[0m
echo %ESC%[93mCreate a new application, go to Bot settings, and copy the token.%ESC%[0m
echo.
:get_token
set /p BOT_TOKEN="Paste the bot token for !BOT_NAME!: "
:: Simple validation - token should have 3 parts separated by dots
echo !BOT_TOKEN! | findstr /r /c:"\..*\..*" >nul
if errorlevel 1 (
    echo %ESC%[91mThat doesn't look like a valid Discord token. Try again.%ESC%[0m
    echo %ESC%[93mIt should be a long string with dots, like: MTExNjExMjYw...%ESC%[0m
    goto :get_token
)

echo.
echo %ESC%[96mPersonality Prompt for !BOT_NAME!:%ESC%[0m
echo %ESC%[90mDescribe how this bot should behave. This is the core of its character.
echo Be as detailed or as simple as you like.
echo.
echo Example:
echo   "You are a fast-talking auctioneer who sells bizarre items..."
echo.
echo Type DONE on its own line when finished.%ESC%[0m
echo.
set PROMPT_FILE=%TEMP%\botforge_prompt_%BOT_INDEX%.txt
if exist "!PROMPT_FILE!" del "!PROMPT_FILE!"
:prompt_loop
set /p LINE=""
if /i "!LINE!"=="DONE" goto :prompt_done
echo !LINE!>> "!PROMPT_FILE!"
goto :prompt_loop
:prompt_done

:: Read personality prompt
set PERSONALITY=
if exist "!PROMPT_FILE!" (
    for /f "usebackq delims=" %%a in ("!PROMPT_FILE!") do (
        if defined PERSONALITY (
            set PERSONALITY=!PERSONALITY!\n%%a
        ) else (
            set PERSONALITY=%%a
        )
    )
)
del "!PROMPT_FILE!" 2>nul

:: Voice reference
echo.
choice /c:YN /n /m "Add a voice reference file for !BOT_NAME!? (8-15 second WAV/MP3) (Y/N, default Y): "
if not errorlevel 2 (
    echo %ESC%[90mRequirements: 8-15 seconds, WAV/MP3/OGG, clear speech, under 5MB%ESC%[0m
    :get_voice
    set /p VOICE_PATH="Full path to audio file: "
    set "VOICE_PATH=!VOICE_PATH:"=!"
    if not "!VOICE_PATH!"=="" (
        if exist "!VOICE_PATH!" (
            for %%f in ("!VOICE_PATH!") do set VEXT=%%~xf
            copy "!VOICE_PATH!" "voice-refs\!BOT_NAME!!VEXT!" >nul
            echo %ESC%[92m✅ Voice reference saved%ESC%[0m
            if exist "pockettts\PocketTTS" (
                if not "!SKIP_TTS!"=="1" (
                    echo Creating voice clone...
                    python -m pocket_tts.export_voice "!VOICE_PATH!" "pockettts\voices\!BOT_NAME!.pt"
                    if exist "pockettts\voices\!BOT_NAME!.pt" (
                        echo %ESC%[92m✅ Voice clone created%ESC%[0m
                    )
                )
            )
        ) else (
            echo %ESC%[91mFile not found. Try again or leave blank to skip.%ESC%[0m
            set VOICE_PATH=
            goto :get_voice
        )
    )
) else (
    echo Voice reference skipped.
)

:: Per-bot provider override
if "%PER_BOT_MODEL%"=="1" (
    echo.
    echo %ESC%[96mProvider for !BOT_NAME!:%ESC%[0m
    set /p PCH2="Same as main provider? (Y/n): "
    if /i "!PCH2!"=="n" (
        set /p BOT_BURL="Base URL: "
        set /p BOT_MODEL="Model: "
        set /p BOT_KEY="API key (if needed): "
    )
)

:: ── Write config.yaml ──
set BOT_DIR=bots\!BOT_NAME!
if not exist "!BOT_DIR!" mkdir "!BOT_DIR!"

:: Write the config yaml
(
echo # Bot-Forge Configuration
echo # Generated: %DATE% %TIME%
echo.
echo bot_token: "%BOT_TOKEN%"
echo client_id: AUTO
echo status_message: "!BOT_NAME! 🎮"
echo.
echo max_text: 100000
echo max_images: 5
echo max_messages: 3
echo use_plain_responses: true
echo allow_dms: false
echo message_content_intent: true
echo.
echo permissions:
echo   users:
echo     admin_ids: []
echo     allowed_ids: []
echo     blocked_ids: []
echo.
echo providers:
echo   main:
echo     base_url: "!BOT_BURL!%BURL%"
echo     api_key: "%API_KEY%"
echo     model: "!BOT_MODEL!%MODEL_NAME%"
echo.
echo tts_personality: "!BOT_NAME!"
echo.
echo system_prompt: |-
echo   IMPORTANT: You are a bot. You are NOT the user.
echo   Never claim to be the user.
echo   You are !BOT_NAME!.
echo.
echo personality_prompt: |-
) > "!BOT_DIR!\config.yaml"

:: Append personality (multi-line with proper YAML indentation)
setlocal disabledelayedexpansion
for /f "delims=" %%a in ("%TEMP%\botforge_personality_%BOT_INDEX%.txt") do (
    if exist "%%a" (
        for /f "usebackq delims=" %%b in ("%%a") do (
            echo   %%b>> "!BOT_DIR!\config.yaml"
        )
    )
)
setlocal enabledelayedexpansion

:: Write start.bat
(
echo @echo off
echo cd /d "%~dp0..\.."
echo python core\llmcord.py "bots\!BOT_NAME!\config.yaml"
echo pause
) > "!BOT_DIR!\start.bat"

echo %ESC%[92m✅ !BOT_NAME! configured%ESC%[0m

set BOT_NAMES=!BOT_NAMES! !BOT_NAME!
set /a BOT_INDEX+=1
set /a NEXT_NUM+=1
goto :bootstrap_loop

:bootstrap_done

:: ── Choose referee bot ──
echo.
echo %ESC%[96m[9.5/9] Designate Referee Bot%ESC%[0m
echo.
echo One bot acts as the REFEREE for all games ^(judges debates, scores battles, etc.^).
echo The other bots will always defer to this bot for judging.
echo.
set REFEREE_NAME=
for %%n in (%BOT_NAMES%) do (
    if "!REFEREE_NAME!"=="" (
        choice /c:YN /n /m "Is %%n the referee? (Y/N, default Y for first bot): "
        if not errorlevel 2 (
            set REFEREE_NAME=%%n
        )
    )
)
if "!REFEREE_NAME!"=="" (
    for %%f in ("bots\*") do set REFEREE_NAME=%%~nxf
    echo %ESC%[93mDefaulting to first bot as referee.%ESC%[0m
)
echo %ESC%[92m✅ Referee: !REFEREE_NAME!%ESC%[0m

:: Write referee_bot_name to ALL bot configs so every instance knows who the ref is
setlocal enabledelayedexpansion
for /d %%d in (bots\*) do (
    set "CFG=%%d\config.yaml"
    if exist "!CFG!" (
        echo referee_bot_name: "!REFEREE_NAME!" >> "!CFG!"
    )
)
endlocal

:: ── Generate Launchers ──
echo.
echo %ESC%[96mGenerating start script...%ESC%[0m

:: Generate start_bot_forge.bat — a simple bat wrapper around the unified launcher
(
echo @echo off
echo title Bot-Forge - AI Bot Games
echo.
echo python run_bot_forge.py
echo pause
) > start_bot_forge.bat
echo %ESC%[92m✅ Created start_bot_forge.bat%ESC%[0m
echo.
echo %ESC%[92mThe unified launcher (run_bot_forge.py) starts everything:^%ESC%[0m
echo %ESC%[92m  - Memory server (if enabled)^%ESC%[0m
echo %ESC%[92m  - PocketTTS server (if installed)^%ESC%[0m
echo %ESC%[92m  - All bot instances with labeled logs^%ESC%[0m

:: ── Invite Links ──
echo.
echo %ESC%[96m═══════════════════════════════════════════%ESC%[0m
echo %ESC%[92m        ✅  Setup Complete!%ESC%[0m
echo %ESC%[96m═══════════════════════════════════════════%ESC%[0m
echo.
echo To invite your bots to a Discord server:
echo.
echo   1. Go to https://discord.com/developers/applications
echo   2. Click each bot's application
echo   3. Copy the APPLICATION ID (not the token)
echo   4. Replace CLIENT_ID in this URL and open it:
echo.
echo   https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID^&permissions=277025770560^&scope=bot+applications.commands
echo.
echo   5. Select your server and authorize
echo.
echo %ESC%[96m───────────────────────────────────────%ESC%[0m
echo.
echo %ESC%[92mNext steps:%ESC%[0m
echo.
echo   1. Invite all bots to your server ^(see above^)
echo   2. Run:  start_bot_forge.bat   ^(starts everything: bots + TTS + memory^)
echo       OR  python run_bot_forge.py  ^(same thing, with colored logs^)
echo   3. Join a voice channel in Discord
echo   4. Type  /join    to have a bot join
echo   5. Type  /auction_house   to start a game!
echo.
echo %ESC%[90mNeed help? DM me on Discord: drgekoz%ESC%[0m
echo.
pause
