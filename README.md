# Jarvis MVP (Voice + Gemini)

## Setup (Windows)

1. **Install Python 3.12** if you don't have it (Python 3.14+ breaks PyAudio/distutils, so 3.12 specifically): https://www.python.org/downloads/release/python-3120/

2. **Open this folder in a terminal (cmd/PowerShell)** and create a virtual environment using Python 3.12 specifically:
   ```
   py -3.12 -m venv venv312
   venv312\Scripts\activate
   ```
   (Always activate `venv312` — not any other `venv` folder — before running anything below.)

3. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```
   If `PyAudio` still fails to build, double check you're on Python 3.12 (`python --version` after activating) — the prebuilt wheel only exists for 3.10–3.13.

4. **Set up your API keys:**
   - Rename `.env.example` to `.env`
   - Get a free Gemini key: https://aistudio.google.com/app/apikey
   - Get an ElevenLabs key (free tier works): https://elevenlabs.io
   - Find your voice's ID in ElevenLabs under VoiceLab, and paste it in
   - Fill all three values into `.env`

5. **Run it:**
   ```
   python main.py
   ```
   Say "jarvis" to activate it, then speak your command.

## What this version does
- **Wake word**: say "jarvis" any time to activate it (no button to press)
- **Multi-turn conversation**: once awake, keep talking without repeating "jarvis"
- Speech-to-text via free Google Web Speech API
- Chat via Gemini (`gemini-3.6-flash`), with tool use enabled
- Text-to-speech via ElevenLabs using your chosen voice
- **Persistent memory**: Jarvis remembers facts about you across restarts
  (saved to `Jarvis_Files/jarvis_memory.json`), like your name or preferences
- **Tools Gemini can use on its own:**
  - Open any installed app by name (searches your Start Menu shortcuts; falls back to a browser search if nothing installed matches)
  - Close a running app by name (matches against whatever's actually running, not just a fixed list)
  - Minimize or restore an open window by matching its title
  - Create text files (saved to a `Jarvis_Files` folder on your Desktop)
  - List files in that folder
  - Quick web search (DuckDuckGo Instant Answer API — good for facts, not deep search)
  - Remember/recall/forget permanent facts about you
- Say "goodbye jarvis" to exit, or Ctrl+C

## Examples to try
- "Jarvis, my name is Joseph, remember that"
- (restart the program) "Jarvis, what do you remember about me?"
- "Jarvis, open notepad"
- "Jarvis, create a file called todo.txt with my grocery list: milk, eggs, bread"

## Dashboard (live status page)
Jarvis writes its current state to `dashboard/status.json` while running.
To view it as a webpage:

1. Open a **second** terminal (leave Jarvis running in the first), then activate the same environment:
   ```
   venv312\Scripts\activate
   ```
2. In the `jarvis` folder, run:
   ```
   python -m http.server 8099 --directory dashboard
   ```
3. Open http://localhost:8099/dashboard.html in your browser (type the full address into the browser's address bar at the top — not a search box)

It shows whether Jarvis is idle/listening/thinking/speaking, the last
thing you said, its last reply, and everything in its permanent memory —
updating automatically once a second.

## Not included yet (future phases)
- Gmail / Spotify API integrations (currently Spotify only *opens* the app, doesn't control playback) — these need you to register a developer app with Google/Spotify and do a one-time browser login (OAuth); ask and we'll set them up next
- Running arbitrary system commands (intentionally left out for safety — only a curated set of apps/files)

Tell me what to add next and we'll build it the same way.
