import os
from functools import partial

import sentry_sdk
from flask import Flask, json, abort, jsonify, request
from semver import VersionInfo
from sentry_sdk.integrations.flask import FlaskIntegration
from werkzeug.contrib.cache import SimpleCache


# SENTRY_DSN will be taken from env
sentry_sdk.init(integrations=[FlaskIntegration()])

CACHE_TIMEOUT = 3600
cache = SimpleCache(threshold=200, default_timeout=CACHE_TIMEOUT)


class InvalidPathComponent(ValueError):
    pass


class RegistryJsonEncoder(json.JSONEncoder):

    def default(self, o):
        if hasattr(o, 'to_json'):
            return o.to_json()
        return json.JSONEncoder.default(self, o)


class RegistryFlask(Flask):
    json_encoder = RegistryJsonEncoder

    def make_response(self, result):
        if isinstance(result, ApiResponse):
            return jsonify(result.data)
        return Flask.make_response(self, result)


def validate_path_component(path):
    if not path:
        raise InvalidPathComponent('Invalid path')
    for item in path.split('/'):
        if item == '.' or item == '..' or not item:
            raise InvalidPathComponent('Invalid path %s' % path)


class ApiResponse(object):

    def __init__(self, data):
        self.data = data


class PackageInfo(object):

    def __init__(self, registry, data):
        self._registry = registry
        self._data = data

    @property
    def canonical(self):
        return self._data['canonical']

    @property
    def sdk_id(self):
        return self._data.get('sdk_id')

    @property
    def version(self):
        return self._data['version']

    def to_json(self):
        return self._data


class Registry(object):

    def __init__(self):
        self.path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _path(self, *args):
        for arg in args:
            validate_path_component(arg)
        return os.path.join(self.path, *args)

    def get_package(self, canonical, version='latest'):
        """Looks up a package by canonical version"""
        if ':' not in canonical:
            return
        registry, package = canonical.split(':', 1)
        try:
            path = self._path('packages', registry, package, '%s.json' % version)
            with open(path) as f:
                return PackageInfo(self, json.load(f))
        except (IOError, OSError):
            return

    def get_package_versions(self, canonical):
        """Returns all versions of a package."""
        if ':' not in canonical:
            return
        registry, package = canonical.split(':', 1)
        rv = set()
        for filename in os.listdir(self._path('packages', registry, package)):
            if filename.endswith('.json'):
                with open(self._path('packages', registry, package, filename)) as f:
                    rv.add(json.load(f)['version'])
        return sorted(rv, key=VersionInfo.parse)

    def iter_packages(self):
        for package_registry in os.listdir(self._path('packages')):
            for item in os.listdir(self._path('packages', package_registry)):
                if os.path.exists(os.path.join(
                        self._path('packages', package_registry, item, '__NAMESPACE__'))):
                    for subitem in os.listdir(self._path('packages', package_registry, item)):
                        yield '%s:%s/%s' % (package_registry, item, subitem)
                else:
                    yield '%s:%s' % (package_registry, item)

    def get_packages(self):
        rv = {}
        for package_name in self.iter_packages():
            pkg = self.get_package(package_name)
            if pkg is not None:
                rv[pkg.canonical] = pkg
        return rv

    def get_sdks(self):
        rv = {}
        for link in os.listdir(self._path('sdks')):
            try:
                with open(self._path('sdks', link, 'latest.json')) as f:
                    canonical = json.load(f)['canonical']
                    pkg = self.get_package(canonical)
                    if pkg is not None:
                        rv[link] = pkg
            except (IOError, OSError):
                continue
        return rv

    def get_sdk(self, sdk_id, version='latest'):
        try:
            with open(self._path('sdks', sdk_id, 'latest.json')) as f:
                canonical = json.load(f)['canonical']
                return self.get_package(canonical, version)
        except (IOError, OSError):
            pass

    def get_apps(self):
        rv = {}
        for link in os.listdir(self._path('apps')):
            try:
                app = self.get_app(link)
                if app is not None:
                    rv[link] = app
            except (IOError, OSError):
                continue
        return rv

    def get_app(self, app_id, version='latest'):
        try:
            with open(self._path('apps', app_id, '%s.json' % version)) as f:
                return json.load(f)
        except (IOError, OSError):
            pass

    def get_marketing_slugs(self):
        with open(self._path('misc', 'marketing-slugs.json')) as f:
            return json.load(f)

    def resolve_marketing_slug(self, slug):
        slugs = self.get_marketing_slugs()
        data = slugs.get(slug)
        if data is None:
            return
        target = None
        if data['type'] == 'sdk':
            target = self.get_sdk(data['target'])
        elif data['type'] == 'package':
            target = self.get_package(data['target'])
        elif data['type'] == 'integration':
            if data.get('sdk'):
                package = self.get_sdk(data['sdk'])
            elif data.get('package'):
                package = self.get_package(data['package'])
            else:
                package = None
            if package is not None:
                target = {
                    'package': package,
                    'integration': data['integration'],
                }
        return {
            'definition': data,
            'target': target,
        }


