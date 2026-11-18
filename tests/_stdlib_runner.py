"""标准库测试运行器：在无 pytest/flask 的环境里执行 tests/ 下的全部测试。

仅用于本地验证，不参与应用运行。通过 stub 提供 pytest 的 fixture/raises/mark
与最小 Flask，测试文件本身保持 pytest 原生写法不变。
"""
import contextlib
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- 最小 Flask stub（覆盖 app 实际用到的 API） ----------
class _Blueprint:
    def __init__(self, *a, **k):
        self.name = a[0] if a else ''
        self._deferred = []

    def _wrap(self, rule, methods):
        def deco(fn):
            self._deferred.append((rule, fn, methods))
            return fn
        return deco

    def get(self, rule, **k):
        return self._wrap(rule, ('GET',))

    def post(self, rule, **k):
        return self._wrap(rule, ('POST',))

    def route(self, rule, **k):
        return self._wrap(rule, tuple(k.get('methods', ('GET',))))


class _Rule:
    def __init__(self, rule, endpoint, methods):
        self.rule, self.endpoint, self.methods = rule, endpoint, methods


class _UrlMap:
    def __init__(self):
        self._rules = []

    def add(self, rule, endpoint, methods):
        self._rules.append(_Rule(rule, endpoint, methods))

    def iter_rules(self):
        return iter(self._rules)


class _Flask:
    def __init__(self, import_name):
        self.import_name = import_name
        self.config = {}
        self.view_functions = {}
        self.url_map = _UrlMap()

    def register_blueprint(self, bp):
        for rule, fn, methods in bp._deferred:
            endpoint = '%s.%s' % (bp.name, fn.__name__)
            self.view_functions[endpoint] = fn
            self.url_map.add(rule, endpoint, methods)

    def errorhandler(self, exc):
        def deco(fn):
            return fn
        return deco

    def get(self, rule):
        return self._add(rule, ('GET',))

    def post(self, rule):
        return self._add(rule, ('POST',))

    def _add(self, rule, methods):
        def deco(fn):
            self.view_functions[fn.__name__] = fn
            self.url_map.add(rule, fn.__name__, methods)
            return fn
        return deco

    def test_client(self):
        return _ManualClient(self)

    def app_context(self):
        return contextlib.nullcontext()


class _Request:
    def get_json(self, silent=False):
        return getattr(self, '_json', {})

    @property
    def args(self):
        return {}


flask = types.ModuleType('flask')
flask.Flask = _Flask
flask.Blueprint = _Blueprint


def _render_template(template, **ctx):
    # 真实渲染需要 Jinja；测试仅断言页面含 run_id，这里输出关键字段占位
    data = ctx.get('data', {}) or {}
    run = ctx.get('run', {}) or {}
    return 'PAGE %s %s' % (data.get('run_id', ''), run.get('run_id', ''))


flask.render_template = _render_template
flask.request = _Request()
flask.redirect = lambda loc: ('redirect', loc)
flask.jsonify = lambda *a, **k: (a[0] if a else (k if k else {}))


class _Response:
    def __init__(self, body='', status=200, headers=None, mimetype=None):
        self.data = body.encode('utf-8') if isinstance(body, str) else (body or b'')
        self.status_code = status
        self.headers = headers or {}
        self.mimetype = mimetype

    def get_json(self):
        return json.loads(self.data.decode('utf-8'))


flask.Response = _Response
sys.modules['flask'] = flask


# ---------- 最小 pytest stub ----------
class _Raises:
    def __init__(self, exc):
        self.exc, self.value = exc, None

    def __enter__(self):
        return self

    def __exit__(self, etype, evalue, tb):
        if etype is None:
            raise AssertionError('DID_NOT_RAISE %s' % self.exc.__name__)
        if not issubclass(etype, self.exc):
            return False
        self.value = evalue
        return True


class _Mark:
    @staticmethod
    def parametrize(*a, **k):
        def deco(fn):
            return fn
        return deco


class _Pytest(types.ModuleType):
    raises = staticmethod(_Raises)
    mark = _Mark

    @staticmethod
    def fixture(fn=None, **kw):
        def mark(f):
            f._is_fixture = True
            return f
        return mark(fn) if fn is not None else mark


sys.modules['pytest'] = _Pytest('pytest')


