import os
from .app import run as _run

def run():
    # Ensure BOT_TOKEN is present either from env or config fallback handled in app.run
    _run()

if __name__ == "__main__":
    run()

