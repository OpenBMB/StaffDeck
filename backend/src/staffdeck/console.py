"""Human terminal commands over the same SDK used by the JSON API CLI."""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import closing, redirect_stdout
from pathlib import Path

from . import cli as api_cli
from .client import StaffDeck
from .errors import APIError, RunFailedError, StaffDeckError, WaitTimeout
from .profiles import ProfileStore, connection_url

ALIASES = {
    'agent': 'agents', 'sop': 'sops', 'skill': 'skills', 'tool': 'tools',
    'session': 'sessions', 'kb': 'knowledge-bases', '--chat': 'chat', '--auth': 'auth',
}
_GLOBAL_VALUES = {'--base-url', '--profile', '--http-timeout', '--max-retries', '--output'}


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(f'参数不完整或命令不正确。请运行：{self.prog} --help')


def _clean(value):
    return ''.join(c for c in str(value) if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127))


def _normalise(argv):
    args = list(argv)
    index = 0
    while index < len(args) and args[index] in _GLOBAL_VALUES:
        index += 2
    if index < len(args):
        args[index] = ALIASES.get(args[index], args[index])
        if args[index] in {'login', 'logout', 'status'}:
            args.insert(index, 'auth')
    return args, index


def _options(parser, root=False):
    default = None if root else argparse.SUPPRESS
    parser.add_argument('--base-url', default=os.environ.get('STAFFDECK_BASE_URL') if root else default)
    parser.add_argument('--profile', default=os.environ.get('STAFFDECK_PROFILE', 'local') if root else default)
    parser.add_argument('--http-timeout', type=float, default=30.0 if root else default)
    parser.add_argument('--max-retries', type=int, default=2 if root else default)


def _parser():
    parser = Parser(prog='staffdeck', description='StaffDeck 开源版命令行',
                    formatter_class=argparse.RawDescriptionHelpFormatter,
                    epilog='快速开始：\n  staffdeck login                 登录（默认 admin / admin）\n'
                           '  staffdeck agent list            查看员工\n'
                           '  staffdeck chat                  选择员工并持续对话\n'
                           '  staffdeck chat --agent 财务      直接与财务助手对话\n'
                           '  staffdeck chat --agent IT "你好" 只问一句\n'
                           '  staffdeck --output json ...      使用原有 JSON 接口命令')
    _options(parser, root=True)
    root = Path(os.environ.get('STAFFDECK_INSTALL_ROOT') or
                (sys._MEIPASS if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[3]))
    manifest = root / 'distribution/manifest.json'
    version = json.loads(manifest.read_text(encoding='utf-8')).get('version', '0.6.0') if manifest.is_file() else '0.6.0'
    parser.add_argument('--version', action='version', version='StaffDeck CLI ' + version)
    groups = parser.add_subparsers(dest='command')
    chat = groups.add_parser('chat', help='直接开始交互对话，或发送一句话')
    _options(chat)
    chat.add_argument('--agent-id', '--agent', dest='agent_id', help='员工名称或 ID；只有一个员工时自动选择')
    chat.add_argument('prompt', nargs='*', help='可直接写一句话；省略则进入交互聊天')
    chat.add_argument('--message', help='发送一句话；- 表示读取标准输入')
    chat.add_argument('--session-id', help='继续已有会话')
    chat.add_argument('--stream', action='store_true', help='逐步显示回复')
    chat.add_argument('--timeout', type=float, default=420)
    agents = groups.add_parser('agents', aliases=['agent'], help='查看可用员工')
    _options(agents)
    actions = agents.add_subparsers(dest='action')
    listing = actions.add_parser('list', help='列出员工名称、ID 和状态')
    _options(listing)
    listing.add_argument('--limit', type=int, default=100)
    auth = groups.add_parser('auth', help='登录、查看连接或退出')
    _options(auth)
    actions = auth.add_subparsers(dest='action')
    login = actions.add_parser('login', help='登录；开源版默认账号密码均为 admin')
    _options(login)
    login.add_argument('--username', default='admin')
    login.add_argument('--tenant-id', default=os.environ.get('STAFFDECK_TENANT_ID'))
    login.add_argument('--password-stdin', action='store_true')
    for name in ('status', 'logout'):
        _options(actions.add_parser(name))
    return parser


