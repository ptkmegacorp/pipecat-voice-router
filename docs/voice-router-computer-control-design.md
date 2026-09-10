# Voice Router + Computer Control Design

This note describes the desired voice-control architecture for Pi/Pig.

Good framework options:

- **Pipecat**: best for custom voice pipelines and inserting our own router between STT and LLM.
- **LiveKit Agents**: best for polished realtime voice sessions, VAD, turn detection, and browser/client audio.

The key idea:

> Voice framework handles audio. Our router decides whether the text becomes a computer action, a browser action, a main LLM task, or a spoken conversation.

---

## High-level pipeline

```text
microphone
→ VAD
→ optional wake word
→ ASR
→ end-of-turn detection
→ text router
→ one of:
   1. direct computer-control function
   2. browser/app automation function
   3. main LLM / Pig orchestrator
   4. conversational LLM + TTS
→ optional TTS response
```

The important part is that **TTS is conditional**.

Some commands should be silent and immediate:

```text
"scroll down"
"open youtube and search for x"
"switch window"
"close tab"
```

Other commands should produce spoken output:

```text
"search for x and tell me about it"
"what is this repo doing?"
"explain the difference between routing and orchestration"
```

---

## Pipecat as an option

Pipecat is a good fit because it is a Python pipeline framework.

Conceptually:

```text
STT output frame
→ custom router processor
→ call local function / call Pig / call TTS path
```

This makes it natural to add a small router in front of the main model.

Use Pipecat if we want:

- local-first voice stack
- custom routing code
- custom functions for i3, Firefox, Pig, terminal, etc.
- easy insertion point between STT and LLM
- explicit control over when TTS happens

---

## LiveKit Agents as an option

LiveKit Agents is a good fit if we want polished realtime voice sessions.

Conceptually:

```text
LiveKit session
→ Silero VAD
→ LiveKit turn detector
→ STT final transcript
→ route_transcript(text, context)
→ selected action
```

Use LiveKit if we want:

- browser/client voice UI
- strong realtime session handling
- turn detection
- wake-word examples
- plugin-style STT/LLM/TTS
- more production-like voice agent behavior

---

# Router responsibilities

The router decides:

1. Is this a direct computer-control command?
2. Is this an app/browser automation command?
3. Is this a main LLM research/task command?
4. Should the assistant speak a TTS response?
5. Which function/model/tool should run?
6. Does the currently focused window matter?

Router input:

```json
{
  "text": "scroll down",
  "focused_app": "firefox",
  "focused_window_title": "YouTube - Mozilla Firefox",
  "desktop": "i3",
  "asr_confidence": 0.92
}
```

Router output:

```json
{
  "route": "computer_control",
  "function": "scroll",
  "args": {"direction": "down", "amount": "normal"},
  "tts": false
}
```

---

# Main route types

## 1. Direct computer control

Examples:

```text
scroll up
scroll down
page up
page down
click
close tab
switch window
focus terminal
```

These should usually be:

```json
{"tts": false}
```

Reason: the user asked for an action, not a conversation.

### Focus-aware behavior

The command `scroll down` should do different things depending on the focused app.

Examples:

```text
focused app = Pig/Pi terminal ledger
→ send key/mouse event that scrolls the ledger

focused app = Firefox
→ scroll webpage down

focused app = code editor
→ scroll editor buffer down
```

This means the router needs context:

- focused window class
- focused window title
- active i3 workspace
- maybe focused terminal app/mode

Possible Linux tools:

- `i3-msg -t get_tree` for focused window
- `xdotool getactivewindow getwindowname getwindowclassname`
- `ydotool` or `xdotool` for input events
- app-specific APIs where available

---

## 2. Browser/app automation

Example:

```text
open youtube and search for X
```

Desired behavior:

1. Router recognizes this as a browser automation command.
2. Router calls a function to open a new i3 Firefox window at YouTube.
3. Main LLM or browser-control agent enters/searches the query.
4. No TTS response.

