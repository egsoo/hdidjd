import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .screenshotbot import ScreenShotBot
from .config import Config


BOT_CLIENT = None
MAIN_LOOP = None

# (chat_id, message_id) -> (bytes, file_name, mime_type)
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX_BYTES = 512 * 1024 * 1024


async def download_message_media(chat_id, message_id):
    if BOT_CLIENT is None:
        raise RuntimeError("Telegram client is not initialized")

    message = await BOT_CLIENT.get_messages(chat_id, message_id)
    if message.empty or not message.media:
        return None, None, None

    file_name = getattr(message, "file_name", None) or f"{chat_id}_{message_id}"
    mime_type = None
    expected_size = None

    if getattr(message, "document", None) is not None:
        mime_type = message.document.mime_type or "application/octet-stream"
        expected_size = message.document.file_size
    elif getattr(message, "video", None) is not None:
        mime_type = message.video.mime_type or "video/mp4"
        expected_size = message.video.file_size
    elif getattr(message, "audio", None) is not None:
        mime_type = message.audio.mime_type or "audio/mpeg"
        expected_size = message.audio.file_size
    elif getattr(message, "photo", None) is not None:
        mime_type = "image/jpeg"
    else:
        mime_type = "application/octet-stream"

    media = await message.download(in_memory=True)
    if media is None:
        return None, None, None

    if isinstance(media, (bytes, bytearray)):
        data = bytes(media)
    elif hasattr(media, "read"):
        data = media.read()
    elif isinstance(media, str):
        with open(media, "rb") as fp:
            data = fp.read()
        os.remove(media)
    else:
        data = bytes(media)

    if expected_size is not None and len(data) != expected_size:
        logging.warning(
            "Incomplete media for %s/%s: got %d bytes, expected %d",
            chat_id, message_id, len(data), expected_size,
        )

    return data, file_name, mime_type


def _fetch_media(chat_id, message_id):
    """Run the download coroutine on Pyrogram's loop (thread-safe)."""
    key = (chat_id, message_id)

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached

    if MAIN_LOOP is None or MAIN_LOOP.is_closed():
        raise RuntimeError("Main event loop is not ready")

    future = asyncio.run_coroutine_threadsafe(
        download_message_media(chat_id, message_id), MAIN_LOOP
    )
    result = future.result(timeout=180)

    if result[0] is not None:
        with _CACHE_LOCK:
            _CACHE[key] = result
            total = sum(len(v[0]) for v in _CACHE.values())
            while total > _CACHE_MAX_BYTES and len(_CACHE) > 1:
                evicted_key = next(iter(_CACHE))
                evicted = _CACHE.pop(evicted_key)
                total -= len(evicted[0])

    return result


class HealthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _safe_write(self, payload: bytes):
        try:
            if payload:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_full(self, data, file_name, mime_type, send_body):
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Content-Disposition",
                f'inline; filename="{file_name or "media"}"',
            )
            self.end_headers()
            if send_body:
                self._safe_write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_range(self, data, file_name, mime_type, range_header, send_body):
        total = len(data)

        # Parse "bytes=START-END" (supports suffix form "bytes=-N")
        try:
            spec = range_header.split("=", 1)[1]
            start_s, _, end_s = spec.partition("-")
            if not start_s:
                # suffix range: last N bytes
                length = int(end_s) if end_s else 0
                if length <= 0:
                    raise ValueError("empty suffix range")
                start = max(0, total - length)
                end = total - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else total - 1
        except (ValueError, IndexError):
            # Malformed Range header — respond with the full body instead
            return self._serve_full(data, file_name, mime_type, send_body)

        if start < 0 or start >= total:
            try:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        end = min(end, total - 1)
        chunk = data[start:end + 1]

        try:
            self.send_response(206)
            self.send_header("Content-Type", mime_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Content-Disposition",
                f'inline; filename="{file_name or "media"}"',
            )
            self.end_headers()
            if send_body:
                self._safe_write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_simple(self, status, body: bytes, send_body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self._safe_write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle(self, send_body: bool):
        path = urlparse(self.path).path

        if path in ("/", "/health", "/healthz"):
            self._send_simple(200, b"OK", send_body)
            return

        if path.startswith("/file/"):
            try:
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "file":
                    chat_id = int(parts[1])
                    message_id = int(parts[2])
                    data, file_name, mime_type = _fetch_media(chat_id, message_id)
                    if data is None:
                        raise ValueError("Media not found")

                    range_header = self.headers.get("Range")
                    if range_header:
                        self._serve_range(
                            data, file_name, mime_type, range_header, send_body
                        )
                    else:
                        self._serve_full(data, file_name, mime_type, send_body)
                    return
            except Exception:
                logging.exception("Failed to serve file for %s", self.path)
                self._send_simple(404, b"Not found", send_body)
                return

        self._send_simple(404, b"Not found", send_body)

    def do_GET(self):
        self._handle(send_body=True)

    def do_HEAD(self):
        self._handle(send_body=False)

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG if Config.DEBUG else logging.INFO)
    logging.getLogger("pyrogram").setLevel(
        logging.INFO if Config.DEBUG else logging.WARNING
    )

    BOT_CLIENT = ScreenShotBot()

    # Pyrogram must own the event loop. Any `await` on its client (including
    # `Message.download`) must be scheduled on this loop, otherwise the future
    # is orphaned across loops and downloads silently truncate — which is
    # exactly what was producing the variable-offset "partial file" errors
    # in FFmpeg's logs.
    MAIN_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(MAIN_LOOP)

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    BOT_CLIENT.run()