# ---------- 简易 test_client stub（手工匹配 url_map） ----------
class _Resp:
    def __init__(self, body, status):
        # 视图可能直接返回 flask.Response（CSV 下载）
        if body.__class__.__name__ == '_Response':
            self._body, self.status_code, self.data = None, body.status_code, body.data
        else:
            self._body, self.status_code = body, status
            self.data = body if isinstance(body, bytes) else json.dumps(
                body, ensure_ascii=False).encode('utf-8')

    def get_json(self):
        return self._body if self._body is not None else json.loads(
            self.data.decode('utf-8'))


class _Matched:
    def __init__(self, endpoint, arguments):
        self.endpoint, self.arguments = endpoint, arguments


class _ManualClient:
    def __init__(self, app):
        self.app = app

    def _match(self, path, method):
        parts = path.strip('/').split('/')
        candidates = []
        for rule in self.app.url_map.iter_rules():
            if method not in rule.methods:
                continue
            rp = rule.rule.strip('/').split('/')
            if len(rp) != len(parts):
                continue
            args, ok, static_segs = {}, True, 0
            for seg_p, seg_r in zip(parts, rp):
                if seg_r.startswith('<') and seg_r.endswith('>'):
                    args[seg_r[1:-1].split(':')[-1]] = seg_p
                else:
                    static_segs += 1
                    if seg_p != seg_r:
                        ok = False
                        break
            if ok:
                # Flask 优先静态段更多的规则（/api/runs 优先于 /api/runs/<run_id>）
                candidates.append((static_segs, rule, args))
        if not candidates:
            return _Matched(None, {})
        _, rule, args = max(candidates, key=lambda c: c[0])
        return _Matched(rule.endpoint, args)

    def _call(self, matched, json_body):
        if matched.endpoint is None:
            return _Resp({'code': 'NOT_FOUND', 'message': '入口不存在'}, 404)
        view = self.app.view_functions.get(matched.endpoint)
        old = getattr(flask.request, '_json', {})
        flask.request._json = json_body or {}
        try:
            from app.errors import DomainError
            try:
                return _Resp(view(**matched.arguments), 200)
            except DomainError as e:
                return _Resp({'code': e.code, 'message': e.message}, e.status)
        finally:
            flask.request._json = old

    def get(self, path):
        return self._call(self._match(path, 'GET'), None)

    def post(self, path, json=None):
        return self._call(self._match(path, 'POST'), json)


def _make_client():
    from app.main import create_app
    app = create_app()
    app.config['TESTING'] = True
    return _ManualClient(app)


# ---------- fixture 收集与依赖注入 ----------
def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Monkey:
    def __init__(self):
        self._stash = []

    def setenv(self, k, v):
        self._stash.append((k, os.environ.get(k)))
        os.environ[k] = v

    def undo(self):
        for k, old in reversed(self._stash):
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        self._stash.clear()


def build_fixtures(conftest_mod, tmp_path):
    mp = _Monkey()
    builtin = {'tmp_path': tmp_path, 'monkeypatch': mp}
    cache = {}

    def resolve(name, stack):
        if name in cache:
            return cache[name]
        if name in builtin:
            return builtin[name]
        fn = getattr(conftest_mod, name, None)
        if fn is None or not getattr(fn, '_is_fixture', False):
            raise RuntimeError('unknown fixture %s' % name)
        deps = list(inspect.signature(fn).parameters)
        val = fn(**{d: resolve(d, stack + [name]) for d in deps})
        cache[name] = val
        return val

    return resolve, mp


def main():
    tests_dir = ROOT / 'tests'
    conftest = load_module(tests_dir / 'conftest.py')
    total = passed = failed = 0
    failures = []
    for path in sorted(tests_dir.glob('test_*.py')):
        mod = load_module(path)
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith('test_'):
                continue
            total += 1
            resolve, mp = build_fixtures(conftest, Path(tempfile.mkdtemp()))
            try:
                deps = list(inspect.signature(fn).parameters)
                fn(**{d: resolve(d, []) for d in deps})
                passed += 1
            except Exception:
                failed += 1
                failures.append((path.name, name, traceback.format_exc()))
            finally:
                mp.undo()
    print('PASSED %d / %d, FAILED %d' % (passed, total, failed))
    for f, n, tb in failures:
        print('\n==== FAIL %s::%s ====' % (f, n))
        print(tb)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