def is_caching_enabled():
    cache_env = os.getenv("REGISTRY_ENABLE_CACHE", "").strip()
    if cache_env == "1":
        return True
    elif cache_env == "0":
        return False
    return os.getenv('FLASK_ENV') == "production"


def return_cached():
    if not request.values:
        response = cache.get(request.path)
        if response:
            response.headers['X-From-Cache'] = '1'
            return response


def cache_response(response):
    if not request.values:
        # Make the response picklable
        response.freeze()
        cache.set(request.path, response)
    return response


def set_cache_enabled(app, enable: bool):
    app.config['CACHE_ENABLED'] = enable

    assert type(enable) == bool
    if enable:
        app.before_request(return_cached)
        app.after_request(cache_response)
    else:
        app.before_request_funcs = {}
        app.after_request_funcs = {}


app = RegistryFlask(__name__)
app.config.from_envvar('APISERVER_CONFIG', silent=True)
app.enable_cache = partial(set_cache_enabled, app)
app.enable_cache(is_caching_enabled())


@app.route('/packages/<path:package>/<version>')
def get_package_version(package, version):
    pkg_info = registry.get_package(package, version)
    if pkg_info is None:
        abort(404)
    return ApiResponse(pkg_info)


@app.route('/packages/<path:package>/versions')
def get_package_versions(package):
    latest_pkg_info = registry.get_package(package)
    if latest_pkg_info is None:
        abort(404)
    return ApiResponse({
        'latest': latest_pkg_info,
        'versions': registry.get_package_versions(package),
    })


@app.route('/marketing-slugs')
def get_marketing_slugs():
    return ApiResponse(dict(slugs=sorted(registry.get_marketing_slugs().keys())))


@app.route('/marketing-slugs/<slug>')
def resolve_marketing_slugs(slug):
    rv = registry.resolve_marketing_slug(slug)
    if rv is None:
        abort(404)
    return ApiResponse(rv)


@app.route('/sdks')
def get_sdk_summary():
    return ApiResponse(registry.get_sdks())


@app.route('/sdks/<sdk_id>/<version>')
def get_sdk_version(sdk_id, version):
    pkg_info = registry.get_sdk(sdk_id, version)
    if pkg_info is None:
        abort(404)
    return ApiResponse(pkg_info)


@app.route('/sdks/<sdk_id>/versions')
def get_sdk_versions(sdk_id):
    latest_pkg_info = registry.get_sdk(sdk_id)
    if latest_pkg_info is None:
        abort(404)
    return ApiResponse({
        'latest': latest_pkg_info,
        'versions': registry.get_package_versions(latest_pkg_info.canonical),
    })


@app.route('/packages')
def get_package_summary():
    return ApiResponse(registry.get_packages())


@app.route('/apps')
def get_app_summary():
    return ApiResponse(registry.get_apps())


@app.route('/apps/<app_id>/<version>')
def get_app_version(app_id, version):
    app_info = registry.get_app(app_id, version)
    if app_info is None:
        abort(404)
    return ApiResponse(app_info)


@app.route('/healthz')
def healthcheck():
    return "ok\n", 200


registry = Registry()
