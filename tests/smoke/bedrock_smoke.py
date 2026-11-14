"""End-to-end boto3 transport/runner check; uses a local fake CLI, never AWS or model access.

Run with boto3 installed: python tests/smoke/bedrock_smoke.py
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def client_check(mode):
    import boto3
    from botocore.config import Config
    import botocore.utils
    import urllib3.connectionpool

    def no_metadata(*args, **kwargs):
        raise AssertionError("dummy credentials must avoid metadata credential requests")

    botocore.utils.InstanceMetadataFetcher.retrieve_iam_role_credentials = no_metadata
    original = urllib3.connectionpool.HTTPConnectionPool.urlopen

    def local_only(pool, *args, **kwargs):
        if pool.host not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"refusing nonlocal request to {pool.host}")
        return original(pool, *args, **kwargs)

    urllib3.connectionpool.HTTPConnectionPool.urlopen = local_only
    assert os.environ["AWS_ACCESS_KEY_ID"] == "bridge-dummy"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "bridge-dummy"
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
    args = {"endpoint_url": "https://bedrock-runtime.us-east-1.amazonaws.com"} if mode == "hardcoded" else {}
    config = Config(retries={"max_attempts": 0}, connect_timeout=2, read_timeout=5)
    with closing(boto3.client("bedrock-runtime", region_name="us-east-1", config=config, **args)) as client:
        if mode == "environment":
            assert client.meta.endpoint_url == os.environ["AWS_ENDPOINT_URL_BEDROCK_RUNTIME"]
        else:
            assert client.meta.endpoint_url.startswith("https://bedrock-runtime.")
        request = {"modelId": "test-model", "messages": [{"role": "user", "content": [{"text": "hello"}]}]}
        # The explicitly supplied faults file injects exactly one failure, then normal behavior resumes.
        try:
            client.converse(**request)
        except client.exceptions.ThrottlingException as exc:
            assert exc.response["Error"]["Message"] == "scripted throttle"
        else:
            raise AssertionError("the first call should receive the explicitly scripted 429")
        reply = client.converse(**request)
        assert reply["output"]["message"]["content"] == [{"text": "hello"}]
        assert reply["usage"]["totalTokens"] == 0
        events = list(client.converse_stream(**request)["stream"])
        assert events[0] == {"messageStart": {"role": "assistant"}}
        assert events[1]["contentBlockDelta"]["delta"] == {"text": "hello"}
        assert events[-1]["metadata"]["usage"]["totalTokens"] == 0
        request["toolConfig"] = {"tools": [{"toolSpec": {"name": "lookup", "inputSchema": {"json": {"type": "object"}}}}],
                                 "toolChoice": {"tool": {"name": "lookup"}}}
        first = client.converse(**request)
        tool = first["output"]["message"]["content"][0]["toolUse"]
        assert tool["input"] == {"city": "Paris"}
        events = list(client.converse_stream(**request)["stream"])
        assert events[1]["contentBlockStart"]["start"]["toolUse"]["toolUseId"] == tool["toolUseId"]
        assert json.loads(events[2]["contentBlockDelta"]["delta"]["toolUse"]["input"]) == {"city": "Paris"}
        request.pop("toolConfig")
        request["messages"] += [first["output"]["message"], {"role": "user", "content": [{"toolResult": {
            "toolUseId": tool["toolUseId"], "content": [{"text": "sunny"}]}}]}]
        assert client.converse(**request)["output"]["message"]["content"] == [{"text": "sunny"}]
    print(f"{mode}: boto3 Converse, ConverseStream, tools/results and native throttling passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=("environment", "hardcoded"))
    options = parser.parse_args()
    if options.client:
        client_check(options.client)
        return
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="mockingbird-bedrock-smoke-") as folder:
        folder = Path(folder)
        fake = folder / "fake_cli.py"
        fake.write_text('''import json, sys
req = json.loads(sys.stdin.read().split("Request JSON:\\n", 1)[1])
if req.get("tool_choice", {}).get("type") == "tool":
    answer = {"tool_calls":[{"name":"lookup","input":{"city":"Paris"}}]}
elif req["messages"][-1]["content"][0].get("type") == "tool_result":
    answer = {"text":"sunny"}
else:
    answer = {"text":"hello"}
print(json.dumps(answer))
''')
        faults = folder / "faults.json"
        faults.write_text('[{"status":429,"error":"scripted throttle"}]')
        env = {k: v for k, v in os.environ.items() if not k.startswith("BRIDGE_")}
        env.update(BRIDGE_CACHE="off", XDG_STATE_HOME=str(folder / "state"))
        auto = shlex.join([sys.executable, str(fake)])
        for mode in ("environment", "hardcoded"):
            result = subprocess.run([sys.executable, str(root / "bridge.py"), "run", "--auto", auto,
                                     "--faults", str(faults), "--", sys.executable, str(Path(__file__).resolve()),
                                     "--client", mode], cwd=root, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode:
                sys.stderr.write(result.stderr)
                raise SystemExit(result.returncode)
            print(result.stdout.strip())


if __name__ == "__main__":
    main()
