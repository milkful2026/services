"""Thin local HTTP shim for Lambda-shaped `handler(event, context)`
functions — translates plain HTTP requests into the same `event` dict
shape API Gateway HTTP API v2's Lambda proxy integration produces, and
the handler's returned `{"statusCode", "headers", "body"}` dict back
into an HTTP response. The exact handler code that runs in Lambda runs
here unmodified; each service's own run_local.py just supplies a
{(method, path): handler} route table.

JWT claims: real API Gateway verifies the token's signature via its
Cognito JWT authorizer *before* invoking the Lambda. This shim does not
verify anything — it decodes the JWT payload as-is via PyJWT's
`verify_signature=False` mode (moto's own Cognito tokens aren't properly
signed either) and passes it through as `requestContext.authorizer.jwt.
claims`. Fine for a developer's own machine; never a stand-in for the
real authorizer anywhere else. Requires PyJWT — already a dependency of
identity-auth; added to user/inventory's requirements-dev.txt purely for
this shim (their own handler code never imports it).

CORS: a real API Gateway deployment sits behind whatever origin the
Flutter app is served from, so cross-origin isn't a concern there. This
shim serves the Flutter web build (a different localhost port) directly
though, so every response needs CORS headers and OPTIONS preflight
requests need answering, or the browser silently blocks every call
before it even reaches this handler — discovered by actually running
the app in Chrome, not by curl/dart, since neither of those enforce
CORS. `Access-Control-Allow-Origin: *` is fine here specifically because
this is local-dev-only, never-deployed tooling with no cookies/
credentialed requests involved (auth is a Bearer header, not a cookie).
"""

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt

_PARAM_SEGMENT = re.compile(r"\{([^{}/]+)\}")


def _compile_pattern(path: str) -> re.Pattern[str] | None:
    """`/cart/items/{id}` -> a regex matching `/cart/items/<anything>`,
    capturing `id` as a named group — `None` if `path` has no `{...}`
    segment (the common case; exact-match dict lookup handles those, this
    is only a fallback for the rare path-parameterized route none of the
    other four services needed until Cart's DELETE /cart/items/{id})."""
    if "{" not in path:
        return None
    pattern = _PARAM_SEGMENT.sub(lambda m: f"(?P<{m.group(1)}>[^/]+)", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
    return re.compile(f"^{pattern}$")


def _match_parameterized_route(routes: dict, method: str, path: str):
    """Falls back to this only when no exact (method, path) key matches —
    keeps the common case (every route with no path parameter) an O(1)
    dict lookup, unchanged from before this existed."""
    for (route_method, route_path), route_fn in routes.items():
        if route_method != method or "{" not in route_path:
            continue
        compiled = _compile_pattern(route_path)
        match = compiled.match(path)
        if match:
            return route_fn, match.groupdict()
    return None, None


def _decode_jwt_claims(auth_header: str | None) -> dict:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return {}
    token = auth_header.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload if isinstance(payload, dict) else {}
    except jwt.PyJWTError:
        return {}


_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Max-Age": "86400",
}


def _make_handler(routes: dict):
    class Handler(BaseHTTPRequestHandler):
        def _write(self, status: int, body: bytes, extra_headers: dict | None = None) -> None:
            self.send_response(status)
            for key, value in _CORS_HEADERS.items():
                self.send_header(key, value)
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            # Preflight — the browser sends this before any request with a
            # non-"simple" Content-Type (application/json) or a custom
            # header (Authorization, X-Request-Id), and blocks the actual
            # request entirely if this isn't answered correctly.
            self._write(204, b"")

        def _dispatch(self, method: str) -> None:
            parsed = urlsplit(self.path)
            route_fn = routes.get((method, parsed.path))
            path_params: dict[str, str] = {}
            if route_fn is None:
                route_fn, path_params = _match_parameterized_route(routes, method, parsed.path)
                path_params = path_params or {}
            if route_fn is None:
                body = json.dumps({"error": f"no local route for {method} {parsed.path}"}).encode()
                self._write(404, body, {"Content-Type": "application/json"})
                return

            content_length = int(self.headers.get("Content-Length", 0) or 0)
            request_body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
            query_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            claims = _decode_jwt_claims(self.headers.get("Authorization"))
            headers = {k.lower(): v for k, v in self.headers.items()}

            event = {
                "body": request_body,
                "headers": headers,
                "queryStringParameters": query_params or None,
                "pathParameters": path_params or None,
                "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
            }

            try:
                response = route_fn(event, None)
            except Exception:
                # Real API Gateway would never see this — Lambda's own
                # invoke error handling returns a 502 to it. Without this,
                # an unhandled exception here (e.g. moto_server not up
                # yet) kills the connection with zero bytes written and
                # the client just sees "Empty reply from server", with no
                # indication of what actually went wrong.
                traceback.print_exc()
                body = json.dumps(
                    {"error": "local server: handler raised an unhandled exception, see server log"}
                ).encode()
                self._write(500, body, {"Content-Type": "application/json"})
                return

            status = response.get("statusCode", 200)
            resp_headers = dict(response.get("headers") or {})
            resp_body = response.get("body") or ""
            resp_body_bytes = resp_body.encode("utf-8")
            resp_headers.setdefault("Content-Type", "application/json")
            self._write(status, resp_body_bytes, resp_headers)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib signature
            print("[local-server] " + (format % args))

    return Handler


def serve(routes: dict, port: int) -> None:
    handler_cls = _make_handler(routes)
    # 0.0.0.0, not 127.0.0.1: binding to loopback only works when this
    # process *is* the machine (a native/host run) — inside a container,
    # the host's published port maps to the container's external
    # interface, which a loopback-only bind can never answer.
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)  # noqa: S104
    print(f"Serving {len(routes)} route(s) on http://localhost:{port}")
    for method, path in routes:
        print(f"  {method:6s} {path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
