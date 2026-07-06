"""Entrypoint: uvicorn in a background thread, pywebview owning the Cocoa
main loop, pystray tray attached to the same NSApp via run_detached().

Window close is intercepted and turned into hide, so monitors keep ticking with
the window closed, and the app is reopened (or quit) from the tray icon.
"""
import logging
import threading
import time

import pystray
import uvicorn
import webview
from PIL import Image, ImageDraw, ImageFont

from . import config
from .server import create_app

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)

APP_URL = f"http://{config.HOST}:{config.PORT}"

window: webview.Window | None = None
uvicorn_server: uvicorn.Server | None = None
tray: pystray.Icon | None = None
_quitting = False


def make_tray_image() -> Image.Image:
    img = Image.new("RGB", (64, 64), (255, 102, 0))  # HN orange
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 44)
    except OSError:
        font = ImageFont.load_default()
    d.text((32, 30), "Y", fill="white", anchor="mm", font=font)
    return img


def run_server() -> None:
    global uvicorn_server
    uvicorn_config = uvicorn.Config(
        create_app(), host=config.HOST, port=config.PORT, log_level="warning"
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)
    uvicorn_server.run()  # creates its own asyncio loop on this thread


def on_closing() -> bool:
    if _quitting:
        return True
    window.hide()
    return False  # cancel close; workers keep running


def show_window() -> None:
    if window is not None:
        window.show()


def quit_app() -> None:
    global _quitting
    _quitting = True
    if uvicorn_server is not None:
        uvicorn_server.should_exit = True
    if tray is not None:
        tray.stop()
    if window is not None:
        window.destroy()


def main() -> None:
    global window, tray

    server_thread = threading.Thread(target=run_server, name="server")
    server_thread.start()
    # wait for the port to accept connections before pointing the webview at it
    import socket
    for _ in range(100):
        try:
            with socket.create_connection((config.HOST, config.PORT), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    tray = pystray.Icon(
        "hnwatch", make_tray_image(), "HN Watch",
        menu=pystray.Menu(
            pystray.MenuItem("Open HN Watch", show_window, default=True),
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    tray.run_detached()

    window = webview.create_window(
        "HN Watch", APP_URL, width=1150, height=780, min_size=(800, 560)
    )
    window.events.closing += on_closing

    webview.start()  # blocks until window.destroy()

    if uvicorn_server is not None:
        uvicorn_server.should_exit = True
    server_thread.join(timeout=10)


if __name__ == "__main__":
    main()
