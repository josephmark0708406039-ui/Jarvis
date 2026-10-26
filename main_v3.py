"""
Jarvis v2 - Wake-word voice assistant powered by Gemini, with tool use.

How it works:
1. Jarvis listens continuously in the background for the wake word ("jarvis").
2. Once heard, it listens for your actual command.
3. Speech is converted to text (free Google Web Speech API).
4. Gemini processes it — and can call built-in tools itself:
     - open_app(name)       -> opens any app visible in your Start Menu
                               (desktop apps AND Store apps like TikTok)
     - close_app / minimize_window / restore_window
     - create_file(name, content) -> saves a text file to your Jarvis_Files folder
     - list_files()         -> lists what's in that folder
     - web_search(query)    -> quick web lookup (DuckDuckGo Instant Answer API)
     - get_weather(city, day) -> real forecast via Open-Meteo (no API key needed)
5. Gemini's reply is spoken back via ElevenLabs.

Only a fixed, named list of apps can be opened or closed — Jarvis cannot
run arbitrary shell commands.

Say "goodbye jarvis" any time to exit, or press Ctrl+C.

--- Migration note ---
This uses the new `google-genai` SDK (the `google.generativeai` package
this used to run on is fully deprecated — no more updates/bug fixes).
Install it with: pip install google-genai
You can (and should) uninstall the old one: pip uninstall google-generativeai
"""

import json
import os
import sys
import subprocess
import tempfile
import threading
import queue
import time
import shutil
from pathlib import Path

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

import requests
import speech_recognition as sr
from dotenv import load_dotenv
from playsound import playsound

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
    sys.exit("Missing GEMINI_API_KEY. Set it in your .env file.")

WAKE_WORD = "jarvis"
EXIT_PHRASES = ("goodbye jarvis", "quit jarvis", "shut down jarvis")

# Folder for the local status dashboard (a simple HTML page you can keep
# open in a browser tab to see what Jarvis is doing).
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
os.makedirs(DASHBOARD_DIR, exist_ok=True)
STATUS_FILE = os.path.join(DASHBOARD_DIR, "status.json")


def write_status(state: str, last_heard: str = "", last_reply: str = "") -> None:
    """Write current status + memory to a JSON file the dashboard reads.
    Never raises — a dashboard write failing should never crash Jarvis."""
    try:
        status = {
            "state": state,  # "idle" | "listening" | "thinking" | "speaking"
            "last_heard": last_heard,
            "last_reply": last_reply,
            "memory": load_memory(),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass
JARVIS_FILES_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "Jarvis_Files")
os.makedirs(JARVIS_FILES_DIR, exist_ok=True)

# Persistent memory: small list of facts Jarvis remembers across runs.
MEMORY_FILE = os.path.join(JARVIS_FILES_DIR, "jarvis_memory.json")


def load_memory() -> list:
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_memory(facts: list) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)


# Common apps Jarvis can open/close by name. Maps a spoken name to the
# actual executable name Windows uses (needed for both starting AND
# stopping the process safely). Add more here as needed.
APP_MAP = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "chrome": "chrome.exe",
    "browser": "chrome.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "spotify": "spotify.exe",
    "vs code": "Code.exe",
    "vscode": "Code.exe",
    "visual studio code": "Code.exe",
}


# ---------- Tools Gemini can call ----------

def _get_start_apps() -> list:
    """List every app the Windows Start Menu knows about — this covers
    both regular desktop apps AND Microsoft Store apps (like TikTok),
    which don't have classic shortcut files and would otherwise be
    invisible to Jarvis. Returns [(display_name, app_id), ...]."""
    apps = []
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
            capture_output=True, text=True, timeout=15,
        )
        for line in result.stdout.splitlines():
            if "|" in line:
                name, app_id = line.split("|", 1)
                apps.append((name.strip(), app_id.strip()))
    except Exception:
        pass
    return apps


