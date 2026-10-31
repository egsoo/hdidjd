import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .screenshotbot import ScreenShotBot
from .config import Config


BOT_CLIENT = None


async def download_message_media(chat_id, message_id):
    if BOT_CLIENT is None:
        raise RuntimeError("Telegram client is not initialized")

    message = await BOT_CLIENT.get_messages(chat_id, message_id)
    if message.empty or not message.media:
        return None, None, None

    file_name = getattr(message, "file_name", None) or f"{chat_id}_{message_id}"
    mime_type = None
    if getattr(message, "document", None) is not None:
        mime_type = message.document.mime_type or "application/octet-stream"
    elif getattr(message, "video", None) is not None:
        mime_type = message.video.mime_type or "video/mp4"
    elif getattr(message, "audio", None) is not None:
        mime_type = message.audio.mime_type or "audio/mpeg"
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

    return data, file_name, mime_type


class HealthHandler(BaseHTTPRequestHandler):
    def _safe_write(self, payload: bytes):
        try:
            if payload:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/health", "/healthz"):
            body = b"OK"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self._safe_write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        if path.startswith("/file/"):
            try:
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "file":
                    chat_id = int(parts[1])
                    message_id = int(parts[2])
                    data, file_name, mime_type = asyncio.run(
                        download_message_media(chat_id, message_id)
                    )
                    if data is None:
                        raise ValueError("Media not found")

                    try:
                        self.send_response(200)
                        self.send_header(
                            "Content-Type", mime_type or "application/octet-stream"
                        )
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header(
                            "Content-Disposition",
                            f'inline; filename="{file_name or "media"}"',
                        )
                        self.end_headers()
                        self._safe_write(data)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return
            except Exception as exc:
                try:
                    logging.exception("Failed to serve file for %s", self.path)
                    body = str(exc).encode()
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self._safe_write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

        try:
            self.send_response(404)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

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

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    BOT_CLIENT.run()
