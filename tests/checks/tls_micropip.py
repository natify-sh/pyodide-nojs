# micropip and requests over the guest's own TLS: no host fetch, only the host's sockets.
import asyncio

import micropip

asyncio.run(micropip.install(["six", "requests"]))
import requests
import six

r = requests.get("https://pypi.org/pypi/micropip/json", timeout=30)
print("micropip over guest TLS: six", six.__version__, "| requests.get", r.status_code, r.json()["info"]["name"])
print("installed", sorted(micropip.list()))
