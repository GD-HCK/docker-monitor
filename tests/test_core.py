"""Unit tests for the parsing helpers: no Docker needed."""

import pytest

import docker_monitor as dm


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('0B', 0),
        ('512B', 512),
        ('1.5kB', 1500),
        ('2MiB', 2 * 1024**2),
        ('15.35GiB', 15.35 * 1024**3),
        ('', 0),
        ('garbage', 0),
    ],
)
def test_parse_size(text, expected):
    assert dm.parse_size(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(0, '0B'), (1023, '1023B'), (1024, '1.0KiB'), (5 * 1024**2, '5.0MiB'), (3 * 1024**3, '3.0GiB')],
)
def test_human_bytes(value, expected):
    assert dm.human_bytes(value) == expected


@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        ('Up 2 minutes (healthy)', 'healthy'),
        ('Up 5 seconds (unhealthy)', 'unhealthy'),
        ('Up 1 second (health: starting)', 'starting'),
        ('Up 3 hours', ''),
        ('Exited (1) 2 minutes ago', ''),
    ],
)
def test_health_of(status, expected):
    assert dm.health_of(status) == expected


def make_container(**overrides) -> dm.Container:
    fields = dict.fromkeys(dm.Container.__dataclass_fields__, '')
    fields.update(overrides)
    return dm.Container(**fields)


def test_short_ports_dedupes_ipv6_and_drops_bind_address():
    c = make_container(ports='0.0.0.0:8080->80/tcp, [::]:8080->80/tcp, 127.0.0.1:5432->5432/tcp, 6379/tcp')
    assert c.short_ports == '8080→80 5432→5432'


def test_uptime_strips_up_and_health():
    assert make_container(status='Up 12 minutes (healthy)').uptime == '12 minutes'
    assert make_container(status='Exited (0) 3 hours ago').uptime == 'Exited (0) 3 hours ago'


def test_redact_env_hides_values_but_keeps_names():
    data = {'Config': {'Env': ['POSTGRES_PASSWORD=hunter2', 'PATH=/usr/bin', 'EMPTY=']}}
    redacted = dm.redact_env(data)['Config']['Env']
    assert redacted == ['POSTGRES_PASSWORD=•••', 'PATH=•••', 'EMPTY=•••']
    assert 'hunter2' not in str(redacted)


def test_stats_update_parses_docker_stats_row():
    stats = dm.Stats()
    stats.update(
        {
            'CPUPerc': '12.50%',
            'MemUsage': '70.86MiB / 15.35GiB',
            'MemPerc': '0.45%',
            'NetIO': '1kB / 2kB',
            'BlockIO': '0B / 0B',
            'PIDs': '42',
        }
    )
    assert stats.cpu == 12.5
    assert stats.mem_used == pytest.approx(70.86 * 1024**2)
    assert stats.pids == '42'
    assert list(stats.cpu_history) == [12.5]


def test_stats_line_parser_strips_terminal_escapes():
    import threading

    store: dict[str, dm.Stats] = {}
    parse = dm.stats_line_parser(store, threading.Lock())
    parse('\x1b[H{"ID":"69649e9764bc3e","CPUPerc":"1.00%","MemUsage":"1MiB / 2GiB","MemPerc":"0.05%"}\x1b[K')
    parse('not json')
    assert store['69649e9764bc'].cpu == 1.0
