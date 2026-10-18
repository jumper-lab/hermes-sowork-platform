"""Child-only wire redirect. Production source/config gain no endpoint override."""
import os
import httpx

_original = httpx.AsyncClient.__init__


def _init(self, *args, **kwargs):
    if str(kwargs.get("base_url", "")) == "https://api.sowork.com/public":
        kwargs["base_url"] = os.environ["SOWORK_TEST_API"]
    _original(self, *args, **kwargs)


httpx.AsyncClient.__init__ = _init
