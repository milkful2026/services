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
"""

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt


def _decode_jwt_claims(auth_header: str | None) -> dict:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return {}
    token = auth_header.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload if isinstance(payload, dict) else {}
    except jwt.PyJWTError:
        return {}


def _make_handler(routes: dict):
    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            parsed = urlsplit(self.path)
            route_fn = routes.get((method, parsed.path))
            if route_fn is None:
                body = json.dumps({"error": f"no local route for {method} {parsed.path}"}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            status = response.get("statusCode", 200)
            resp_headers = dict(response.get("headers") or {})
            resp_body = response.get("body") or ""
            resp_body_bytes = resp_body.encode("utf-8")

            self.send_response(status)
            resp_headers.setdefault("Content-Type", "application/json")
            for key, value in resp_headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp_body_bytes)))
            self.end_headers()
            if resp_body_bytes:
                self.wfile.write(resp_body_bytes)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib signature
            print("[local-server] " + (format % args))

    return Handler


def serve(routes: dict, port: int) -> None:
    handler_cls = _make_handler(routes)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    print(f"Serving {len(routes)} route(s) on http://localhost:{port}")
    for method, path in routes:
        print(f"  {method:6s} {path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
