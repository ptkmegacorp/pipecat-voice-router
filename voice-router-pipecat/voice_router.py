#!/usr/bin/env python3
import json, subprocess, sys, urllib.parse
from pathlib import Path

from routing import route_text

CONFIG = json.loads(Path(__file__).with_name('router_config.json').read_text())

def get_focused_window():
    try:
        name = subprocess.check_output(['xdotool','getactivewindow','getwindowname'], text=True).strip()
        klass = subprocess.check_output(['xdotool','getactivewindow','getwindowclassname'], text=True).strip()
        return {'name': name, 'class': klass}
    except Exception:
        return {'name': '', 'class': ''}

def find_pig_overlay_window():
    try:
        out = subprocess.check_output(
            ['xdotool', 'search', '--onlyvisible', '--name', '^pig-io-overlay$'],
            text=True,
        ).strip()
        ids = [wid for wid in out.splitlines() if wid.strip()]
        return ids[-1] if ids else None
    except Exception:
        return None


def scroll_urxvt_window(wid, direction, repeat=4):
    key = 'shift+Next' if direction == 'down' else 'shift+Prior'
    subprocess.run(
        ['xdotool', 'key', '--window', wid, '--delay', '40', '--repeat', str(repeat), key],
        check=False,
    )


def scroll(direction, context=None):
    overlay_wid = find_pig_overlay_window()
    if overlay_wid:
        scroll_urxvt_window(overlay_wid, direction)
        return

    win = (context or {}).get('focused_window') or get_focused_window()
    klass, title = (win.get('class') or '').lower(), (win.get('name') or '').lower()
    if 'urxvt' in klass or 'rxvt' in klass or 'terminal' in klass or 'pig' in title or 'pi' in title:
        try:
            wid = subprocess.check_output(['xdotool', 'getactivewindow'], text=True).strip()
            scroll_urxvt_window(wid, direction)
        except Exception:
            pass
        return
    subprocess.run(['xdotool', 'key', 'Page_Down' if direction == 'down' else 'Page_Up'])

def make_full_screen():
    # Default i3 behavior: `fullscreen toggle` on the focused container.
    subprocess.run(['i3-msg', 'fullscreen', 'toggle'])


def exit_full_screen():
    # Default i3 behavior: explicitly disable fullscreen on the focused container.
    subprocess.run(['i3-msg', 'fullscreen', 'disable'])


def focus_direction(direction):
    if direction not in {'left', 'right', 'up', 'down'}:
        raise ValueError(f'invalid focus direction: {direction}')
    subprocess.run(['i3-msg', 'focus', direction], check=False)


def list_routed_commands():
    lines = ['Directly routed voice commands:']
    for r in CONFIG['routes']:
        examples = r.get('match') or [r.get('prefix', '').strip() + '...']
        lines.append(f"- {r['name']}: {', '.join(examples)} -> {r['function']} (tts={r.get('tts')})")
        if r.get('description'):
            lines.append(f"  {r['description']}")
    result = '\n'.join(lines)
    print(result)
    return result


def open_youtube_search_url(query):
    url = 'https://www.youtube.com/results?search_query=' + urllib.parse.quote_plus(query)
    subprocess.Popen(['i3-msg','exec',f'firefox --new-window {url}'])

def open_firefox():
    subprocess.Popen(['i3-msg', 'exec', 'firefox --new-window about:blank'])

def close_firefox():
    subprocess.run(['i3-msg', '[instance="firefox"] kill'], check=False)

def close_youtube():
    subprocess.run(['i3-msg', '[class="mpv"] kill'], check=False)
    subprocess.run(['pkill', '-x', 'mpv'], check=False)

def focus_pig_io_overlay():
    subprocess.run(['/home/bot/pig-io/overlay.sh', 'show'], check=False)
    subprocess.Popen(['i3-msg', '[title="^pig-io-overlay$"]', 'focus'])


def open_pig_io_overlay():
    subprocess.Popen(['/home/bot/pig-io/overlay.sh', 'show'])


def close_pig_io_overlay():
    subprocess.run(['/home/bot/pig-io/overlay.sh', 'hide'], check=False)

def ask_pig(prompt):
    print(f'ASK_PIG TODO: {prompt}')
    return ''

def ask_local_llm(prompt):
    print(f'ASK_LOCAL_LLM TODO: {prompt}')
    return ''

def execute_action(action):
    fn, args = action['function'], action.get('args', {})
    if fn == 'scroll': return scroll(args['direction'], action.get('context'))
    if fn == 'make_full_screen': return make_full_screen()
    if fn == 'exit_full_screen': return exit_full_screen()
    if fn == 'focus_direction': return focus_direction(args['direction'])
    if fn == 'list_routed_commands': return list_routed_commands()
    if fn == 'open_youtube_search_url': return open_youtube_search_url(args['query'])
    if fn == 'open_firefox': return open_firefox()
    if fn == 'close_firefox': return close_firefox()
    if fn == 'close_youtube': return close_youtube()
    if fn == 'open_pig_io_overlay': return open_pig_io_overlay()
    if fn == 'focus_pig_io_overlay': return focus_pig_io_overlay()
    if fn == 'close_pig_io_overlay': return close_pig_io_overlay()
    if fn == 'ask_pig': return ask_pig(args['prompt'])
    if fn == 'ask_local_llm': return ask_local_llm(args['prompt'])
    raise ValueError(fn)

if __name__ == '__main__':
    text = ' '.join(sys.argv[1:]) or 'scroll down'
    action = route_text(text, {'focused_window': get_focused_window()})
    print(json.dumps(action, indent=2))
    execute_action(action)
