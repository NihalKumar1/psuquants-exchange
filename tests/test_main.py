"""Starting the real server: it needs an admin secret and starts with no markets."""

import importlib

import pytest


@pytest.fixture
def main(monkeypatch):
    # Importing the module builds the app, so it needs a secret in the environment.
    monkeypatch.setenv("ADMIN_SECRET", "test-secret")
    return importlib.import_module("exchange.server.main")


@pytest.mark.parametrize("environ", [{}, {"ADMIN_SECRET": "   "}])
def test_the_server_refuses_to_start_without_an_admin_secret(main, environ):
    with pytest.raises(SystemExit, match="ADMIN_SECRET"):
        main.build_app(environ)


def test_the_server_starts_with_no_markets_and_a_four_digit_room_code(main):
    app = main.build_app({"ADMIN_SECRET": "x"})

    room = app.state.room
    assert room.exchange.markets == {}
    assert len(room.code) == 4 and room.code.isdigit()
