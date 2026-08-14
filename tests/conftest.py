import sys
import types


if "websockets" not in sys.modules:
    websockets_stub = types.ModuleType("websockets")

    class _ConnectionClosed(Exception):
        pass

    class _WebSocketServerProtocol:
        pass

    async def _connect(*args, **kwargs):
        raise RuntimeError("websockets.connect is unavailable in tests")

    def _serve(*args, **kwargs):
        class _DummyServer:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _DummyServer()

    websockets_stub.ConnectionClosed = _ConnectionClosed
    websockets_stub.WebSocketServerProtocol = _WebSocketServerProtocol
    websockets_stub.connect = _connect
    websockets_stub.serve = _serve
    sys.modules["websockets"] = websockets_stub


if "fastapi" not in sys.modules:
    fastapi_stub = types.ModuleType("fastapi")
    responses_stub = types.ModuleType("fastapi.responses")

    class _HTMLResponse(str):
        pass

    class _WebSocket:
        async def accept(self):
            return None

        async def send_text(self, message):
            return None

        async def receive_text(self):
            raise RuntimeError("fastapi.WebSocket is unavailable in tests")

    class _WebSocketDisconnect(Exception):
        pass

    class _FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def websocket(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    fastapi_stub.FastAPI = _FastAPI
    fastapi_stub.WebSocket = _WebSocket
    fastapi_stub.WebSocketDisconnect = _WebSocketDisconnect
    responses_stub.HTMLResponse = _HTMLResponse
    fastapi_stub.responses = responses_stub
    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = responses_stub