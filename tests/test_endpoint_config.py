from __future__ import annotations

import unittest

from rclipboard.endpoints import parse_endpoint


class EndpointConfigTests(unittest.TestCase):
    def test_parse_host_port_endpoint(self):
        endpoint = parse_endpoint("127.0.0.1:8989")
        self.assertEqual(endpoint.scheme, "http")
        self.assertEqual(endpoint.host, "127.0.0.1")
        self.assertEqual(endpoint.port, 8989)

    def test_parse_uds_endpoint(self):
        endpoint = parse_endpoint("uds:///tmp/rclipboard.sock")
        self.assertEqual(endpoint.scheme, "uds")
        self.assertEqual(endpoint.path, "/tmp/rclipboard.sock")
