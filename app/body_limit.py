"""Reject request bodies larger than a fixed size before the application reads them.

A pure ASGI middleware, so a body is never read ahead of the limit. A declared Content-Length over the
limit is refused at once without reading anything. The bytes actually received are counted as well,
because the header may be absent (chunked uploads) or may understate the real size.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 64 * 1024


def _json_response(status: int, detail: str) -> tuple[Message, Message]:
    body = json.dumps({"detail": detail}).encode()
    start = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),
        ],
    }
    return start, {"type": "http.response.body", "body": body}


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is None:
            await self._respond(send, 400, "Invalid Content-Length header")
            return
        if declared > self.max_bytes:
            await self._respond(send, 413, "Request body too large")
            return

        received = 0
        refused = False  # set once the limit is crossed mid-stream
        responded = False  # set once the application has started its own response

        async def limited_receive() -> Message:
            nonlocal received, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    refused = True
                    if not responded:
                        await self._respond(send, 413, "Request body too large")
                    # The application sees a disconnect instead of the rest of the body.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal responded
            if refused:  # whatever the application says after the limit was crossed is dropped
                return
            responded = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)

    def _declared_length(self, scope: Scope) -> int | None:
        """The largest declared Content-Length (0 if absent), or None if any value is malformed."""
        largest = 0
        for name, value in scope["headers"]:
            if name == b"content-length":
                if not value.isascii() or not value.isdigit():
                    return None
                digits = value.lstrip(b"0")
                # Python refuses to convert thousands of digits to an int; so many digits is over any limit anyway.
                largest = max(largest, int(digits or b"0") if len(digits) <= 18 else self.max_bytes + 1)
        return largest

    async def _respond(self, send: Send, status: int, detail: str) -> None:
        start, body = _json_response(status, detail)
        await send(start)
        await send(body)