def _launch_start_app(app_id: str) -> None:
    """Launch an app by its Start Menu AppID — works uniformly for both
    desktop apps and Store apps, unlike os.startfile alone."""
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])


def open_app(app_name: str) -> str:
    """Open an application installed on the user's Windows PC by name
    (e.g. 'notepad', 'spotify', 'vs code', 'tiktok', 'discord' — anything
    visible in the Start Menu). If nothing installed matches, opens the
    default web browser and searches for the app name instead.
    """
    key = app_name.strip().lower()

    # 1. Small curated map first (fast, exact, handles common aliases).
    exe = APP_MAP.get(key)
    if exe:
        try:
            os.startfile(exe)
            return f"Opened {app_name}."
        except OSError:
            pass  # fall through to Start Menu search

    # 2. Search everything the Start Menu shows (desktop + Store apps).
    apps = _get_start_apps()

    # Exact match first.
    for name, app_id in apps:
        if name.lower() == key:
            _launch_start_app(app_id)
            return f"Opened {app_name}."

    # Then substring match either direction.
    for name, app_id in apps:
        name_l = name.lower()
        if key in name_l or name_l in key:
            _launch_start_app(app_id)
            return f"Opened {app_name}."

    # Then "all significant words present" match (handles things like
    # "vs code" -> "Visual Studio Code" where it's not a plain substring).
    key_words = [w for w in key.split() if len(w) > 1]
    if key_words:
        for name, app_id in apps:
            name_l = name.lower()
            if all(w in name_l for w in key_words):
                _launch_start_app(app_id)
                return f"Opened {app_name}."

    # 3. Nothing installed matches — fall back to a browser search.
    try:
        import webbrowser
        from urllib.parse import quote_plus
        webbrowser.open(f"https://www.google.com/search?q={quote_plus(app_name)}")
        return f"I couldn't find {app_name} installed, so I searched for it in your browser instead."
    except Exception as e:
        return f"Couldn't find {app_name} or open a browser search: {e}"


def _get_running_processes() -> dict:
    """Return {name without .exe (lowercase): actual image name} for
    every process currently running, via the real Windows process list —
    never from arbitrary text."""
    procs = {}
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            parts = line.split('","')
            if not parts:
                continue
            name = parts[0].strip('"')
            key = name[:-4].lower() if name.lower().endswith(".exe") else name.lower()
            procs[key] = name
    except Exception:
        pass
    return procs


