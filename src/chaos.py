"""Raw-TCP chaos listener — connection-level failures no HTTP framework can
produce. Each mode maps to a request path; the probe targets
`http://host:CHAOS_PORT/<mode>`:

  /rst      accept, read the request, abort with a TCP RST
  /empty    accept, read the request, close without any response bytes
  /partial  send half a response head, then close

These drive the laceNotifications `error` trigger (connection reset /
premature close) and generic connection-error handling. Stateless — safe
for multi-tenant use.
"""

import asyncio
import socket

REQUEST_LIMIT = 4096
READ_TIMEOUT = 5.0

PARTIAL_HEAD = b"HTTP/1.1 200 OK\r\nContent-Length: 1024\r\nContent-Ty"


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            data = await asyncio.wait_for(reader.read(REQUEST_LIMIT), READ_TIMEOUT)
        except asyncio.TimeoutError:
            data = b""
        request_line = data.split(b"\r\n", 1)[0] if data else b""
        path = request_line.split(b" ")[1] if request_line.count(b" ") >= 2 else b"/"

        if path.startswith(b"/partial"):
            writer.write(PARTIAL_HEAD)
            await writer.drain()
        elif not path.startswith(b"/empty"):
            # default (incl. /rst): abort with RST so clients report a
            # connection error rather than a clean EOF
            sock = writer.get_extra_info("socket")
            if sock is not None:
                import struct

                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_chaos_server(port: int) -> asyncio.AbstractServer:
    return await asyncio.start_server(_handle, host="0.0.0.0", port=port)
