"""Spike: pywebview main thread + pystray run_detached + hide-on-close + osascript.

Self-driving: hides the window after 3s, shows it at 6s, fires a notification,
auto-quits at 12s. Logs each step to shell_spike.log.
"""
import subprocess
import threading
import time

import pystray
import webview
from PIL import Image, ImageDraw

LOG = open("shell_spike.log", "w", buffering=1)


def log(msg):
    LOG.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def make_icon_image():
    img = Image.new("RGB", (64, 64), (255, 102, 0))  # HN orange
    d = ImageDraw.Draw(img)
    d.text((22, 20), "Y", fill="white")
    return img


def notify(title, message):
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{message}" with title "{title}"'],
        capture_output=True, timeout=10,
    )
    log("notification fired")


window = None
tray = None


def on_closing():
    log("window closing event -> intercept, hide instead")
    window.hide()
    return False  # cancel the close


def driver():
    time.sleep(3)
    log("driver: hiding window")
    window.hide()
    time.sleep(3)
    log("driver: showing window")
    window.show()
    notify("HN Watch spike", "tray + hide/show + notification all work")
    time.sleep(3)
    log("driver: quitting")
    if tray is not None:
        tray.stop()
        log("tray stopped")
    window.destroy()


def main():
    global window, tray
    log("pywebview + pystray backend loading")

    tray = pystray.Icon(
        "hnwatch", make_icon_image(), "HN Watch",
        menu=pystray.Menu(
            pystray.MenuItem("Open HN Watch", lambda: window.show()),
            pystray.MenuItem("Quit", lambda: window.destroy()),
        ),
    )
    tray.run_detached()
    log("pystray run_detached() returned OK")

    window = webview.create_window(
        "HN Watch (spike)", html="<h1>shell spike</h1>", width=500, height=300
    )
    window.events.closing += on_closing

    threading.Thread(target=driver, daemon=True).start()
    log("starting webview (Cocoa main loop)")
    webview.start()
    log("webview.start() returned -> clean exit")


if __name__ == "__main__":
    main()