def close_app(app_name: str) -> str:
    """Close a running application by name (e.g. 'chrome', 'vs code',
    'notepad') — matches against whatever is actually currently running,
    so it works for more than just a fixed list."""
    key = app_name.strip().lower()

    # Curated aliases first (handles cases like "vs code" -> "Code.exe").
    exe = APP_MAP.get(key)

    if not exe:
        running = _get_running_processes()
        if key in running:
            exe = running[key]
        else:
            for proc_key, proc_name in running.items():
                if key in proc_key or proc_key in key:
                    exe = proc_name
                    break

    if not exe:
        return f"I couldn't find '{app_name}' running to close."

    result = subprocess.run(
        ["taskkill", "/IM", exe, "/F"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return f"Closed {app_name}."
    return f"{app_name} doesn't seem to be running."



# ---------- PC inspection / navigation tools ----------

def _resolve_user_path(path: str) -> Path:
    """Resolve a user-supplied Windows path and common spoken shortcuts."""
    raw = (path or "").strip().strip('"')
    if not raw:
        raise ValueError("A path is required.")

    expanded = os.path.expandvars(os.path.expanduser(raw))
    aliases = {
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "pictures": Path.home() / "Pictures",
        "videos": Path.home() / "Videos",
    }
    return aliases.get(raw.lower(), Path(expanded)).resolve()


def get_storage_info() -> str:
    """Report total, used and free storage for common Windows drives."""
    results = []
    checked = set()

    for candidate in ("C:\\", "D:\\", "E:\\", "F:\\"):
        try:
            usage = shutil.disk_usage(candidate)
            total_gb = usage.total / (1024 ** 3)
            used_gb = usage.used / (1024 ** 3)
            free_gb = usage.free / (1024 ** 3)
            percent = (usage.used / usage.total * 100) if usage.total else 0
            drive = candidate[:2].upper()
            if drive not in checked:
                results.append(
                    f"{drive} {percent:.0f}% used, "
                    f"{used_gb:.1f} GB used, {free_gb:.1f} GB free of {total_gb:.1f} GB"
                )
                checked.add(drive)
        except OSError:
            continue

    return "Storage: " + "; ".join(results) + "." if results else \
        "I couldn't read any Windows drives."


def get_path_info(path: str) -> str:
    """Inspect a file or folder without modifying it."""
    try:
        p = _resolve_user_path(path)
    except Exception as e:
        return f"Invalid path: {e}"

    if not p.exists():
        return f"I couldn't find {p}."

    try:
        if p.is_file():
            size_mb = p.stat().st_size / (1024 ** 2)
            return f"{p} is a file ({size_mb:.2f} MB)."

        items = list(p.iterdir())
        dirs = sum(x.is_dir() for x in items)
        files = sum(x.is_file() for x in items)
        return f"{p} is a folder containing {dirs} folders and {files} files."
    except OSError as e:
        return f"I couldn't inspect {p}: {e}"


def list_directory(path: str = "Desktop") -> str:
    """List the immediate contents of a folder."""
    try:
        p = _resolve_user_path(path)
    except Exception as e:
        return f"Invalid path: {e}"

    if not p.exists() or not p.is_dir():
        return f"{p} is not a folder I can list."

    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        if not items:
            return f"{p} is empty."

        shown = items[:40]
        names = [
            f"[{'Folder' if item.is_dir() else 'File'}] {item.name}"
            for item in shown
        ]
        extra = f" There are {len(items) - len(shown)} more items." if len(items) > len(shown) else ""
        return f"{p}: " + "; ".join(names) + extra
    except OSError as e:
        return f"I couldn't list {p}: {e}"


def open_path(path: str) -> str:
    """Open a file or folder with its Windows default application."""
    try:
        p = _resolve_user_path(path)
    except Exception as e:
        return f"Invalid path: {e}"

    if not p.exists():
        return f"I couldn't find {p}."

    try:
        os.startfile(str(p))
        return f"Opened {p}."
    except OSError as e:
        return f"I couldn't open {p}: {e}"


def find_files(name: str, search_root: str = "") -> str:
    """Find files/folders by name under a selected root."""
    query = (name or "").strip().lower()
    if not query:
        return "Tell me the file or folder name to search for."

    try:
        root = _resolve_user_path(search_root) if search_root.strip() else Path.home()
    except Exception as e:
        return f"Invalid search location: {e}"

    if not root.exists() or not root.is_dir():
        return f"{root} is not a valid search folder."

    matches = []
    try:
        for item in root.rglob("*"):
            if query in item.name.lower():
                matches.append(str(item))
                if len(matches) >= 25:
                    break
    except (OSError, PermissionError):
        pass

    if not matches:
        return f"I couldn't find anything matching '{name}' under {root}."

    suffix = " (showing the first 25)" if len(matches) >= 25 else ""
    return "Matches" + suffix + ": " + "; ".join(matches)


def get_system_summary() -> str:
    """Return basic Windows/Python/runtime information."""
    try:
        import platform
        return (
            f"Computer: {platform.node() or 'unknown'}; "
            f"Windows: {platform.platform()}; "
            f"CPU threads: {os.cpu_count() or 0}; "
            f"Python: {platform.python_version()}."
        )
    except Exception as e:
        return f"I couldn't read system information: {e}"


# Some apps are commonly called something that doesn't literally appear
# in their window title (e.g. VS Code's title says "Visual Studio Code",
# never "vs code"). Map spoken shorthand to the real title text.
WINDOW_TITLE_ALIASES = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
}


def _find_window(app_name: str):
    """Find an open window matching app_name, tried in order: exact
    title match, alias-resolved match, substring match, then
    all-significant-words-present match."""
    import pygetwindow as gw
    key = app_name.strip().lower()
    candidates = [key]
    if key in WINDOW_TITLE_ALIASES:
        candidates.append(WINDOW_TITLE_ALIASES[key])

    titles = [t for t in gw.getAllTitles() if t.strip()]

    for cand in candidates:
        for title in titles:
            if title.lower() == cand:
                wins = gw.getWindowsWithTitle(title)
                if wins:
                    return wins[0]

    for cand in candidates:
        for title in titles:
            title_l = title.lower()
            if cand in title_l or title_l in cand:
                wins = gw.getWindowsWithTitle(title)
                if wins:
                    return wins[0]

    for cand in candidates:
        words = [w for w in cand.split() if len(w) > 1]
        if not words:
            continue
        for title in titles:
            title_l = title.lower()
            if all(w in title_l for w in words):
                wins = gw.getWindowsWithTitle(title)
                if wins:
                    return wins[0]

    return None


def minimize_window(app_name: str) -> str:
    """Minimize the window of a currently open application, matched by
    its window title (e.g. 'vs code', 'chrome', 'notepad')."""
    win = _find_window(app_name)
    if not win:
        return f"I couldn't find an open window matching '{app_name}'."
    win.minimize()
    return f"Minimized {app_name}."


def restore_window(app_name: str) -> str:
    """Restore (un-minimize) a window that was previously minimized,
    matched by its window title."""
    win = _find_window(app_name)
    if not win:
        return f"I couldn't find a window matching '{app_name}' to restore."
    win.restore()
    return f"Restored {app_name}."


def create_file(filename: str, content: str = "") -> str:
    """Create a text file with the given content inside the user's
    Jarvis_Files folder on the Desktop. filename should include an
    extension, e.g. 'notes.txt'.
    """
    safe_name = os.path.basename(filename)  # prevent path traversal
    path = os.path.join(JARVIS_FILES_DIR, safe_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Created {safe_name} in Jarvis_Files."


def list_files() -> str:
    """List the files currently saved in the user's Jarvis_Files folder."""
    files = os.listdir(JARVIS_FILES_DIR)
    if not files:
        return "Jarvis_Files is empty."
    return "Files: " + ", ".join(files)


_WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    80: "light showers", 81: "showers", 82: "heavy showers",
    95: "thunderstorm",
}


def get_weather(city: str, day: str = "today") -> str:
    """Get the weather forecast for a named city. `day` should be
    'today' or 'tomorrow'."""
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
            timeout=10,
        ).json()
        results = geo.get("results")
        if not results:
            return f"I couldn't find a location named '{city}'."
        place = results[0]
        lat, lon = place["latitude"], place["longitude"]
        resolved_name = place.get("name", city)
        country = place.get("country", "")

        forecast = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
                "timezone": "auto",
                "forecast_days": 2,
            },
            timeout=10,
        ).json()

        daily = forecast.get("daily", {})
        idx = 0 if day.lower().strip() != "tomorrow" else 1
        if not daily.get("time") or idx >= len(daily["time"]):
            return f"I couldn't get a forecast for {resolved_name}."

        hi = daily["temperature_2m_max"][idx]
        lo = daily["temperature_2m_min"][idx]
        rain_chance = daily["precipitation_probability_max"][idx]
        code = daily["weathercode"][idx]
        condition = _WEATHER_CODES.get(code, "changeable conditions")

        label = "Today" if idx == 0 else "Tomorrow"
        location = f"{resolved_name}, {country}" if country else resolved_name
        return (
            f"{label} in {location}: {condition}, high of {hi}°C, low of {lo}°C, "
            f"{rain_chance}% chance of rain."
        )
    except Exception as e:
        return f"Weather lookup failed: {e}"


