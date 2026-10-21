"""Loopback SoWork REST/SSE stand-in; real HTTP, OAuth headers and idempotency."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

DM_EVENT = "chat.direct_message_created"
THREAD_EVENT = "chat.thread_reply_created"


class SoWorkServer:
    def __init__(self):
        self.events, self.requests, self.parents = [], [], {}
        self.deliveries, self.previews = {}, {}
        self.stream_cursors, self.poll_cursors = [], []
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.generation = 0
        self.fail_after_accept = False
        self.get_failures = {}
        self.subscriptions = {"sub": {"id": "sub", "officeId": "workspace", "events": [DM_EVENT, THREAD_EVENT], "creator": {"id": "app"}}}
        self.fail_subscription_after_accept = False
        self.dm_participants = [{"id": "human"}]
        self.owner_user_id = None

    def start(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):pass
            def do_GET(self):owner.handle(self)
            def do_POST(self):owner.handle(self)
            def do_PATCH(self):owner.handle(self)
            def do_DELETE(self):owner.handle(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):return f"http://127.0.0.1:{self.server.server_port}/public"

    def close(self):
        self.closed.set();self.server.shutdown();self.server.server_close();self.thread.join(5)

    def message(self, text, *, sender="human", root=None, mid=None):
        mid = mid or str(uuid4())
        msg = {"id": mid, "text": text, "channel": {"id": "dm", "type": "dm"},
               "sender": {"id": sender}, "createdAt": time.time()}
        data = {"message": msg, "channelId": "dm", "conversationType": "direct", "memberCount": 2}
        if root:
            msg["parentId"] = root
            data.update(threadRootId=root, channel={"id": "dm", "type": "dm"})
        event = {"id": str(uuid4()), "type": THREAD_EVENT if root else DM_EVENT,
                 "officeId": "workspace", "installationId": "install", "createdAt": time.time(), "data": data}
        self.replay(event)
        return event

    def replay(self, event):
        with self.lock:self.events.append({"logId": f"opaque/{len(self.events)+1}", "event": copy.deepcopy(event)})

    def after(self, cursor):
        with self.lock:
            start = next((i+1 for i,e in enumerate(self.events) if e["logId"] == cursor), 0)
            return copy.deepcopy(self.events[start:])

    def visible(self):
        with self.lock:return copy.deepcopy(list(self.deliveries.values()))

    def visuals(self):
        with self.lock:return copy.deepcopy(list(self.previews.values()))

    def handle(self, h):
        path = urlsplit(h.path).path.removeprefix("/public")
        query = parse_qs(urlsplit(h.path).query)
        content = h.rfile.read(int(h.headers.get("Content-Length", "0")))
        body = json.loads(content) if content and h.headers.get("Content-Type", "").startswith("application/json") else None
        with self.lock:self.requests.append((h.command, path, body))
        def reply(value, status=200):
            payload = json.dumps(value).encode();h.send_response(status)
            h.send_header("Content-Type", "application/json");h.send_header("Content-Length", str(len(payload)))
            if status == 503:h.send_header("Retry-After", "0")
            h.end_headers();h.wfile.write(payload)
        if path == "/oauth/token":
            assert b"client_id=test-client" in content and b"installation_id=install" in content
            return reply({"access_token": "synthetic-access", "expires_in": 3600})
        if h.headers.get("Authorization") != "Bearer synthetic-access":return reply({}, 401)
        if h.command == "GET":
            with self.lock:
                failures = self.get_failures.get(path, [])
                failure = failures.pop(0) if failures else None
            if failure:
                return reply({"error": {"code": failure[1]}}, failure[0])
        if path == "/v1/app":return reply({"installation": {"id": "install", "state": "active", "consentedScopes": ["chat.dm", "chat.write", "chat.history", "events.subscribe:"+DM_EVENT, "events.subscribe:"+THREAD_EVENT], **({"ownerUserId": self.owner_user_id} if self.owner_user_id else {})}, "group": {"id": "workspace"}, "appUser": {"id": "app"}})
        if path == "/v1/subscriptions":
            if h.command == "POST":
                with self.lock:
                    sid = "setup-" + str(len(self.subscriptions))
                    sub = {"id": sid, "officeId": "workspace", "events": body["events"], "creator": {"id": "app"}}
                    self.subscriptions[sid] = sub
                    if self.fail_subscription_after_accept:
                        self.fail_subscription_after_accept = False
                        h.close_connection = True
                        return
                return reply(sub)
            with self.lock:return reply({"webhooks": copy.deepcopy(list(self.subscriptions.values()))})
        if path == "/v1/chat/dms":return reply({"conversations": [{"id": "dm", "type": "direct", "participants": self.dm_participants}]})
        if path.endswith("/replies"):return reply({"parent": self.parents[path.split("/")[-2]], "messages": []})
        if path == "/v1/events":
            cursor = query.get("after", [None])[0]
            with self.lock:self.poll_cursors.append(cursor)
            return reply({"items": self.after(cursor), "hasMore": False})
        if path == "/v1/events/stream":
            cursor = h.headers.get("Last-Event-ID")
            with self.lock:
                self.stream_cursors.append(cursor);generation = self.generation
            h.send_response(200);h.send_header("Content-Type", "text/event-stream");h.end_headers()
            try:
                while not self.closed.is_set():
                    for entry in self.after(cursor):
                        h.wfile.write(("id: "+entry["logId"]+"\nevent: message\ndata: "+json.dumps(entry["event"])+"\n\n").encode())
                        cursor = entry["logId"]
                    if generation != self.generation:
                        h.wfile.write(b"event: goodbye\ndata: {}\n\n");h.wfile.flush();return
                    h.wfile.write(b": heartbeat\n\n");h.wfile.flush()
                    self.closed.wait(.05)
            except (BrokenPipeError, ConnectionResetError):pass
            return
        if path == "/v1/chat/channels/dm/messages":
            key = h.headers.get("Idempotency-Key")
            assert key
            with self.lock:
                previous = self.deliveries.get(key)
                assert previous is None or previous["body"] == body
                receipt = previous or {"id": str(uuid4()), "sender": {"id": "app"}, "channel": {"id": "dm"}, "body": body, **({"parentId": body["parentId"]} if body.get("parentId") else {})}
                self.deliveries[key] = receipt
                fail, self.fail_after_accept = self.fail_after_accept, False
            return reply({"error": {"code": "temporary"}} if fail else receipt, 503 if fail else 200)
        if path == "/v1/chat/channels/dm/ephemeral-messages":
            mid = str(uuid4())
            with self.lock:self.previews[mid] = body
            return reply({"id": mid, "channelId": "dm", "visibleToUserId": "human", "senderId": "app", **({"parentId": body["parentId"]} if body.get("parentId") else {})})
        if path.startswith("/v1/chat/ephemeral-messages/"):
            mid = path.rsplit("/", 1)[-1]
            with self.lock:
                if h.command == "DELETE":self.previews.pop(mid, None)
                elif mid in self.previews:self.previews[mid].update(body)
            return reply({})
        return reply({"error": {"code": "unexpected_test_endpoint"}}, 404)