def _connection(args):
    key = os.environ.get('STAFFDECK_API_KEY', '').strip()
    base = args.base_url
    if not key:
        profile = ProfileStore(args.profile).load()
        if profile:
            if base and connection_url(base) != profile['base_url']:
                raise UsageError('此连接属于另一个服务，请选择对应的 --profile 和 --base-url。')
            base, key = profile['base_url'], profile['api_key']
    if not key or not base:
        raise UsageError('尚未登录。先运行 staffdeck login；默认账号密码均为 admin。')
    return StaffDeck(base_url=base, api_key=key, timeout=args.http_timeout,
                     max_retries=args.max_retries)


def _rows(client, limit=100):
    return [row for row in client.agents.list(limit=limit).data['data']
            if not row.get('is_overall') and row.get('status') not in {'archived', 'deleted'}]


def _choose_agent(client, reference):
    if reference and reference.startswith('agent_'):
        return client.agents.get(reference).data
    rows = _rows(client)
    if reference:
        matches = [row for row in rows if reference in (row['id'], row['name'])]
        if not matches:
            matches = [row for row in rows if reference.casefold()
                       == row['name'].removesuffix('助手').casefold()]
        if len(matches) == 1:
            return matches[0]
        raise UsageError('找不到唯一匹配的员工。运行 staffdeck agent list 查看名称和 ID。')
    if not rows:
        raise UsageError('暂无可用员工，请先在网页的“我的数字员工”中创建。')
    if len(rows) == 1:
        return rows[0]
    if not sys.stdin.isatty():
        raise UsageError('存在多个员工，请指定名称，例如 staffdeck chat --agent 财务助手。')
    print('选择数字员工：')
    for index, row in enumerate(rows, 1):
        print(f'  {index}. {_clean(row["name"])}')
    while True:
        answer = input('输入序号或员工名称（/exit 退出）：').strip()
        if answer in {'/exit', '/quit', '退出'}:
            return None
        if answer.isdecimal() and 1 <= int(answer) <= len(rows):
            return rows[int(answer) - 1]
        matches = [row for row in rows if answer in (row['name'], row['id'])]
        if len(matches) == 1:
            return matches[0]
        print('没有找到这个员工，请重新输入。')


def _turn(client, agent, message, session, args):
    body = {'input': message, 'session_mode': 'stateful'}
    if session:
        body['session_id'] = session
    receipt = client.runs.create(agent['id'], body)
    run_id = receipt.data.get('run_id') or receipt.data.get('id')
    if not run_id:
        raise UsageError('服务没有返回运行编号，请先在网页检查执行记录，避免重复提交。')
    args.last_run_id = run_id
    print('正在执行…', file=sys.stderr, flush=True)
    streamed = False
    if args.stream:
        with closing(client.runs.events(run_id)) as events:
            for event in events:
                if event.event == 'run.output.delta' and isinstance(event.data, dict):
                    text = event.data.get('content', '')
                    if text:
                        print(_clean(text), end='', flush=True)
                        streamed = True
        state = client.runs.get(run_id).data
        if state.get('status') in {'failed', 'cancelled'}:
            raise RunFailedError(run_id, state)
        result = client.runs.result(run_id).data
    else:
        result = client.runs.wait(run_id, timeout=args.timeout).data
    if not streamed:
        print(_clean(result.get('reply') or '本次执行完成，暂无文字回复。'), flush=True)
    else:
        print()
    return result.get('session_id', session)


def _chat(client, args):
    agent = _choose_agent(client, args.agent_id)
    if agent is None:
        return 0
    if args.message and args.prompt:
        raise UsageError('一句话可以直接写在 chat 后，或使用 --message，请任选一种。')
    message = args.message if args.message is not None else (' '.join(args.prompt) or None)
    interactive = message is None and sys.stdin.isatty()
    if message == '-' or (message is None and not interactive):
        message = sys.stdin.read()
    session = args.session_id
    if not interactive:
        if not message or not message.strip():
            raise UsageError('请输入内容，例如 staffdeck chat --agent 财务助手 "你好"。')
        _turn(client, agent, message, session, args)
        return 0
    print(f'正在与 {_clean(agent["name"])} 对话。/new 新会话，/exit 退出。')
    while True:
        try:
            message = input('\n你：').strip()
        except EOFError:
            print('\n已退出对话。')
            return 0
        if message in {'/exit', '/quit', 'exit', 'quit', '退出'}:
            print('已退出对话。')
            return 0
        if message == '/new':
            session = None
            print('已开始新会话。')
        elif message:
            print(f'\n{_clean(agent["name"])}：', flush=True)
            session = _turn(client, agent, message, session, args)