def web_search(query: str) -> str:
    """Look up a quick factual answer for a query on the web."""
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10,
        )
        data = resp.json()
        answer = data.get("AbstractText") or data.get("Answer")
        if answer:
            return answer
        related = data.get("RelatedTopics", [])
        if related and isinstance(related[0], dict) and related[0].get("Text"):
            return related[0]["Text"]
        return "I couldn't find a quick answer for that — try rephrasing the question."
    except Exception as e:
        return f"Search failed: {e}"


def remember(fact: str) -> str:
    """Save a fact about the user or their preferences permanently, so
    Jarvis still knows it in future sessions (e.g. their name, a
    preference, an ongoing project detail). Keep facts short and specific.
    """
    facts = load_memory()
    facts.append(fact)
    save_memory(facts)
    return f"Got it, I'll remember: {fact}"


def view_memory() -> str:
    """Show everything Jarvis currently remembers about the user
    permanently across sessions."""
    facts = load_memory()
    if not facts:
        return "I don't have any permanent memories saved yet."
    return "Here's what I remember: " + "; ".join(facts)


def forget_all_memory() -> str:
    """Erase all permanently remembered facts. Only use this if the user
    explicitly asks to forget everything or clear memory."""
    save_memory([])
    return "All permanent memories cleared."


TOOLS = [
    open_app, close_app, minimize_window, restore_window,
    create_file, list_files,
    get_storage_info, get_path_info, list_directory, open_path, find_files,
    get_system_summary,
    web_search, get_weather,
    remember, view_memory, forget_all_memory,
]

