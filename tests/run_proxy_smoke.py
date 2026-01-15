from __future__ import annotations

import json

from tests.helpers import free_port, post_json, running_server, wait_for_value


def main() -> int:
    upstream_port = free_port()
    proxy_port = free_port()
    print(f"upstream={upstream_port} proxy={proxy_port}")

    with running_server(port=upstream_port), running_server(
        port=proxy_port,
        proxy=True,
        upstream_port=upstream_port,
    ):
        status, body = post_json(
            f"http://127.0.0.1:{upstream_port}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "c",
                        "mime": "text/plain",
                        "encoding": "utf-8",
                        "value": "smoke-upstream",
                    }
                ],
                "meta": {"app": "proxy-smoke"},
            },
        )
        assert status == 200, body
        cached = wait_for_value(proxy_port, "c", "smoke-upstream")
        print("upstream -> proxy ok", json.dumps(cached))

        status, body = post_json(
            f"http://127.0.0.1:{proxy_port}/v1/clip.put",
            {
                "items": [
                    {
                        "topic": "p",
                        "mime": "text/plain",
                        "encoding": "utf-8",
                        "value": "smoke-proxy",
                    }
                ],
                "meta": {"app": "proxy-smoke"},
            },
        )
        assert status == 200, body
        upstream = wait_for_value(upstream_port, "p", "smoke-proxy")
        print("proxy -> upstream ok", json.dumps(upstream))

    print("proxy smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