Why no TTS:

```text
The computer action itself is the response.
```

Router output:

```json
{
  "route": "browser_automation",
  "function": "open_youtube_search",
  "args": {"query": "small orchestrator models"},
  "use_main_llm_for_browser_entry": true,
  "tts": false
}
```

---

## Specific scenario: "open youtube and search for X"

User says:

```text
open youtube and search for liquid ai lfm 2.5
```

Pipeline:

```text
voice
→ ASR final text
→ router classifies browser automation
→ function opens new i3 window with Firefox at YouTube
→ main LLM/browser actor enters query into YouTube search field
→ no TTS
```

### Practical implementation plan

There are two possible implementations.

## Option A: direct URL construction, no LLM needed

For YouTube search, the simplest and most reliable path is to skip UI typing entirely:

```text
https://www.youtube.com/results?search_query=liquid+ai+lfm+2.5
```

Function:

```python
import subprocess
import urllib.parse


def open_youtube_search(query: str):
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    subprocess.Popen(["i3-msg", "exec", f"firefox --new-window '{url}'"])
```

Router output:

```json
{
  "route": "browser_automation",
  "function": "open_youtube_search_url",
  "args": {"query": "liquid ai lfm 2.5"},
  "tts": false
}
```

This is preferred if the target site has a known URL pattern.

## Option B: open YouTube, then main LLM controls page

Use this when direct URL construction is not enough.

Steps:

```text
1. open new Firefox window at https://youtube.com
2. wait for page load
3. main LLM/browser actor receives task:
   "search YouTube for: liquid ai lfm 2.5"
4. browser actor clicks/focuses search box
5. browser actor types query
6. browser actor presses Enter
7. no TTS
```

Pseudo-code:

```python
import subprocess
import time


def open_firefox_youtube():
    subprocess.Popen(["i3-msg", "exec", "firefox --new-window https://youtube.com"])
    time.sleep(2)


def ask_browser_actor_to_search(query: str):
    # This could call Pig/main LLM with browser-control tools.
    return call_main_llm_browser_actor({
        "task": "search_youtube",
        "query": query,
        "instructions": [
            "Use the currently focused Firefox YouTube window.",
            "Focus the search box.",
            "Enter the query.",
            "Submit the search.",
            "Do not produce TTS. The browser action is the response."
        ]
    })


def open_youtube_and_search_with_llm(query: str):
    open_firefox_youtube()
    ask_browser_actor_to_search(query)
```

This is more general, but also more fragile than direct URL construction.

---

## 3. Main LLM research/task route

Example:

```text
search for X and tell me about it
```

Desired behavior:

1. Router recognizes this is not just browser automation.
2. It routes to main LLM/Pig orchestrator.
3. Main LLM can use web search, extraction, local files, etc.
4. The final answer should be spoken with TTS, and maybe also printed.

Router output:

```json
{
  "route": "main_llm_research",
  "function": "ask_pig",
  "args": {"prompt": "search for X and tell me about it"},
  "tools_allowed": ["web_search", "web_extract", "read", "grep", "find"],
  "tts": true
}
```

Difference from YouTube command:

```text
"open youtube and search for X"
→ computer/browser action
→ no TTS

"search for X and tell me about it"
→ research/answer task
→ TTS yes
```

---

# Example router rules

These can start as simple deterministic rules before using a tiny model.

```python
def route_text(text: str, context: dict) -> dict:
    t = text.lower().strip()

    if t in {"scroll down", "scroll up", "page down", "page up"}:
        direction = "down" if "down" in t else "up"
        return {
            "route": "computer_control",
            "function": "scroll",
            "args": {"direction": direction, "context": context},
            "tts": False,
        }

    if t.startswith("open youtube and search for "):
        query = t.removeprefix("open youtube and search for ").strip()
        return {
            "route": "browser_automation",
            "function": "open_youtube_search_url",
            "args": {"query": query},
            "tts": False,
        }

    if t.startswith("search for ") and "tell me about" in t:
        return {
            "route": "main_llm_research",
            "function": "ask_pig",
            "args": {"prompt": text},
            "tts": True,
        }

    return {
        "route": "conversation",
        "function": "ask_local_llm",
        "args": {"prompt": text},
        "tts": True,
    }
```