# ---------- Gemini client (new `google-genai` SDK) ----------
# `client` owns the HTTP transport and its timeout. 20_000ms = 20s per
# request, matching the old hard-timeout behavior — without this, a
# stalled connection to Gemini's servers can hang indefinitely with no
# output at all.
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=20_000),
)

_saved_facts = load_memory()
_memory_context = (
    "Here is what you already remember about the user from past sessions:\n- "
    + "\n- ".join(_saved_facts)
    if _saved_facts
    else "You don't have any permanent memories about the user yet."
)

SYSTEM_PRIMER = (
    "You are Jarvis, a concise, helpful personal assistant running on the "
    "user's Windows PC. You have tools to open/close a specific set of "
    "apps, inspect storage and basic system information, inspect/list/find/open "
"files and folders, create/list files in the user's Jarvis_Files folder, do quick "
    "web searches, and remember facts about the user permanently across "
    "sessions (use `remember` when the user shares something worth "
    "keeping, like their name or a lasting preference — don't ask "
    "permission first, just do it naturally).\n\n"
    "IMPORTANT: only say you did something if you actually called a tool "
    "that did it. You can open apps, close running apps, minimize windows, "
    "and restore minimized windows — but nothing beyond your listed tools "
    "(e.g. you cannot resize, move, or switch focus between windows). If "
    "asked to do something you have no tool for, say plainly that you "
    "can't do that yet — never substitute a different action and pass it "
    "off as what was asked, and never claim an action succeeded if no "
    "tool was called.\n\n"
    "Keep spoken replies short and natural, since you are speaking out "
    "loud, not writing an essay.\n\n" + _memory_context
)

# Passing plain Python functions as `tools` gives us automatic function
# calling (same behavior as the old SDK's enable_automatic_function_calling),
# and system_instruction here replaces the old "prime the chat with a
# throwaway send_message()" trick — one less startup API call.
chat = client.chats.create(
    model="gemini-flash-lite-latest",
    config=types.GenerateContentConfig(
        tools=TOOLS,
        system_instruction=SYSTEM_PRIMER,
    ),
)

