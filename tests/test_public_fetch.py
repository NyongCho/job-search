import socket
import time
import unittest
from unittest.mock import patch

from job_search import public_fetch


class PublicFetchTests(unittest.TestCase):
    def test_redirect_is_revalidated_before_opening_another_socket(self):
        for address in ("127.0.0.1", "169.254.169.254", "::ffff:127.0.0.1"):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            records = [[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
                       [(family, socket.SOCK_STREAM, 6, "", (address, 443))]]
            with self.subTest(address=address), patch.object(socket, "getaddrinfo", side_effect=records), \
                    patch.object(public_fetch, "_request", return_value=(302, {"location": "https://other.example/"}, b"")) as request:
                with self.assertRaises(OSError):
                    public_fetch.fetch_public("https://public.example/", deadline=time.monotonic() + 1, limit=1024)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.args[1][0][-1][0], "93.184.216.34")

    def test_total_timeout_applies_even_while_resolving_dns(self):
        def stalled(*args, **kwargs):
            time.sleep(0.2)
            return []
        start = time.monotonic()
        with patch.object(socket, "getaddrinfo", side_effect=stalled):
            with self.assertRaises(TimeoutError):
                public_fetch.fetch_public("https://public.example/", deadline=start + 0.025, limit=1024)
        self.assertLess(time.monotonic() - start, 0.15)
