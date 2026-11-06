import io
import json
from unittest.mock import MagicMock

import pytest

from staffdeck import APIResponse, console
from staffdeck_harness.cli_install import install


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('STAFFDECK_API_KEY', 'secret-do-not-print')
    monkeypatch.setenv('STAFFDECK_BASE_URL', 'http://127.0.0.1:5186')
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.agents.list.return_value = APIResponse({'data': [
        {'id': 'overall', 'name': '整体智能体', 'is_overall': True, 'status': 'active'},
        {'id': 'employee', 'name': '财务助手', 'status': 'active'},
    ]}, 200)
    monkeypatch.setattr(console, 'StaffDeck', lambda **kwargs: mock)
    return mock


def test_empty_command_and_auth_help_need_no_connection(capsys):
    assert console.main([]) == 0
    assert 'staffdeck chat' in capsys.readouterr().out
    assert console.main(['--auth']) == 0
    assert 'staffdeck login' in capsys.readouterr().out


def test_singular_agent_alias_is_readable(client, capsys):
    assert console.main(['agent', 'list']) == 0
    out = capsys.readouterr().out
    assert '财务助手' in out and 'employee' in out and '整体智能体' not in out
    assert 'secret-do-not-print' not in out


def test_interactive_chat_keeps_session_until_new_command(client, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', TerminalInput())
    messages = iter(['80元', '100元', '/new', '120元', '/exit'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(messages))
    client.runs.create.side_effect = [APIResponse({'id': f'run{i}'}, 202) for i in range(3)]
    client.runs.wait.side_effect = [APIResponse({'reply': f'回复{i}', 'session_id': f's{i}'}, 200)
                                    for i in range(3)]
    assert console.main(['--chat']) == 0
    bodies = [call.args[1] for call in client.runs.create.call_args_list]
    assert bodies[0] == {'input': '80元', 'session_mode': 'stateful'}
    assert bodies[1]['session_id'] == 's0'
    assert 'session_id' not in bodies[2]
    out = capsys.readouterr().out
    assert '财务助手' in out and '回复2' in out and '已退出对话' in out
    assert '"status_code"' not in out


def test_multiple_employees_require_a_real_selection(client, monkeypatch):
    client.agents.list.return_value = APIResponse({'data': [
        {'id': 'finance', 'name': '财务助手', 'status': 'active'},
        {'id': 'legal', 'name': '法务助手', 'status': 'active'},
    ]}, 200)
    monkeypatch.setattr('sys.stdin', TerminalInput())
    answers = iter(['2', '/exit'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    assert console.main(['chat']) == 0
    client.runs.create.assert_not_called()


def test_named_one_shot_and_pipe(client, monkeypatch):
    client.runs.create.return_value = APIResponse({'id': 'run'}, 202)
    client.runs.wait.return_value = APIResponse({'reply': '你好', 'session_id': 'session'}, 200)
    assert console.main(['chat', '--agent', '财务助手', '你好']) == 0
    client.runs.create.assert_called_with('employee', {'input': '你好', 'session_mode': 'stateful'})
    monkeypatch.setattr('sys.stdin', io.StringIO('管道输入'))
    assert console.main(['chat']) == 0
    assert client.runs.create.call_args.args[1]['input'] == '管道输入'


def test_install_registers_an_executable_without_replacing_another_command(tmp_path):
    root = tmp_path / 'app with spaces'
    (root / 'distribution').mkdir(parents=True)
    (root / 'distribution/manifest.json').write_text(json.dumps({'installation': {'tenant_id': 'local'}}))
    directory = tmp_path / 'bin'
    target = install(root, bin_dir=directory, executable='/usr/bin/python3',
                     base_url='http://127.0.0.1:5186', config_dir=tmp_path / 'profiles')
    assert target.name == 'staffdeck' and target.stat().st_mode & 0o111
    text = target.read_text()
    assert 'STAFFDECK_TENANT_ID=local' in text and 'staffdeck_harness.cli "$@"' in text
    assert 'STAFFDECK_INSTALL_ROOT=' in text and 'app with spaces' in text
    install(root, bin_dir=directory, executable='/usr/bin/python3')
    target.write_text('#!/bin/sh\necho another application\n')
    with pytest.raises(ValueError):
        install(root, bin_dir=directory)


def test_hyphenated_sdk_groups_still_use_the_sdk(monkeypatch):
    from staffdeck import console
    seen = []
    monkeypatch.setattr(console.api_cli, 'main', lambda args: seen.append(args) or 0)
    for group in ('knowledge-bases', 'mcp-servers', 'scheduled-tasks'):
        assert console.main([group, '--help']) == 0
    assert seen == [[group, '--help'] for group in
                    ('knowledge-bases', 'mcp-servers', 'scheduled-tasks')]


def test_native_console_launcher_uses_binary_instead_of_python_module(tmp_path):
    root = tmp_path / 'bundle'
    (root / 'distribution').mkdir(parents=True)
    (root / 'distribution/manifest.json').write_text(json.dumps({'installation': {'tenant_id': 'tenant_demo'}}))
    target = install(root, bin_dir=tmp_path/'bin', executable=root/'staffdeck-cli', native=True)
    assert ' -m staffdeck_harness.cli' not in target.read_text()
    assert str(root/'staffdeck-cli') in target.read_text()


def test_login_discovers_shipped_tenant_and_saved_local_port(tmp_path, monkeypatch, capsys):
    root = tmp_path/'bundle'; (root/'distribution').mkdir(parents=True)
    (root/'distribution/manifest.json').write_text(json.dumps({'installation':{'tenant_id':'old-custom-tenant'}}))
    data=tmp_path/'data'; data.mkdir(); (data/'network.json').write_text('{"port":5188}')
    monkeypatch.setenv('STAFFDECK_INSTALL_ROOT',str(root))
    monkeypatch.setenv('STAFFDECK_CONFIG_DIR',str(tmp_path/'profiles'))
    monkeypatch.delenv('STAFFDECK_BASE_URL',raising=False)
    monkeypatch.delenv('STAFFDECK_TENANT_ID',raising=False)
    monkeypatch.setenv('ULTRARAG_DATA_DIR',str(data))
    def auth(args):
        assert args.tenant_id=='old-custom-tenant'
        assert args.base_url=='http://127.0.0.1:5188'
        print(json.dumps({'user':{'username':'admin'}}));return 0
    monkeypatch.setattr(console.api_cli,'_auth',auth)
    assert console.main(['login','--password-stdin'])==0
    assert '登录成功' in capsys.readouterr().out


def test_login_follows_the_actual_port_after_a_server_collision(tmp_path, monkeypatch):
    data=tmp_path/'data';data.mkdir()
    (data/'network.json').write_text('{"port":5173}')
    (data/'service.json').write_text('{"port":5186}')
    monkeypatch.setenv('ULTRARAG_DATA_DIR',str(data))
    monkeypatch.setenv('STAFFDECK_CONFIG_DIR',str(tmp_path/'profiles'))
    monkeypatch.delenv('STAFFDECK_BASE_URL',raising=False)
    def auth(args):
        assert args.base_url=='http://127.0.0.1:5186'
        print(json.dumps({'user':{'username':'admin'}}));return 0
    monkeypatch.setattr(console.api_cli,'_auth',auth)
    assert console.main(['login','--password-stdin'])==0