recognizer = sr.Recognizer()
recognizer.dynamic_energy_threshold = True
recognizer.pause_threshold = 0.8
with sr.Microphone() as _src:
    print("[Jarvis] Calibrating for ambient noise...")
    recognizer.adjust_for_ambient_noise(_src, duration=1.5)


def listen_once(prompt: str = "", timeout=None, phrase_time_limit=None) -> str:
    """Capture one utterance from the mic and convert to text."""
    with sr.Microphone() as source:
        if prompt:
            print(prompt)
        try:
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
        except sr.WaitTimeoutError:
            return ""

    try:
        _t0 = time.time()
        text = recognizer.recognize_google(audio)
        print(f"[timing] STT took {time.time() - _t0:.2f}s")
        print(f"[You] {text}")
        return text
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        print(f"[Jarvis] Speech recognition error: {e}")
        return ""


# ---------- Typed input, running alongside voice ----------
# One persistent background thread reads stdin lines for the whole
# program's life and drops non-empty ones onto a queue. get_input() then
# races "a typed line showed up" against "the mic finished listening" and
# returns whichever happens first, so you can talk to Jarvis OR just type
# a command and hit Enter, turn by turn, with no mode switch needed.
_text_queue = queue.Queue()


def _stdin_reader() -> None:
    while True:
        try:
            line = input()
        except EOFError:
            break
        line = line.strip()
        if line:
            _text_queue.put(line)


threading.Thread(target=_stdin_reader, daemon=True).start()


def get_input(prompt: str = "", timeout=None, phrase_time_limit=None):
    """Wait for either a typed line or a spoken utterance, whichever
    arrives first. Returns (text, source) where source is 'voice' or
    'text'. text is '' if the mic listen timed out / heard nothing
    usable (typed input has no timeout — it just waits alongside voice).
    """
    # A typed line queued up earlier (e.g. while Jarvis was speaking)
    # counts as "already arrived" — hand it back immediately.
    try:
        typed = _text_queue.get_nowait()
        print(f"[You] {typed}")
        return typed, "text"
    except queue.Empty:
        pass

    print("[Jarvis] (speak, or type a command and press Enter)")
    local_voice_queue = queue.Queue()
    threading.Thread(
        target=lambda: local_voice_queue.put(
            listen_once(prompt=prompt, timeout=timeout, phrase_time_limit=phrase_time_limit)
        ),
        daemon=True,
    ).start()

    while True:
        try:
            typed = _text_queue.get_nowait()
            print(f"[You] {typed}")
            return typed, "text"
        except queue.Empty:
            pass
        try:
            spoken = local_voice_queue.get(timeout=0.2)
            return spoken, "voice"
        except queue.Empty:
            continue


def ask_gemini(text: str) -> str:
    """Send text to Gemini (which may call tools) and return its reply.
    Retries a couple of times with a short wait if the free-tier rate
    limit is hit or the connection stalls, instead of crashing or
    hanging silently."""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        _t0 = time.time()
        try:
            response = chat.send_message(text)
            print(f"[timing] Gemini took {time.time() - _t0:.2f}s")
            reply = response.text.strip()
            print(f"[Jarvis] {reply}")
            return reply
        except genai_errors.ClientError as e:
            # 429 = rate limit hit on the free tier.
            if getattr(e, "code", None) == 429:
                if attempt == max_attempts:
                    msg = "I've hit my request limit for now — give it a minute and try again."
                    print(f"[Jarvis] {msg}")
                    return msg
                wait = 15 * attempt
                print(f"[Jarvis] Rate limit hit, waiting {wait}s before retrying...")
                time.sleep(wait)
                continue
            print(f"[Jarvis] [ERROR] Gemini call failed: {type(e).__name__}: {e}")
            return "Sorry, I hit an error processing that — could you say it again?"
        except Exception as e:
            # The transport layer doesn't expose one single clean timeout
            # class, so we detect it by name/message instead — this still
            # catches DeadlineExceeded, ReadTimeout, socket timeouts, etc.
            is_timeout = "timeout" in type(e).__name__.lower() or "timeout" in str(e).lower()
            elapsed = time.time() - _t0
            if is_timeout:
                if attempt == max_attempts:
                    msg = "My connection to the AI backend keeps stalling — check your network and try again."
                    print(f"[Jarvis] {msg}")
                    return msg
                print(f"[Jarvis] Gemini call timed out after {elapsed:.1f}s (attempt {attempt}), retrying...")
                continue
            print(f"[Jarvis] [ERROR] Gemini call failed: {type(e).__name__}: {e}")
            return "Sorry, I hit an error processing that — could you say it again?"


