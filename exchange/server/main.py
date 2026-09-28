"""The app that uvicorn runs:

    $env:ADMIN_SECRET="choose-a-password"     (PowerShell; on Render, set it in the dashboard)
    uvicorn exchange.server.main:app --workers 1

The server starts with no markets: the instructor creates them on the admin page (/admin),
logging in with ADMIN_SECRET. The room code students type is printed in the console and shown
on the admin page.
"""

import os

from exchange.engine import Exchange

from .app import create_app
from .room import Room, new_room_code


def build_app(environ):
    """The app for this server. Refuses to start without an admin secret."""
    admin_secret = environ.get("ADMIN_SECRET", "").strip()
    if not admin_secret:
        raise SystemExit(
            "ADMIN_SECRET is not set. Choose an admin password and set it first, e.g. in "
            'PowerShell: $env:ADMIN_SECRET="choose-a-password"'
        )

    room = Room(code=new_room_code(), exchange=Exchange())
    print(f"\n    ROOM CODE: {room.code}\n", flush=True)
    return create_app(room, admin_secret)


app = build_app(os.environ)
