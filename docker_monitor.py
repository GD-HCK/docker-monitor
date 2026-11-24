#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=8.2,<9"]
# ///
"""Docker Monitor: an interactive, colourful terminal dashboard for the local Docker stack.

Live container table grouped by compose project, CPU/memory bars and sparklines, streaming
logs, a live event feed and start/stop/restart/exec/remove actions. It only talks to the
`docker` CLI, so it works wherever `docker ps` works (Docker Desktop on Windows/macOS, Linux).

    python docker_monitor.py               # interactive dashboard (needs `textual`)
    python docker_monitor.py --snapshot    # one-shot coloured summary, then exit
    python docker_monitor.py --json        # one-shot machine-readable summary
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

__version__ = '0.1.0'

# Go template for `docker ps`: one JSON object per line with only the fields we use, and
# the compose labels pulled out directly (the .Labels string can't be split reliably).
PS_FORMAT = (
    '{"id":{{json .ID}},"name":{{json .Names}},"image":{{json .Image}},'
    '"state":{{json .State}},"status":{{json .Status}},"ports":{{json .Ports}},'
    '"created":{{json .CreatedAt}},"networks":{{json .Networks}},"mounts":{{json .Mounts}},'
    '"project":{{json (.Label "com.docker.compose.project")}},'
    '"service":{{json (.Label "com.docker.compose.service")}}}'
)
HISTORY = 90  # stats samples kept per container (about 1.5 minutes)
REFRESH_SECONDS = 3  # container list refresh; events trigger an earlier one
NO_PROJECT = '(standalone)'
STATE_STYLE = {
    'running': ('●', 'bold green'),
    'restarting': ('◐', 'bold yellow'),
    'paused': ('❚❚', 'bold cyan'),
    'created': ('○', 'bright_black'),
    'exited': ('○', 'red'),
    'dead': ('✖', 'bold red'),
    'removing': ('◌', 'yellow'),
}
HEALTH_STYLE = {
    'healthy': ('♥ healthy', 'green'),
    'unhealthy': ('✚ unhealthy', 'bold red'),
    'starting': ('… starting', 'yellow'),
}
SIZE_UNITS = {
    'b': 1,
    'kb': 1e3,
    'mb': 1e6,
    'gb': 1e9,
    'tb': 1e12,
    'kib': 1024,
    'mib': 1024**2,
    'gib': 1024**3,
    'tib': 1024**4,
}


# --------------------------------------------------------------------------- data layer


class DockerError(RuntimeError):
    pass


def docker(*args: str, timeout: int = 20) -> str:
    """Run a docker CLI command and return stdout; raise DockerError with stderr on failure."""
    try:
        result = subprocess.run(
            ['docker', *args],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DockerError('docker CLI not found on PATH') from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f'docker {args[0]} timed out after {timeout}s') from exc
    if result.returncode != 0:
        raise DockerError(result.stderr.strip() or f'docker {args[0]} exited {result.returncode}')
    return result.stdout


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip().startswith('{')]


def parse_size(text: str) -> float:
    match = re.match(r'\s*([\d.]+)\s*([a-zA-Z]*)', text or '')
    if not match:
        return 0.0
    return float(match.group(1)) * SIZE_UNITS.get(match.group(2).lower() or 'b', 1)


def human_bytes(value: float) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return f'{value:.0f}{unit}' if unit == 'B' else f'{value:.1f}{unit}'
        value /= 1024
    return f'{value:.1f}TiB'


def health_of(status: str) -> str:
    if '(unhealthy)' in status:
        return 'unhealthy'
    if '(healthy)' in status:
        return 'healthy'
    if 'health: starting' in status:
        return 'starting'
    return ''


@dataclass
class Container:
    id: str
    name: str
    image: str
    state: str
    status: str
    ports: str
    created: str
    networks: str
    mounts: str
    project: str
    service: str

    @property
    def health(self) -> str:
        return health_of(self.status)

    @property
    def uptime(self) -> str:
        # "Up 12 minutes (healthy)" -> "12 minutes"; "Exited (0) 3 hours ago" -> "exited 3 hours ago"
        text = re.sub(r'\s*\((healthy|unhealthy|health: starting)\)', '', self.status)
        return re.sub(r'^Up\s+', '', text)

    @property
    def short_ports(self) -> str:
        # Keep host->container mappings, drop duplicate IPv6 entries and bind addresses.
        seen: list[str] = []
        for part in self.ports.split(','):
            part = part.strip()
            if '->' not in part:
                continue
            host, target = part.split('->', 1)
            mapping = f'{host.rsplit(":", 1)[-1]}→{target.replace("/tcp", "")}'
            if mapping not in seen:
                seen.append(mapping)
        return ' '.join(seen)


@dataclass
class Stats:
    cpu: float = 0.0
    mem_used: float = 0.0
    mem_limit: float = 0.0
    mem_pct: float = 0.0
    net_io: str = ''
    block_io: str = ''
    pids: str = ''
    cpu_history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    mem_history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    updated: float = 0.0

    def update(self, row: dict) -> None:
        self.cpu = float(row.get('CPUPerc', '0').rstrip('%') or 0)
        used, _, limit = (row.get('MemUsage') or '0B / 0B').partition('/')
        self.mem_used, self.mem_limit = parse_size(used), parse_size(limit)
        self.mem_pct = float(row.get('MemPerc', '0').rstrip('%') or 0)
        self.net_io = row.get('NetIO', '')
        self.block_io = row.get('BlockIO', '')
        self.pids = row.get('PIDs', '')
        self.cpu_history.append(self.cpu)
        self.mem_history.append(self.mem_used / 1024**2)
        self.updated = time.monotonic()


def list_containers() -> list[Container]:
    rows = json_lines(docker('ps', '--all', '--no-trunc', '--format', PS_FORMAT))
    containers = []
    for row in rows:
        row['id'] = row['id'][:12]
        row['project'] = row.get('project') or NO_PROJECT
        containers.append(Container(**{k: row.get(k) or '' for k in Container.__dataclass_fields__}))
    return containers


def engine_info() -> dict:
    try:
        info = json.loads(docker('info', '--format', '{{json .}}', timeout=15))
    except (DockerError, json.JSONDecodeError):
        return {}
    return {
        'version': info.get('ServerVersion', '?'),
        'os': info.get('OperatingSystem', '?'),
        'cpus': info.get('NCPU') or os.cpu_count() or 1,
        'mem_total': info.get('MemTotal') or 0,
        'images': info.get('Images', 0),
        'context': os.environ.get('DOCKER_CONTEXT', ''),
    }


def redact_env(inspect: dict) -> dict:
    """Never show environment values: they routinely carry passwords and tokens."""
    config = inspect.get('Config') or {}
    if config.get('Env'):
        config['Env'] = [f'{item.split("=", 1)[0]}=•••' for item in config['Env']]
    return inspect


class StreamReader:
    """Runs a long-lived docker command (stats/events/logs) on a daemon thread, feeding each
    output line to a callback and restarting it if it dies (e.g. Docker Desktop restarts)."""

    def __init__(self, args: list[str], on_line, restart: bool = True):
        self.args, self.on_line, self.restart = args, on_line, restart
        self.process: subprocess.Popen | None = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> 'StreamReader':
        self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.process = subprocess.Popen(
                    ['docker', *self.args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                )
                for line in self.process.stdout:
                    if self.stopped.is_set():
                        break
                    self.on_line(line.rstrip('\n'))
            except Exception as exc:  # noqa: BLE001 - keep the stream alive whatever happens
                self.on_line(f'[stream error] {exc}')
            if not self.restart:
                break
            self.stopped.wait(3)

    def stop(self) -> None:
        self.stopped.set()
        if self.process and self.process.poll() is None:
            self.process.kill()


ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def stats_line_parser(store: dict[str, Stats], lock: threading.Lock):
    def parse(line: str) -> None:
        line = ANSI.sub('', line).strip()
        if not line.startswith('{'):
            return
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return
        with lock:
            store.setdefault(row.get('ID', '')[:12], Stats()).update(row)

    return parse


# --------------------------------------------------------------------------- snapshot mode


def snapshot(as_json: bool) -> int:
    try:
        containers = list_containers()
        stats_rows = json_lines(ANSI.sub('', docker('stats', '--no-stream', '--format', '{{json .}}', timeout=30)))
    except DockerError as exc:
        print(f'docker-monitor: {exc}', file=sys.stderr)
        return 1
    stats: dict[str, Stats] = {}
    for row in stats_rows:
        stats.setdefault(row.get('ID', '')[:12], Stats()).update(row)
    info = engine_info()

    if as_json:
        out = {
            'engine': info,
            'containers': [
                {
                    'name': c.name,
                    'id': c.id,
                    'project': c.project,
                    'service': c.service,
                    'image': c.image,
                    'state': c.state,
                    'health': c.health,
                    'status': c.status,
                    'ports': c.short_ports,
                    'cpu_pct': stats[c.id].cpu if c.id in stats else None,
                    'mem_bytes': stats[c.id].mem_used if c.id in stats else None,
                }
                for c in containers
            ],
        }
        print(json.dumps(out, indent=2))
        return 0

    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    # Piped output (e.g. read by Claude) would default to 80 columns and truncate cells.
    console = Console() if sys.stdout.isatty() else Console(width=120)
    running = sum(c.state == 'running' for c in containers)
    unhealthy = [c for c in containers if c.health == 'unhealthy']
    console.print(
        Text.assemble(
            ('🐳 Docker ', 'bold #2496ED'),
            (f'{info.get("version", "?")} ', 'bold'),
            (f'· {info.get("os", "")} · {info.get("cpus", "?")} CPUs · {human_bytes(info.get("mem_total", 0))}', 'dim'),
        )
    )
    console.print(
        Text.assemble(
            (f'● {running} running  ', 'bold green'),
            (f'○ {len(containers) - running} stopped  ', 'red' if len(containers) - running else 'dim'),
            (f'✚ {len(unhealthy)} unhealthy', 'bold red' if unhealthy else 'dim'),
        )
    )
    table = Table(box=box.ROUNDED, border_style='white', header_style='bold #2496ED', expand=False)
    for column in ('', 'STACK / SERVICE', 'STATE', 'CPU', 'MEM', 'PORTS', 'UPTIME'):
        table.add_column(column, no_wrap=True, min_width=1 if not column else min(len(column), 6))
    by_project: dict[str, list[Container]] = {}
    for c in containers:
        by_project.setdefault(c.project, []).append(c)
    for project in sorted(by_project, key=lambda p: (p == NO_PROJECT, p)):
        members = by_project[project]
        up = sum(c.state == 'running' for c in members)
        table.add_row(
            '',
            Text(f'▾ {project}', style='bold magenta'),
            Text(f'{up}/{len(members)} up', style='green' if up == len(members) else 'yellow'),
            '',
            '',
            '',
            '',
        )
        for c in sorted(members, key=lambda c: c.name):
            icon, style = STATE_STYLE.get(c.state, ('?', 'white'))
            label, hstyle = HEALTH_STYLE.get(c.health, (c.state, style))
            s = stats.get(c.id)
            table.add_row(
                Text(icon, style=style),
                f'  {c.service or c.name}',
                Text(label, style=hstyle),
                cpu_text(s.cpu, info.get('cpus', 1)) if s else Text('-', style='dim'),
                Text(human_bytes(s.mem_used), style=pct_style(s.mem_pct)) if s else Text('-', style='dim'),
                Text(c.short_ports, style='cyan'),
                Text(c.uptime, style='dim'),
            )
    console.print(table)
    return 0


def pct_style(pct: float) -> str:
    return 'bold red' if pct >= 85 else 'yellow' if pct >= 60 else 'green'


def cpu_text(cpu: float, cpus: int):
    from rich.text import Text

    return Text(f'{cpu:5.1f}%', style=pct_style(cpu / max(cpus, 1) * 100 if cpu > 100 else cpu))


def bar(pct: float, width: int = 10):
    """A coloured horizontal meter: green → yellow → red."""
    from rich.text import Text

    pct = max(0.0, min(pct, 100.0))
    filled = round(pct / 100 * width)
    text = Text('█' * filled, style=pct_style(pct))
    text.append('░' * (width - filled), style='#303030')
    return text


# --------------------------------------------------------------------------- interactive app


def build_app():
    """Build the Textual app (imports kept local so --snapshot/--json only need rich)."""
    from rich.syntax import Syntax
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.theme import Theme
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        RichLog,
        Sparkline,
        Static,
        TabbedContent,
        TabPane,
    )

    theme = Theme(
        name='docker-night',
        primary='#2496ED',
        secondary='#7AA2F7',
        accent='#FF9E64',
        warning='#E0AF68',
        error='#F7768E',
        success='#9ECE6A',
        foreground='#E6E6E6',
        background='#000000',
        surface='#000000',
        panel='#0D1117',
        dark=True,
    )

    LOG_LEVELS = [
        (re.compile(r'\b(FATAL|CRITICAL|PANIC|ERROR|ERR|Exception|Traceback)\b', re.I), 'bold red'),
        (re.compile(r'\b(WARN|WARNING)\b', re.I), 'yellow'),
        (re.compile(r'\b(DEBUG|TRACE)\b', re.I), 'bright_black'),
        (re.compile(r'\b(INFO)\b', re.I), 'cyan'),
    ]
    TS = re.compile(r'^(\d{4}-\d\d-\d\dT[\d:.]+Z?)\s')
    NAME_COLOURS = ['#7AA2F7', '#9ECE6A', '#FF9E64', '#BB9AF7', '#2AC3DE', '#E0AF68', '#F7768E']

    def colour_log(line: str, prefix: Text | None) -> Text:
        text = Text()
        if prefix is not None:
            text.append_text(prefix)
        match = TS.match(line)
        if match:
            text.append(match.group(1)[11:23] + ' ', style='#565F89')
            line = line[match.end() :]
        body = Text(ANSI.sub('', line))
        for pattern, style in LOG_LEVELS:
            if pattern.search(line):
                if style != 'cyan':  # errors, warnings and debug colour the whole line
                    body.stylize(style)
                body.highlight_regex(pattern, 'bold ' + style.replace('bold ', ''))
                break
        body.highlight_regex(r'\b\d{3}\b(?= )', 'magenta')  # HTTP status codes in access logs
        text.append_text(body)
        return text

    class ConfirmScreen(ModalScreen[bool]):
        BINDINGS = [Binding('y', 'answer(True)', 'Yes'), Binding('n,escape', 'answer(False)', 'No')]

        def __init__(self, question: str):
            super().__init__()
            self.question = question

        def compose(self) -> ComposeResult:
            with Vertical(id='dialog'):
                yield Label(self.question, id='question')
                with Horizontal(id='buttons'):
                    yield Button('Yes (y)', variant='error', id='yes')
                    yield Button('No (n)', variant='primary', id='no')

        def action_answer(self, value: bool) -> None:
            self.dismiss(value)

        @on(Button.Pressed)
        def pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == 'yes')

    class LogScreen(Screen):
        BINDINGS = [
            Binding('escape,q', 'app.pop_screen', 'Back'),
            Binding('f', 'toggle_follow', 'Follow'),
            Binding('w', 'toggle_wrap', 'Wrap'),
            Binding('c', 'clear', 'Clear'),
        ]

        def __init__(self, targets: list[tuple[str, str]], title: str):
            super().__init__()
            self.targets, self.title_text = targets, title
            self.readers: list[StreamReader] = []

        def compose(self) -> ComposeResult:
            yield Header()
            log = RichLog(id='logview', highlight=False, markup=False, wrap=True, max_lines=5000)
            log.border_title = f'📜 logs · {self.title_text}'
            log.border_subtitle = 'following'
            yield log
            yield Footer()

        def on_mount(self) -> None:
            log = self.query_one(RichLog)
            many = len(self.targets) > 1
            width = max(len(name) for _, name in self.targets)
            for index, (cid, name) in enumerate(self.targets):
                prefix = Text(f'{name:<{width}} │ ', style=NAME_COLOURS[index % len(NAME_COLOURS)]) if many else None

                def on_line(line: str, prefix=prefix) -> None:
                    self.app.call_from_thread(log.write, colour_log(line, prefix))

                tail = '100' if many else '400'
                self.readers.append(
                    StreamReader(
                        ['logs', '--follow', '--timestamps', '--tail', tail, cid],
                        on_line,
                        restart=False,
                    ).start()
                )

        def on_unmount(self) -> None:
            for reader in self.readers:
                reader.stop()

        def action_toggle_follow(self) -> None:
            log = self.query_one(RichLog)
            log.auto_scroll = not log.auto_scroll
            log.border_subtitle = 'following' if log.auto_scroll else 'paused (f to follow)'
            if log.auto_scroll:
                log.scroll_end(animate=False)

        def action_toggle_wrap(self) -> None:
            log = self.query_one(RichLog)
            log.wrap = not log.wrap

        def action_clear(self) -> None:
            self.query_one(RichLog).clear()

    class InspectScreen(Screen):
        BINDINGS = [Binding('escape,q', 'app.pop_screen', 'Back')]

        def __init__(self, cid: str, name: str):
            super().__init__()
            self.cid, self.name_text = cid, name

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll(id='inspect'):
                yield Static('loading…', id='inspect-body')
            yield Footer()

        def on_mount(self) -> None:
            self.query_one('#inspect').border_title = f'🔍 inspect · {self.name_text} (env values hidden)'
            self.load()

        @work(thread=True)
        def load(self) -> None:
            try:
                data = redact_env(json.loads(docker('inspect', self.cid))[0])
                body = Syntax(
                    json.dumps(data, indent=2), 'json', theme='monokai', background_color='#000000', line_numbers=True
                )
            except (DockerError, json.JSONDecodeError, IndexError) as exc:
                body = Text(str(exc), style='red')
            self.app.call_from_thread(self.query_one('#inspect-body', Static).update, body)

    class DockerMonitor(App):
        TITLE = '🐳 Docker Monitor'
        CSS = """
        Screen { background: #000000; }
        #cards { height: 5; padding: 0 1; }
        .card { width: 1fr; height: 5; border: round #FFFFFF; padding: 0 1; content-align: center middle;
                text-align: center; margin: 0 1 0 0; }
        .card:last-of-type { margin: 0; }
        TabbedContent { height: 1fr; }
        #main { height: 1fr; }
        #containers { width: 1fr; height: 1fr; border: round #FFFFFF; }
        #side { width: 46; border: round #FFFFFF; padding: 0 1; }
        #side.hidden { display: none; }
        #detail { height: auto; }
        .spark-label { color: $text-muted; margin-top: 1; }
        #cpu-spark { height: 4; }
        #cpu-spark > .sparkline--max-color { color: #F7768E; }
        #cpu-spark > .sparkline--min-color { color: #2496ED; }
        #mem-spark { height: 4; }
        #mem-spark > .sparkline--max-color { color: #BB9AF7; }
        #mem-spark > .sparkline--min-color { color: #2AC3DE; }
        DataTable { background: #000000; }
        DataTable > .datatable--header { background: #000000; color: #2496ED; text-style: bold; }
        DataTable > .datatable--cursor { background: #1F3B5C; }
        .plain-table { border: round #FFFFFF; height: 1fr; }
        #events { border: round #FFFFFF; height: 1fr; background: #000000; }
        #filter { dock: bottom; display: none; border: round #FF9E64; background: #000000; }
        #filter.visible { display: block; }
        ConfirmScreen { align: center middle; background: #000000 70%; }
        #dialog { width: 64; height: auto; border: thick #F7768E; background: #000000; padding: 1 2; }
        #question { width: 100%; text-align: center; margin-bottom: 1; }
        #buttons { height: auto; align: center middle; }
        #buttons Button { margin: 0 2; }
        #logview { border: round #FFFFFF; background: #000000; }
        #inspect { border: round #FFFFFF; }
        """
        BINDINGS = [
            Binding('q', 'quit', 'Quit'),
            Binding('slash', 'filter', 'Filter'),
            Binding('l', 'logs', 'Logs'),
            Binding('e', 'exec', 'Shell'),
            Binding('s', 'start_stop', 'Start/Stop'),
            Binding('r', 'restart', 'Restart'),
            Binding('ctrl+d', 'remove', 'Remove'),
            Binding('i', 'inspect', 'Inspect'),
            Binding('a', 'toggle_all', 'All/Running'),
            Binding('g', 'toggle_group', 'Group'),
            Binding('d', 'toggle_detail', 'Detail'),
            Binding('1', "show_tab('tab-containers')", 'Containers', show=False),
            Binding('2', "show_tab('tab-images')", 'Images', show=False),
            Binding('3', "show_tab('tab-volumes')", 'Volumes', show=False),
            Binding('4', "show_tab('tab-networks')", 'Networks', show=False),
            Binding('5', "show_tab('tab-events')", 'Events', show=False),
            Binding('escape', 'clear_filter', show=False),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.containers: dict[str, Container] = {}
            self.stats: dict[str, Stats] = {}
            self.stats_lock = threading.Lock()
            self.info: dict = {}
            self.show_all = True
            self.grouped = True
            self.filter_text = ''
            self.selected_key: str | None = None
            self.row_keys: list[str] = []
            self.readers: list[StreamReader] = []
            self.refresh_pending = False

        MAIN_ONLY = {
            'filter',
            'logs',
            'exec',
            'start_stop',
            'restart',
            'remove',
            'inspect',
            'toggle_all',
            'toggle_group',
            'toggle_detail',
            'show_tab',
        }

        def check_action(self, action: str, parameters: tuple) -> bool | None:
            # Hide and disable the dashboard's keys while a log/inspect/confirm screen is on top.
            if action in self.MAIN_ONLY and len(self.screen_stack) > 1:
                return False
            return True

        # ---------------------------------------------------------------- layout
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id='cards'):
                for card in ('running', 'stopped', 'health', 'cpu', 'mem', 'stacks'):
                    yield Static(id=f'card-{card}', classes='card')
            with TabbedContent(id='tabs'):
                with TabPane('📦 Containers', id='tab-containers'):
                    with Horizontal(id='main'):
                        yield DataTable(id='containers', cursor_type='row', zebra_stripes=False)
                        with VerticalScroll(id='side', can_focus=False):
                            yield Static(id='detail')
                            yield Label('CPU %  (last ~90s)', classes='spark-label', id='cpu-label')
                            yield Sparkline([0], id='cpu-spark', summary_function=max)
                            yield Label('Memory', classes='spark-label', id='mem-label')
                            yield Sparkline([0], id='mem-spark', summary_function=max)
                            yield Static(id='detail-extra')
                with TabPane('🖼  Images', id='tab-images'):
                    yield DataTable(id='images', cursor_type='row', classes='plain-table')
                with TabPane('💾 Volumes', id='tab-volumes'):
                    yield DataTable(id='volumes', cursor_type='row', classes='plain-table')
                with TabPane('🔗 Networks', id='tab-networks'):
                    yield DataTable(id='networks', cursor_type='row', classes='plain-table')
                with TabPane('⚡ Events', id='tab-events'):
                    yield RichLog(id='events', markup=False, highlight=False, max_lines=2000)
            yield Input(placeholder='filter by name, image, project, port… (enter to keep, esc to clear)', id='filter')
            yield Footer()

        def on_mount(self) -> None:
            self.register_theme(theme)
            self.theme = 'docker-night'
            table = self.query_one('#containers', DataTable)
            for key, label, width in (
                ('icon', '', 2),
                ('name', 'NAME', None),
                ('state', 'STATE', 12),
                ('cpu', 'CPU', 18),
                ('mem', 'MEMORY', 22),
                ('net', 'NET I/O', 20),
                ('ports', 'PORTS', None),
                ('up', 'UPTIME', None),
            ):
                table.add_column(label, key=key, width=width)
            table.border_title = '📦 containers'
            self.query_one('#side').border_title = '🔎 detail'
            self.query_one('#events', RichLog).border_title = '⚡ live docker events (healthcheck exec events hidden)'
            for table_id, columns in (
                ('#images', ('REPOSITORY', 'TAG', 'ID', 'SIZE', 'CREATED', 'USED BY')),
                ('#volumes', ('NAME', 'DRIVER', 'PROJECT', 'USED BY')),
                ('#networks', ('NAME', 'DRIVER', 'SCOPE', 'ID', 'PROJECT')),
            ):
                self.query_one(table_id, DataTable).add_columns(*columns)
            self.load_info()
            self.refresh_containers()
            self.readers.append(
                StreamReader(
                    ['stats', '--format', '{{json .}}'], stats_line_parser(self.stats, self.stats_lock)
                ).start()
            )
            self.readers.append(
                StreamReader(
                    ['events', '--format', '{{json .}}'],
                    lambda line: self.call_from_thread(self.handle_docker_event, line),
                ).start()
            )
            self.set_interval(REFRESH_SECONDS, self.refresh_containers)
            self.set_interval(1, self.repaint_stats)
            table.focus()

        def on_unmount(self) -> None:
            for reader in self.readers:
                reader.stop()

        # ---------------------------------------------------------------- data refresh
        @work(thread=True, exclusive=True, group='info')
        def load_info(self) -> None:
            self.info = engine_info()
            if self.info:
                self.call_from_thread(
                    setattr,
                    self,
                    'sub_title',
                    f'engine {self.info["version"]} · {self.info["os"]} · {self.info["cpus"]} CPUs · '
                    f'{human_bytes(self.info["mem_total"])}',
                )

        @work(thread=True, exclusive=True, group='ps')
        def refresh_containers(self) -> None:
            self.refresh_pending = False
            try:
                containers = list_containers()
            except DockerError as exc:
                self.call_from_thread(self.notify, str(exc), title='docker ps failed', severity='error')
                return
            self.call_from_thread(self.apply_containers, containers)

        def apply_containers(self, containers: list[Container]) -> None:
            self.containers = {c.id: c for c in containers}
            self.render_containers()
            self.render_cards()
            self.render_detail()

        @work(thread=True, exclusive=True, group='resources')
        def refresh_resources(self) -> None:
            try:
                images = json_lines(docker('images', '--format', '{{json .}}'))
                volumes = json_lines(docker('volume', 'ls', '--format', '{{json .}}'))
                networks = json_lines(docker('network', 'ls', '--format', '{{json .}}'))
            except DockerError as exc:
                self.call_from_thread(self.notify, str(exc), title='docker failed', severity='error')
                return
            self.call_from_thread(self.apply_resources, images, volumes, networks)

        def apply_resources(self, images: list[dict], volumes: list[dict], networks: list[dict]) -> None:
            users_of_image: dict[str, list[str]] = {}
            users_of_volume: dict[str, list[str]] = {}
            for c in self.containers.values():
                users_of_image.setdefault(c.image, []).append(c.name)
                for mount in filter(None, c.mounts.split(',')):
                    users_of_volume.setdefault(mount.strip(), []).append(c.name)

            table = self.query_one('#images', DataTable)
            table.clear()
            for image in sorted(images, key=lambda i: -parse_size(i.get('Size', '0'))):
                ref = f'{image.get("Repository")}:{image.get("Tag")}'
                users = users_of_image.get(ref, []) or users_of_image.get(image.get('Repository', ''), [])
                dangling = image.get('Repository') == '<none>'
                table.add_row(
                    Text(image.get('Repository', ''), style='dim' if dangling else 'bold'),
                    Text(image.get('Tag', ''), style='cyan'),
                    Text(image.get('ID', ''), style='dim'),
                    Text(image.get('Size', ''), style='#BB9AF7'),
                    image.get('CreatedSince', ''),
                    Text(', '.join(users) or 'unused', style='green' if users else 'bright_black'),
                )
            table.border_title = f'🖼  images ({len(images)})'

            table = self.query_one('#volumes', DataTable)
            table.clear()
            for volume in sorted(volumes, key=lambda v: v.get('Name', '')):
                name = volume.get('Name', '')
                labels = volume.get('Labels', '')
                project = re.search(r'com\.docker\.compose\.project=([^,]+)', labels)
                users = users_of_volume.get(name, [])
                table.add_row(
                    Text(name if len(name) < 50 else name[:12] + '…', style='bold'),
                    volume.get('Driver', ''),
                    Text(project.group(1) if project else '', style='magenta'),
                    Text(', '.join(users) or 'unused', style='green' if users else 'bright_black'),
                )
            table.border_title = f'💾 volumes ({len(volumes)})'

            table = self.query_one('#networks', DataTable)
            table.clear()
            for network in sorted(networks, key=lambda n: n.get('Name', '')):
                project = re.search(r'com\.docker\.compose\.project=([^,]+)', network.get('Labels', ''))
                builtin = network.get('Name') in ('bridge', 'host', 'none')
                table.add_row(
                    Text(network.get('Name', ''), style='dim' if builtin else 'bold'),
                    network.get('Driver', ''),
                    network.get('Scope', ''),
                    Text(network.get('ID', ''), style='dim'),
                    Text(project.group(1) if project else '', style='magenta'),
                )
            table.border_title = f'🔗 networks ({len(networks)})'

        def handle_docker_event(self, line: str) -> None:
            try:
                event = json.loads(ANSI.sub('', line))
            except json.JSONDecodeError:
                return
            kind = event.get('Type', '')
            action = event.get('Action', event.get('status', ''))
            attrs = (event.get('Actor') or {}).get('Attributes') or {}
            name = attrs.get('name') or (event.get('Actor') or {}).get('ID', '')[:12]
            when = time.strftime('%H:%M:%S', time.localtime(event.get('time', time.time())))
            # Healthchecks fire exec_create/start/die every few seconds per container: drop them.
            if action.startswith(('exec_', 'top')):
                return
            noisy = action.startswith('health_status: healthy')
            style = (
                'bright_black'
                if noisy
                else 'bold red'
                if action in ('die', 'kill', 'oom', 'destroy') or 'unhealthy' in action
                else 'bold green'
                if action in ('start', 'create', 'unpause')
                else 'yellow'
                if action in ('stop', 'restart', 'pause')
                else 'cyan'
            )
            icon = {'container': '📦', 'image': '🖼 ', 'volume': '💾', 'network': '🔗'}.get(kind, '• ')
            text = Text.assemble(
                (f'{when} ', '#565F89'), f'{icon} ', (f'{kind:<10}', 'dim'), (f'{action:<22} ', style), (name, 'bold')
            )
            if attrs.get('exitCode') not in (None, '0'):
                text.append(f'  exit {attrs["exitCode"]}', style='bold red')
            self.query_one('#events', RichLog).write(text)
            if kind == 'container' and not noisy:
                if action in ('die', 'oom') or 'unhealthy' in action:
                    self.notify(f'{name}: {action}', title='container event', severity='error')
                if not self.refresh_pending:
                    self.refresh_pending = True
                    self.set_timer(0.4, self.refresh_containers)

        # ---------------------------------------------------------------- rendering
        def visible_containers(self) -> list[Container]:
            rows = list(self.containers.values())
            if not self.show_all:
                rows = [c for c in rows if c.state in ('running', 'restarting', 'paused')]
            if self.filter_text:
                needle = self.filter_text.lower()
                rows = [
                    c
                    for c in rows
                    if needle in ' '.join((c.name, c.image, c.project, c.service, c.ports, c.state, c.status)).lower()
                ]
            return rows

        def stats_for(self, cid: str) -> Stats | None:
            with self.stats_lock:
                return self.stats.get(cid)

        def cpu_cell(self, cpu: float | None) -> Text:
            if cpu is None:
                return Text('  ─', style='bright_black')
            cpus = self.info.get('cpus', 1) if self.info else 1
            text = bar(cpu / cpus if cpu > 100 else cpu, 8)
            text.append(f' {cpu:6.1f}%', style=pct_style(cpu / cpus if cpu > 100 else cpu))
            return text

        def mem_cell(self, s: Stats | None) -> Text:
            if s is None:
                return Text('  ─', style='bright_black')
            text = bar(s.mem_pct, 8)
            text.append(f' {human_bytes(s.mem_used):>10}', style=pct_style(s.mem_pct))
            return text

        def container_cells(self, c: Container) -> dict[str, object]:
            icon, style = STATE_STYLE.get(c.state, ('?', 'white'))
            label, hstyle = HEALTH_STYLE.get(c.health, (c.state, style))
            s = self.stats_for(c.id) if c.state == 'running' else None
            indent = '  ' if self.grouped else ''
            label_text = (c.service or c.name) if self.grouped else c.name
            name = Text(indent + label_text, style='bold' if c.state == 'running' else 'bright_black')
            return {
                'icon': Text(icon, style=style),
                'name': name,
                'state': Text(label, style=hstyle),
                'cpu': self.cpu_cell(s.cpu if s else None),
                'mem': self.mem_cell(s),
                'net': Text(s.net_io if s else '', style='#7AA2F7'),
                'ports': Text(c.short_ports, style='cyan'),
                'up': Text(c.uptime, style='#9AA5CE'),
            }

        def project_cells(self, project: str, members: list[Container]) -> dict[str, object]:
            up = sum(c.state == 'running' for c in members)
            sick = sum(c.health == 'unhealthy' for c in members)
            stats = [s for s in (self.stats_for(c.id) for c in members if c.state == 'running') if s]
            cpu = sum(s.cpu for s in stats) if stats else None
            mem_used = sum(s.mem_used for s in stats)
            state_style = 'bold red' if sick else 'bold green' if up == len(members) else 'bold yellow'
            mem_text = Text(f'{"":8} {human_bytes(mem_used):>10}', style='bold #BB9AF7') if stats else Text('')
            return {
                'icon': Text('▾', style='bold #FF9E64'),
                'name': Text(f'{project}', style='bold #FF9E64'),
                'state': Text(f'{up}/{len(members)} up' + (f' ✚{sick}' if sick else ''), style=state_style),
                'cpu': Text(f'{"":8} {cpu:6.1f}%', style='bold #BB9AF7') if cpu is not None else Text(''),
                'mem': mem_text,
                'net': Text(''),
                'ports': Text(''),
                'up': Text(''),
            }

        def desired_rows(self) -> list[tuple[str, dict[str, object]]]:
            rows = self.visible_containers()
            if not self.grouped:
                return [(c.id, self.container_cells(c)) for c in sorted(rows, key=lambda c: c.name)]
            by_project: dict[str, list[Container]] = {}
            for c in rows:
                by_project.setdefault(c.project, []).append(c)
            out = []
            for project in sorted(by_project, key=lambda p: (p == NO_PROJECT, p)):
                members = sorted(by_project[project], key=lambda c: c.service or c.name)
                out.append((f'project:{project}', self.project_cells(project, members)))
                out.extend((c.id, self.container_cells(c)) for c in members)
            return out

        def render_containers(self) -> None:
            table = self.query_one('#containers', DataTable)
            rows = self.desired_rows()
            keys = [key for key, _ in rows]
            columns = ['icon', 'name', 'state', 'cpu', 'mem', 'net', 'ports', 'up']
            if keys == self.row_keys:
                # Same rows in the same order: update cells in place so the cursor never jumps.
                for key, cells in rows:
                    for column in columns:
                        table.update_cell(key, column, cells[column])
            else:
                selected = self.selected_key
                table.clear()
                for key, cells in rows:
                    table.add_row(*(cells[c] for c in columns), key=key)
                self.row_keys = keys
                if selected in keys:
                    table.move_cursor(row=keys.index(selected), animate=False)
            shown = sum(not k.startswith('project:') for k in keys)
            table.border_title = (
                f'📦 containers ({shown}/{len(self.containers)})'
                + ('' if self.show_all else ' · running only')
                + (f' · filter "{self.filter_text}"' if self.filter_text else '')
            )
            table.border_subtitle = 'enter/l logs · e shell · s start/stop · r restart · i inspect'

        def repaint_stats(self) -> None:
            if self.containers:
                self.render_containers()
                self.render_cards()
                self.render_sparklines()

        def render_cards(self) -> None:
            containers = list(self.containers.values())
            running = sum(c.state == 'running' for c in containers)
            stopped = len(containers) - running
            unhealthy = sum(c.health == 'unhealthy' for c in containers)
            healthy = sum(c.health == 'healthy' for c in containers)
            with self.stats_lock:
                live = [self.stats[c.id] for c in containers if c.state == 'running' and c.id in self.stats]
            cpus = self.info.get('cpus', 1) if self.info else 1
            cpu = sum(s.cpu for s in live) / cpus
            mem = sum(s.mem_used for s in live)
            mem_total = (self.info.get('mem_total') if self.info else 0) or (live[0].mem_limit if live else 0)
            mem_pct = mem / mem_total * 100 if mem_total else 0
            projects = {c.project for c in containers if c.project != NO_PROJECT}
            up_projects = {c.project for c in containers if c.project != NO_PROJECT and c.state == 'running'}

            def card(widget_id: str, title: str, value: Text, subtitle: Text | str = '') -> None:
                widget = self.query_one(widget_id, Static)
                widget.border_title = title
                body = Text.assemble(
                    value, '\n', subtitle if isinstance(subtitle, Text) else Text(subtitle, style='dim')
                )
                widget.update(body)

            card(
                '#card-running', 'running', Text(f'● {running}', style='bold green'), f'of {len(containers)} containers'
            )
            card(
                '#card-stopped',
                'stopped',
                Text(f'○ {stopped}', style='bold red' if stopped else 'bold bright_black'),
                'exited / created',
            )
            card(
                '#card-health',
                'health',
                Text(f'✚ {unhealthy} unhealthy', style='bold red')
                if unhealthy
                else Text(f'♥ {healthy} healthy', style='bold green'),
                f'{healthy} healthy · {unhealthy} unhealthy',
            )
            card('#card-cpu', 'cpu (host)', Text(f'{cpu:5.1f}%', style='bold ' + pct_style(cpu)), bar(cpu, 18))
            card('#card-mem', 'memory', Text(human_bytes(mem), style='bold ' + pct_style(mem_pct)), bar(mem_pct, 18))
            card(
                '#card-stacks',
                'compose stacks',
                Text(f'▾ {len(up_projects)}/{len(projects)}', style='bold #FF9E64'),
                'stacks up',
            )

        def current_target(self) -> tuple[str, list[Container]] | None:
            """The highlighted row: ('container', [c]) or ('project', [members])."""
            key = self.selected_key
            if not key:
                return None
            if key.startswith('project:'):
                project = key.split(':', 1)[1]
                members = [c for c in self.visible_containers() if c.project == project]
                return ('project', members) if members else None
            c = self.containers.get(key)
            return ('container', [c]) if c else None

        def render_detail(self) -> None:
            target = self.current_target()
            detail = self.query_one('#detail', Static)
            extra = self.query_one('#detail-extra', Static)
            if not target:
                detail.update(Text('select a container', style='dim'))
                extra.update('')
                return
            kind, members = target
            if kind == 'project':
                c0 = members[0]
                text = Text.assemble(('▾ ', '#FF9E64'), (c0.project, 'bold #FF9E64'), '\n')
                text.append(f'{len(members)} containers\n\n', style='dim')
                for c in members:
                    icon, style = STATE_STYLE.get(c.state, ('?', 'white'))
                    label, hstyle = HEALTH_STYLE.get(c.health, (c.state, style))
                    text.append(f'{icon} ', style=style)
                    text.append(f'{c.service or c.name:<18}', style='bold')
                    text.append(f' {label}\n', style=hstyle)
                text.append('\nactions apply to the whole stack', style='italic #565F89')
                detail.update(text)
                self.query_one('#side').border_title = f'🔎 stack · {c0.project}'
                extra.update('')
            else:
                c = members[0]
                icon, style = STATE_STYLE.get(c.state, ('?', 'white'))
                label, hstyle = HEALTH_STYLE.get(c.health, (c.state, style))
                s = self.stats_for(c.id)
                text = Text.assemble((f'{icon} ', style), (c.name, 'bold'), '\n')
                rows = [
                    ('id', Text(c.id, style='dim')),
                    ('image', Text(c.image, style='#7AA2F7')),
                    ('state', Text(f'{c.state} · {label}' if c.health else c.state, style=hstyle)),
                    ('status', Text(c.status, style='#9AA5CE')),
                    ('stack', Text(f'{c.project} / {c.service}' if c.service else c.project, style='#FF9E64')),
                    ('ports', Text(c.short_ports or '—', style='cyan')),
                    ('network', Text(c.networks or '—')),
                ]
                if s and c.state == 'running':
                    rows += [('pids', Text(s.pids)), ('net i/o', Text(s.net_io)), ('block', Text(s.block_io))]
                for key, value in rows:
                    text.append(f'{key:>8} ', style='#565F89')
                    text.append_text(value)
                    text.append('\n')
                detail.update(text)
                self.query_one('#side').border_title = f'🔎 {c.service or c.name}'
                mounts = [m for m in c.mounts.split(',') if m.strip()]
                body = Text('\n  mounts\n', style='#565F89')
                body.append('\n'.join(f'  • {m[:42]}' for m in mounts) or '  —', style='#BB9AF7')
                extra.update(body)
            self.render_sparklines()

        def render_sparklines(self) -> None:
            target = self.current_target()
            cpu_spark = self.query_one('#cpu-spark', Sparkline)
            mem_spark = self.query_one('#mem-spark', Sparkline)
            if not target:
                return
            kind, members = target
            histories = [self.stats_for(c.id) for c in members]
            histories = [h for h in histories if h and h.cpu_history]
            if not histories:
                cpu_spark.data, mem_spark.data = [0], [0]
                self.query_one('#cpu-label', Label).update('CPU %  (not running)')
                self.query_one('#mem-label', Label).update('Memory')
                return
            length = min(len(h.cpu_history) for h in histories)
            cpu = [sum(list(h.cpu_history)[-length:][i] for h in histories) for i in range(length)]
            mem = [sum(list(h.mem_history)[-length:][i] for h in histories) for i in range(length)]
            cpu_spark.data, mem_spark.data = cpu or [0], mem or [0]
            self.query_one('#cpu-label', Label).update(
                Text.assemble('CPU %  ', (f'now {cpu[-1]:.1f}%', 'bold'), f'  peak {max(cpu):.1f}%')
            )
            self.query_one('#mem-label', Label).update(
                Text.assemble('Memory  ', (f'now {mem[-1]:.0f}MiB', 'bold'), f'  peak {max(mem):.0f}MiB')
            )

        # ---------------------------------------------------------------- events
        @on(DataTable.RowHighlighted, '#containers')
        def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            self.selected_key = event.row_key.value if event.row_key else None
            self.render_detail()

        @on(DataTable.RowSelected, '#containers')
        def row_selected(self, event: DataTable.RowSelected) -> None:
            self.action_logs()

        @on(TabbedContent.TabActivated)
        def tab_activated(self, event: TabbedContent.TabActivated) -> None:
            if event.pane.id in ('tab-images', 'tab-volumes', 'tab-networks'):
                self.refresh_resources()

        @on(Input.Changed, '#filter')
        def filter_changed(self, event: Input.Changed) -> None:
            self.filter_text = event.value.strip()
            self.render_containers()

        @on(Input.Submitted, '#filter')
        def filter_submitted(self) -> None:
            self.query_one('#containers').focus()
            if not self.filter_text:
                self.query_one('#filter').remove_class('visible')

        # ---------------------------------------------------------------- actions
        def action_show_tab(self, tab: str) -> None:
            self.query_one(TabbedContent).active = tab
            # TabbedContent follows focus, so move focus into the new pane too.
            target = {
                'tab-containers': '#containers',
                'tab-images': '#images',
                'tab-volumes': '#volumes',
                'tab-networks': '#networks',
                'tab-events': '#events',
            }[tab]
            self.query_one(target).focus()

        def action_filter(self) -> None:
            self.query_one(TabbedContent).active = 'tab-containers'
            box = self.query_one('#filter', Input)
            box.add_class('visible')
            box.focus()

        def action_clear_filter(self) -> None:
            box = self.query_one('#filter', Input)
            box.value = ''
            box.remove_class('visible')
            self.query_one('#containers').focus()

        def action_toggle_all(self) -> None:
            self.show_all = not self.show_all
            self.render_containers()
            self.notify('showing all containers' if self.show_all else 'showing running containers only')

        def action_toggle_group(self) -> None:
            self.grouped = not self.grouped
            self.row_keys = []
            if not self.grouped and self.selected_key and self.selected_key.startswith('project:'):
                self.selected_key = None
            self.render_containers()

        def action_toggle_detail(self) -> None:
            self.query_one('#side').toggle_class('hidden')

        def action_logs(self) -> None:
            target = self.current_target()
            if not target:
                return
            kind, members = target
            title = members[0].project if kind == 'project' else members[0].name
            self.push_screen(LogScreen([(c.id, c.service or c.name) for c in members], title))

        def action_inspect(self) -> None:
            target = self.current_target()
            if target and target[0] == 'container':
                self.push_screen(InspectScreen(target[1][0].id, target[1][0].name))

        def action_exec(self) -> None:
            target = self.current_target()
            if not target or target[0] != 'container':
                self.notify('select a single container to open a shell', severity='warning')
                return
            c = target[1][0]
            if c.state != 'running':
                self.notify(f'{c.name} is not running', severity='warning')
                return
            shell = 'if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi'
            try:
                with self.suspend():
                    print(f'\n\x1b[1;34m── shell in {c.name} (exit to return) ──\x1b[0m\n')
                    subprocess.run(['docker', 'exec', '-it', c.id, 'sh', '-c', shell])
            except Exception as exc:  # noqa: BLE001 - e.g. suspend unsupported in this terminal
                self.notify(f'could not open a shell: {exc}', severity='error')
            self.refresh_containers()

        def run_on(self, verb: str, members: list[Container], past: str) -> None:
            names = ', '.join(c.service or c.name for c in members)
            self.notify(f'{verb} {names}…', title=verb)
            self.docker_action(verb, [c.id for c in members], names, past)

        @work(thread=True, group='actions')
        def docker_action(self, verb: str, ids: list[str], names: str, past: str) -> None:
            try:
                docker(*verb.split(), *ids, timeout=120)
                self.call_from_thread(self.notify, f'{names} {past}', title='done', severity='information')
            except DockerError as exc:
                self.call_from_thread(self.notify, str(exc), title=f'{verb} failed', severity='error')
            self.call_from_thread(self.refresh_containers)

        def action_start_stop(self) -> None:
            target = self.current_target()
            if not target:
                return
            kind, members = target
            if any(c.state == 'running' for c in members):
                running = [c for c in members if c.state == 'running']
                label = f'stack {members[0].project}' if kind == 'project' else members[0].name
                self.push_screen(
                    ConfirmScreen(f'Stop {label}?  ({len(running)} running)'),
                    lambda ok: ok and self.run_on('stop', running, 'stopped'),
                )
            else:
                self.run_on('start', members, 'started')

        def action_restart(self) -> None:
            target = self.current_target()
            if target:
                self.run_on('restart', target[1], 'restarted')

        def action_remove(self) -> None:
            target = self.current_target()
            if not target:
                return
            kind, members = target
            label = (
                f'all {len(members)} containers of stack {members[0].project}' if kind == 'project' else members[0].name
            )
            running = any(c.state == 'running' for c in members)
            question = (
                f'Remove {label}?'
                + ('\nRunning containers are force-stopped first.' if running else '')
                + '\nVolumes are kept.'
            )
            self.push_screen(
                ConfirmScreen(question),
                lambda ok: ok and self.run_on('rm --force' if running else 'rm', members, 'removed'),
            )

    return DockerMonitor()


def run_app() -> int:
    build_app().run()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog='docker-monitor', description=__doc__.split('\n\n')[0])
    parser.add_argument('--snapshot', action='store_true', help='print a coloured one-shot summary and exit')
    parser.add_argument('--json', action='store_true', help='print a JSON summary and exit')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args()
    if not shutil.which('docker'):
        print('docker-monitor: docker CLI not found on PATH', file=sys.stderr)
        return 1
    if args.snapshot or args.json:
        return snapshot(args.json)
    try:
        docker('version', '--format', '{{.Server.Version}}', timeout=10)
    except DockerError as exc:
        print(
            f'docker-monitor: the Docker engine is not reachable ({exc}). Is Docker Desktop running?', file=sys.stderr
        )
        return 1
    return run_app()


if __name__ == '__main__':
    sys.exit(main())
