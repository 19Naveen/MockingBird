"""Native provider adapters exposed to the HTTP shim."""

from . import bedrock, google

PROVIDERS = {"bedrock": bedrock, "google": google}
