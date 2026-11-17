import os
import sys

import uvicorn

if __name__ == "__main__":
    loop = "asyncio:ProactorEventLoop" if sys.platform == "win32" else "auto"
    # Auto-reload hands the listening socket to a child process, and on Windows
    # the Proactor loop (which Playwright needs) cannot accept on a socket it was
    # handed: "WinError 87" and a server that listens but never answers. Reload
    # is off on Windows unless asked for with UVICORN_RELOAD=1.
    reload = os.environ.get("UVICORN_RELOAD", "0" if sys.platform == "win32" else "1") == "1"
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=reload, loop=loop)
