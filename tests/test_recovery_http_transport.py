# Generated-By: Codex / gpt-6-astra
"""Prepared direct-origin recovery requests have an absolute socket deadline."""

import socket
import http.client
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llmsvc.actions import ManagedModelTransport


@pytest.fixture
def endpoint():
    state={"mode":"fast","paths":[],"release":threading.Event()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            state["paths"].append(self.path)
            try:
                if state["mode"]=="headers":
                    state["release"].wait(2)
                    return
                if state["mode"]=="trickle":
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Progress: ")
                    while not state["release"].wait(.02):
                        self.connection.sendall(b"x")
                    return
                self.send_response(200);self.send_header("Content-Length","0");self.end_headers()
            except OSError:
                pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01));thread.start()
    transport=ManagedModelTransport(swap_url="http://127.0.0.1:"+str(server.server_port),
        models={"source":{"unit":"vllm-source.service"}},systemctl="unused")
    try:
        yield transport,state
    finally:
        state["release"].set();server.shutdown();server.server_close();thread.join(3)


def test_prepared_recovery_origin_is_immutable_and_uses_no_resolver(endpoint,monkeypatch):
    transport,state=endpoint
    prepared=transport.prepare_http("POST","/api/models/unload/source",bounded=True)
    transport.swap_url="http://127.0.0.1:1"
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:pytest.fail("bounded request resolved a host"))
    assert prepared(deadline=time.monotonic()+2)==200
    assert state["paths"]==["/api/models/unload/source"]


@pytest.mark.parametrize("mode",["headers","trickle"])
def test_header_stalls_and_trickles_cannot_extend_absolute_deadline(endpoint,mode):
    transport,state=endpoint;state["mode"]=mode
    prepared=transport.prepare_http("POST","/api/models/unload/source",bounded=True)
    started=time.monotonic()
    try:
        with pytest.raises((OSError,http.client.HTTPException)):
            prepared(deadline=started+.15)
        assert time.monotonic()-started<1
        assert state["paths"]==["/api/models/unload/source"]
    finally: state["release"].set()


@pytest.mark.parametrize("origin",["http://localhost:8000","http://127.0.0.1:0","http://127.0.0.1:bad"])
def test_unbounded_or_invalid_recovery_origin_is_refused_before_request(endpoint,origin):
    transport,state=endpoint;transport.swap_url=origin
    with pytest.raises(ValueError):
        transport.prepare_http("POST","/api/models/unload/source",bounded=True)
    assert state["paths"]==[]
