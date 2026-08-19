"""Start the web app:  python -m webapp"""

import os
import sys
import webbrowser

import uvicorn

PORT = int(os.environ.get("PORT", "8000"))

if __name__ == "__main__":
    url = "http://127.0.0.1:%d" % PORT
    print("\n  Email Verifier running at  %s\n" % url)
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    uvicorn.run("webapp.main:app", host="127.0.0.1", port=PORT, log_level="warning")