def _auth(args):
    profile = ProfileStore(args.profile).load()
    if args.action == 'login' and profile:
        if profile.get('user', {}).get('username') != args.username:
            raise UsageError('已有另一个账号连接，请先 staffdeck logout，或使用另一个 --profile。')
        with _connection(args) as client:
            identity = client.system.whoami().data
        print(f'已登录：{_clean(identity["username"])}（{_clean(profile["base_url"])}）')
        return 0
    if args.action == 'login' and not args.tenant_id:
        root = Path(os.environ.get('STAFFDECK_INSTALL_ROOT') or
                    (sys._MEIPASS if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[3]))
        manifest = root / 'distribution/manifest.json'
        args.tenant_id = (json.loads(manifest.read_text(encoding='utf-8'))['installation']['tenant_id']
                          if manifest.is_file() else 'tenant_demo')
    if args.action == 'login' and not args.base_url:
        # Read only: honor the local application's saved port without changing
        # remote profiles or running the server/bootstrap during client commands.
        data = os.environ.get('ULTRARAG_DATA_DIR')
        if not data:
            data = (Path.home() / 'Library/Application Support/StaffDeck' if sys.platform == 'darwin'
                    else Path(os.environ.get('APPDATA', Path.home())) / 'StaffDeck' if os.name == 'nt'
                    else Path.home() / '.local/share/StaffDeck')
        current = Path(data) / 'service.json'
        network = Path(data) / 'network.json'
        state = current if current.is_file() else network
        config = json.loads(state.read_text(encoding='utf-8')) if state.is_file() else {}
        args.base_url = f"http://127.0.0.1:{int(config.get('port', 5173))}"
    output = io.StringIO()
    with redirect_stdout(output):
        result = api_cli._auth(args)
    data = json.loads(output.getvalue())
    if args.action == 'logout':
        print('已退出本机 CLI 连接。')
    elif args.action == 'login':
        print(f'登录成功：{_clean(data["user"]["username"])}')
    else:
        print(f'已登录：{_clean(data["data"]["username"])}（租户 {_clean(data["data"]["tenant_id"])}）')
    return result


def main(argv=None):
    if os.name == 'nt':
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')
    raw, index = _normalise(sys.argv[1:] if argv is None else argv)
    # Machine output is an explicit projection of the existing SDK CLI.
    if '--output' in raw:
        place = raw.index('--output')
        if place + 1 < len(raw) and raw[place + 1] == 'json':
            return api_cli.main(raw[:place] + raw[place + 2:])
    resource = raw[index] if index < len(raw) else None
    if (resource or '').replace('-', '_') in api_cli._COMMANDS and resource != 'agents':
        return api_cli.main(raw)
    if resource == 'agents' and index + 1 < len(raw) and raw[index + 1] not in {'list', '--help', '-h'}:
        return api_cli.main(raw)
    if resource == 'guide':
        return api_cli.main(raw)
    parser = _parser()
    args = None
    try:
        if resource is None:
            parser.print_help()
            return 0
        if resource == 'auth' and index + 1 == len(raw):
            print('登录：staffdeck login\n查看连接：staffdeck auth status\n退出：staffdeck logout')
            return 0
        args = parser.parse_args(raw)
        if args.command == 'auth':
            return _auth(args)
        with _connection(args) as client:
            if args.command == 'chat':
                return _chat(client, args)
            if args.command in {'agents', 'agent'}:
                rows = _rows(client, getattr(args, 'limit', 100))
                print('名称\tID\t状态')
                for row in rows:
                    print(f'{_clean(row["name"])}\t{_clean(row["id"])}\t{_clean(row["status"])}')
                if not rows:
                    print('暂无可用员工。')
                return 0
    except api_cli.UsageError:
        print('尚未连接，请运行 staffdeck login。开源版默认账号密码均为 admin。', file=sys.stderr)
        return 2
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print('\n已退出；已提交的远端运行不会自动取消。', file=sys.stderr)
        return 130
    except WaitTimeout as exc:
        print(f'等待超时，运行仍可能继续。查看：staffdeck runs get --run-id {_clean(exc.run_id)}', file=sys.stderr)
        return 5
    except APIError as exc:
        print(f'服务返回 HTTP {exc.status_code}。请用 staffdeck auth status 检查连接。', file=sys.stderr)
        return 1
    except (StaffDeckError, ValueError, OSError):
        print('连接或执行失败，请检查本地服务和模型配置。运行 staffdeck --help 查看用法。', file=sys.stderr)
        if args is not None and getattr(args, 'last_run_id', None):
            print(f'查看运行：staffdeck runs get --run-id {_clean(args.last_run_id)}', file=sys.stderr)
        return 3
    return 0
