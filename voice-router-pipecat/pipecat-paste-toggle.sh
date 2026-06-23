#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="$HOME/.cache/pipecat-paste"
PID_FILE="$STATE_DIR/arecord.pid"
WAV_FILE="$STATE_DIR/utterance.wav"
OUT_BASE="$STATE_DIR/transcript"
OUT_TXT="$OUT_BASE.txt"
LOG_FILE="$STATE_DIR/pipecat-paste.log"
STATUS="/home/bot/pipecat-voice-router/voice-router-pipecat/voice_status.py"
MIC_DEVICE="${PIPECAT_PASTE_MIC_DEVICE:-plughw:2,0}"
WHISPER_BIN="${PIPECAT_PASTE_WHISPER_BIN:-/home/bot/whisper.cpp/build/bin/whisper-cli}"
WHISPER_MODEL="${PIPECAT_PASTE_WHISPER_MODEL:-/home/bot/whisper.cpp/models/ggml-base.en.bin}"

mkdir -p "$STATE_DIR"
log() { printf '%s %s\n' "$(date -Is)" "$*" >> "$LOG_FILE"; }
set_status() { "$STATUS" "$@" >/dev/null 2>&1 || true; }
notify() { notify-send "Pipecat paste" "$1" >/dev/null 2>&1 || true; }

active_window_id() {
  xdotool getactivewindow 2>/dev/null || true
}

active_window_class() {
  local wid
  wid="$(active_window_id)"
  [ -n "$wid" ] || return 0
  xprop -id "$wid" WM_CLASS 2>/dev/null | sed -E 's/.*= //; s/"//g; s/,.*//; s/.*/\L&/'
}

active_window_title() {
  local wid
  wid="$(active_window_id)"
  [ -n "$wid" ] || return 0
  xdotool getwindowname "$wid" 2>/dev/null | tr '[:upper:]' '[:lower:]'
}

paste_text() {
  local text="$1"
  local old_clip=""
  local old_primary=""
  local klass=""
  local title=""
  old_clip="$(xclip -selection clipboard -o 2>/dev/null || true)"
  old_primary="$(xclip -selection primary -o 2>/dev/null || true)"
  printf '%s' "$text" | xclip -selection clipboard -i
  printf '%s' "$text" | xclip -selection primary -i
  klass="$(active_window_class)"
  title="$(active_window_title)"
  log "paste target class=$klass title=$title"
  if [[ "$title" == *pig-io-overlay* ]]; then
    # pig-io overlay is a raw terminal TUI; bracketed paste from the terminal
    # does not reliably reach its Input widget, so type the transcript directly.
    xdotool type --clearmodifiers --delay 1 -- "$text"
    sleep 0.2
    printf '%s' "$old_clip" | xclip -selection clipboard -i
    printf '%s' "$old_primary" | xclip -selection primary -i
    return
  fi
  case "$klass" in
    *urxvt*|*rxvt*|*xterm*|*terminal*)
      xdotool key --clearmodifiers Shift+Insert
      ;;
    *)
      xdotool key --clearmodifiers ctrl+v
      ;;
  esac
  sleep 1
  printf '%s' "$old_clip" | xclip -selection clipboard -i
  printf '%s' "$old_primary" | xclip -selection primary -i
}

finish_recording() {
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -INT "$pid" 2>/dev/null || true
    sleep 0.25
    kill -TERM "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE" "$OUT_TXT"

  set_status mode transcribing
  notify "Transcribing..."
  log "transcribing $WAV_FILE"
  "$WHISPER_BIN" -m "$WHISPER_MODEL" -f "$WAV_FILE" -l en -t 4 -np -otxt -of "$OUT_BASE" >> "$LOG_FILE" 2>&1 || {
    set_status mode error
    notify "Transcription failed"
    exit 1
  }

  local text
  text="$(tr '\n' ' ' < "$OUT_TXT" | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')"
  if [ -z "$text" ]; then
    set_status mode idle
    notify "No transcript"
    exit 0
  fi

  paste_text "$text"
  set_status mode idle
  notify "Pasted transcript"
  log "pasted: $text"
}

start_recording() {
  rm -f "$WAV_FILE" "$OUT_TXT"
  set_status profile "pipecat paste"
  set_status enabled on
  set_status mode recording
  set_status hearing on
  notify "Recording. Press Ctrl+Space again to paste."
  log "recording from $MIC_DEVICE to $WAV_FILE"
  arecord -q -D "$MIC_DEVICE" -f S16_LE -r 16000 -c 1 -t wav "$WAV_FILE" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
}

if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  set_status hearing off
  finish_recording
else
  start_recording
fi