Later this deterministic router can be replaced or backed up by a tiny model:

- FunctionGemma 270M
- LFM2.5-350M
- MiniCPM5-1B
- Qwen-0.8B-AgentJSON
- xLAM-1B

---

# Focus-aware scroll function

Pseudo-code:

```python
import subprocess
import json


def get_focused_window():
    # Option 1: use xdotool
    name = subprocess.check_output([
        "xdotool", "getactivewindow", "getwindowname"
    ], text=True).strip()

    klass = subprocess.check_output([
        "xdotool", "getactivewindow", "getwindowclassname"
    ], text=True).strip()

    return {"name": name, "class": klass}


def scroll(direction: str, context: dict | None = None):
    win = context.get("focused_window") if context else get_focused_window()
    klass = (win.get("class") or "").lower()
    title = (win.get("name") or "").lower()

    if "firefox" in klass:
        key = "Page_Down" if direction == "down" else "Page_Up"
        subprocess.run(["xdotool", "key", key])
        return

    if "terminal" in klass or "pig" in title or "pi" in title:
        # For a terminal ledger, mouse wheel events may work better than PageUp/PageDown.
        button = "5" if direction == "down" else "4"
        subprocess.run(["xdotool", "click", "--repeat", "5", button])
        return

    # Fallback
    key = "Page_Down" if direction == "down" else "Page_Up"
    subprocess.run(["xdotool", "key", key])
```

This is the shape we want:

```text
same voice phrase
→ different action depending on focused app/window
```

---

# Router output schema

A useful standard JSON shape:

```json
{
  "route": "computer_control | browser_automation | main_llm_research | conversation | ignore",
  "function": "string_function_name",
  "args": {},
  "tts": true,
  "requires_main_llm": false,
  "requires_browser_actor": false,
  "confidence": 0.0,
  "notes": "optional"
}
```

This lets the voice stack handle the same event consistently.

---

# How it fits with Pipecat

```text
Pipecat STT final frame
→ route_text(text, desktop_context)
→ if route computer_control: call function, drop TTS
→ if route browser_automation: call browser function, drop TTS
→ if route main_llm_research: call Pig, send result to TTS
→ if route conversation: call local LLM, send result to TTS
```

Pipecat is good because this router can be a custom processor.

---

# How it fits with LiveKit Agents

```text
LiveKit user turn committed
→ route_text(text, desktop_context)
→ action branch
→ optionally publish TTS/audio reply
```

LiveKit is good because it gives cleaner realtime session/turn infrastructure.

---

# First implementation target

Start with a deterministic router for the obvious phrases:

1. `scroll up`
2. `scroll down`
3. `open youtube and search for X`
4. `search for X and tell me about it`

Then add a tiny model router once the function schema is stable.

Initial routing table:

| Phrase | Route | Function | TTS |
|---|---|---|---|
| `scroll up` | `computer_control` | `scroll(up)` | no |
| `scroll down` | `computer_control` | `scroll(down)` | no |
| `open youtube and search for X` | `browser_automation` | `open_youtube_search_url(X)` | no |
| `search for X and tell me about it` | `main_llm_research` | `ask_pig(prompt)` | yes |
| normal conversation | `conversation` | `ask_local_llm(prompt)` | yes |

---

# Important design principle

Do not make the main LLM handle every voice command.

For computer control:

```text
voice → router → function
```

For research/conversation:

```text
voice → router → main LLM/orchestrator → TTS
```

This keeps simple commands fast, silent, and reliable while still allowing complex questions to go to the main model.
