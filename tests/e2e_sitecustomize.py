"""Child-only wire redirect. Production source/config gain no endpoint override."""
import os
import json
import httpx

_original = httpx.AsyncClient.__init__


def _init(self, *args, **kwargs):
    if str(kwargs.get("base_url", "")) == "https://api.sowork.com/public":
        if os.environ.get("SOWORK_TEST_APIS"):
            from hermes_constants import get_hermes_home
            kwargs["base_url"] = json.loads(os.environ["SOWORK_TEST_APIS"])[str(get_hermes_home().resolve())]
        else:
            kwargs["base_url"] = os.environ["SOWORK_TEST_API"]
    _original(self, *args, **kwargs)


httpx.AsyncClient.__init__ = _init
