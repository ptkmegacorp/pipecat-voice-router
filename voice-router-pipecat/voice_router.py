#!/usr/bin/env python3
import json, subprocess, sys, urllib.parse
from pathlib import Path

from routing import route_text

CONFIG = json.loads(Path(__file__).with_name('router_config.json').read_text())

def get_focused_window():
    try:
        wid = subprocess.check_output(['xdotool', 'getactivewindow'], text=True).strip()
        name = subprocess.check_output(['xdotool', 'getwindowname', wid], text=True).strip()
        klass = ''
        prop = subprocess.check_output(['xprop', '-id', wid, 'WM_CLASS'], text=True).strip()
        if '=' in prop:
            vals = prop.split('=', 1)[1].strip()
            parts = [p.strip().strip('"') for p in vals.split(',')]
            klass = parts[-1] if parts else ''
        return {'name': name, 'class': klass}
    except Exception:
        return {'name': '', 'class': ''}

def scroll_urxvt_window(wid, direction, repeat=4):
    key = 'shift+Next' if direction == 'down' else 'shift+Prior'
    subprocess.run(
        ['xdotool', 'key', '--window', wid, '--delay', '40', '--repeat', str(repeat), key],
        check=False,
    )


def is_urxvt_like(klass, title):
    klass = (klass or '').lower()
    title = (title or '').lower()
    return (
        'urxvt' in klass
        or 'rxvt' in klass
        or klass in {'terminal', 'xterm'}
        or 'pig-io-overlay' in title
    )


def scroll(direction, context=None):
    win = (context or {}).get('focused_window') or get_focused_window()
    klass, title = win.get('class') or '', win.get('name') or ''
    if is_urxvt_like(klass, title):
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
    subprocess.run(
        ['i3-msg', '[title="^pig-io-overlay$"]', 'move to workspace current, sticky enable, focus'],
        check=False,
    )


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