def speak(text: str) -> None:
    """Convert text to speech via ElevenLabs and play it."""
    if not ELEVENLABS_API_KEY or ELEVENLABS_API_KEY == "your_elevenlabs_api_key_here":
        print("[Jarvis] (voice not configured yet — text only)")
        return

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    _t0 = time.time()
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    print(f"[timing] ElevenLabs TTS took {time.time() - _t0:.2f}s")
    if resp.status_code != 200:
        print(f"[Jarvis] TTS error {resp.status_code}: {resp.text[:200]}")
        return

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(resp.content)
        temp_path = f.name

    playsound(temp_path)
    os.remove(temp_path)


def _is_exit_phrase(text: str) -> bool:
    lower = text.lower().strip()
    return any(phrase in lower for phrase in EXIT_PHRASES) or lower in ("goodbye", "quit", "stop")


def main() -> None:
    print("=== Jarvis ===")
    print(f"Say '{WAKE_WORD}' to activate, or just type a command and press "
          f"Enter. Say/type 'goodbye jarvis' to exit.\n")
    write_status("idle")

    while True:
        write_status("listening")
        heard, source = get_input(timeout=None, phrase_time_limit=6)
        if not heard:
            continue

        if source == "text":
            # Typing is already an explicit action — no wake word needed.
            if _is_exit_phrase(heard):
                speak("Goodbye.")
                print("Shutting down. Goodbye.")
                write_status("idle")
                break
            command_text = heard
        else:
            lower = heard.lower()
            if WAKE_WORD not in lower:
                continue

            if _is_exit_phrase(lower):
                speak("Goodbye.")
                print("Shutting down. Goodbye.")
                write_status("idle")
                break

            # If they said more than just the wake word in the same
            # breath, treat the remainder as the command.
            remainder = lower.split(WAKE_WORD, 1)[1].strip(" ,.")
            if remainder:
                command_text = remainder
            else:
                speak("Yes?")
                command_text, _ = get_input(
                    prompt="[Jarvis] Listening for your command...",
                    timeout=6,
                    phrase_time_limit=10,
                )
                if not command_text:
                    continue

        write_status("thinking", last_heard=command_text)
        reply = ask_gemini(command_text)
        write_status("speaking", last_heard=command_text, last_reply=reply)
        speak(reply)

        # Stay "awake" for a bit so the user doesn't have to repeat the
        # wake word for follow-up commands in the same conversation.
        while True:
            write_status("listening", last_heard=command_text, last_reply=reply)
            follow_up, _ = get_input(
                prompt="[Jarvis] (still listening — no need to say 'jarvis' again; you can also just type)",
                timeout=8,
                phrase_time_limit=10,
            )
            if not follow_up:
                # Silence for a while -> go back to waiting for the wake word.
                break

            if _is_exit_phrase(follow_up):
                speak("Goodbye.")
                print("Shutting down. Goodbye.")
                write_status("idle")
                sys.exit(0)

            write_status("thinking", last_heard=follow_up)
            reply = ask_gemini(follow_up)
            write_status("speaking", last_heard=follow_up, last_reply=reply)
            speak(reply)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutting down. Goodbye.")
