"""End-to-end tests against a real Docker engine; skipped when none is reachable."""

import json
import subprocess
import sys
import uuid

import pytest

import docker_monitor as dm


def linux_engine_available() -> bool:
    # The tests run a Linux image, so a Windows-containers engine (e.g. GitHub's Windows runners) can't host them.
    try:
        return dm.docker('version', '--format', '{{.Server.Os}}', timeout=10).strip() == 'linux'
    except dm.DockerError:
        return False


pytestmark = pytest.mark.skipif(not linux_engine_available(), reason='no Linux Docker engine reachable')


@pytest.fixture(scope='module')
def container():
    """A throwaway container in its own fake compose project, removed afterwards."""
    name = f'dm-test-{uuid.uuid4().hex[:8]}'
    subprocess.run(
        [
            'docker',
            'run',
            '-d',
            '--name',
            name,
            '--label',
            'com.docker.compose.project=dm-test',
            '--label',
            'com.docker.compose.service=sleeper',
            'alpine:3',
            'sleep',
            '300',
        ],
        check=True,
        capture_output=True,
    )
    yield name
    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)


def test_json_snapshot_lists_container(container):
    out = subprocess.run([sys.executable, dm.__file__, '--json'], check=True, capture_output=True, text=True)
    data = json.loads(out.stdout)
    match = [c for c in data['containers'] if c['name'] == container]
    assert match and match[0]['project'] == 'dm-test' and match[0]['service'] == 'sleeper'
    assert match[0]['state'] == 'running'


def test_coloured_snapshot_runs(container):
    out = subprocess.run(
        [sys.executable, dm.__file__, '--snapshot'], check=True, capture_output=True, text=True, encoding='utf-8'
    )
    assert 'dm-test' in out.stdout


async def test_dashboard_groups_filters_and_restarts(container):
    app = dm.build_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(3)
        assert any(c.name == container for c in app.containers.values())
        assert 'project:dm-test' in app.row_keys

        # Filter down to our container, select it and restart it.
        await pilot.press('slash', *container, 'enter')
        await pilot.pause(0.5)
        await pilot.press('down')
        assert app.containers[app.selected_key].name == container
        await pilot.press('r')
        await pilot.pause(6)
        state = dm.docker('inspect', '--format', '{{.State.Running}}', container).strip()
        assert state == 'true'

        # Tabs switch with the number keys.
        await pilot.press('2')
        await pilot.pause(1)
        from textual.widgets import TabbedContent

        assert app.query_one(TabbedContent).active == 'tab-images'
