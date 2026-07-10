import json
import os
import socket
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from crosscopy import discovery


class DiscoveryReliabilityTests(unittest.TestCase):
    def make_discovery(self):
        instance = discovery.Discovery(7373)
        self.addCleanup(instance.stop)
        return instance

    def test_directed_broadcast_addresses_cover_each_ipv4_subnet(self):
        adapter = SimpleNamespace(ips=[
            SimpleNamespace(ip="192.168.20.14", network_prefix=24),
            SimpleNamespace(ip="10.40.5.8", network_prefix=16),
            SimpleNamespace(ip="169.254.3.2", network_prefix=16),
            SimpleNamespace(ip=("fe80::1", 0, 0), network_prefix=64),
        ])
        fake_ifaddr = SimpleNamespace(get_adapters=lambda: [adapter])
        with mock.patch.dict(sys.modules, {"ifaddr": fake_ifaddr}):
            addresses = discovery.get_broadcast_addresses()
        self.assertIn("255.255.255.255", addresses)
        self.assertIn("192.168.20.255", addresses)
        self.assertIn("10.40.255.255", addresses)
        self.assertNotIn("169.254.255.255", addresses)

    def test_beacon_records_source_address_and_wakes_http_hello(self):
        instance = self.make_discovery()
        payload = json.dumps({
            "magic": discovery.BEACON_MAGIC,
            "id": "peer-id",
            "name": "Windows PC",
            "platform": "win32",
            "version": "0.5.0",
            "port": 7373,
            "clip": "clip-id",
        }).encode("utf-8")
        with mock.patch.object(discovery.config, "get_device_id",
                               return_value="local-id"), \
                mock.patch.object(instance, "record_peer",
                                  return_value=True) as record, \
                mock.patch.object(instance._hello_wake, "set") as wake:
            instance._handle_beacon(payload, ("192.168.20.50", 51000))
        record.assert_called_once_with(
            "peer-id", name="Windows PC", host="192.168.20.50", port=7373,
            platform="win32", version="0.5.0", source="broadcast",
            addresses=["192.168.20.50"], clip="clip-id")
        wake.assert_called_once_with()

    def test_invalid_or_self_beacons_are_ignored(self):
        instance = self.make_discovery()
        self_payload = json.dumps({
            "magic": discovery.BEACON_MAGIC,
            "id": "local-id",
            "port": 7373,
        }).encode("utf-8")
        with mock.patch.object(discovery.config, "get_device_id",
                               return_value="local-id"), \
                mock.patch.object(instance, "record_peer") as record:
            instance._handle_beacon(b"not-json", ("192.168.1.2", 1))
            instance._handle_beacon(self_payload, ("192.168.1.2", 1))
        record.assert_not_called()

    def test_beacon_sends_to_limited_and_directed_broadcasts(self):
        instance = self.make_discovery()
        sock = mock.Mock()
        with mock.patch.object(discovery, "get_broadcast_addresses",
                               return_value=["192.168.1.255",
                                             "255.255.255.255"]), \
                mock.patch.object(discovery, "beacon_port", return_value=7374), \
                mock.patch.object(instance, "_beacon_payload",
                                  return_value=b"payload"):
            instance._send_beacon(sock)
        self.assertEqual([
            mock.call(b"payload", ("192.168.1.255", 7374)),
            mock.call(b"payload", ("255.255.255.255", 7374)),
        ], sock.sendto.call_args_list)

    def test_windows_stable_network_rescans_and_reannounces(self):
        instance = self.make_discovery()
        zc = mock.Mock()
        info = object()
        instance._zeroconf = zc
        instance._service_info = info
        instance._mdns_ips = ("192.168.1.10",)
        instance._last_mdns_announce = 0
        with mock.patch.object(discovery, "_HAVE_ZEROCONF", True), \
                mock.patch.object(discovery, "get_local_ips",
                                  return_value=["192.168.1.10"]):
            instance._refresh_windows_interfaces()
        zc.update_interfaces.assert_called_once_with(["192.168.1.10"])
        zc.update_service.assert_called_once_with(info)

    def test_windows_mdns_binds_explicit_ipv4_interfaces(self):
        addresses = ["192.168.1.10", "10.20.30.40"]
        with mock.patch.object(discovery.sys, "platform", "win32"):
            self.assertEqual(addresses, discovery.mdns_interfaces(addresses))
        with mock.patch.object(discovery.sys, "platform", "darwin"):
            self.assertEqual(discovery.InterfaceChoice.All,
                             discovery.mdns_interfaces(addresses))

    def test_windows_address_change_rebuilds_mdns_and_wakes_beacon(self):
        instance = self.make_discovery()
        instance._zeroconf = mock.Mock()
        instance._mdns_ips = ("169.254.10.2",)
        with mock.patch.object(discovery, "_HAVE_ZEROCONF", True), \
                mock.patch.object(discovery, "get_local_ips",
                                  return_value=["192.168.1.10"]), \
                mock.patch.object(instance, "_shutdown_zeroconf") as shutdown, \
                mock.patch.object(instance, "_start_zeroconf") as start, \
                mock.patch.object(instance._beacon_wake, "set") as wake:
            instance._refresh_windows_interfaces()
        shutdown.assert_called_once_with()
        start.assert_called_once_with()
        wake.assert_called_once_with()

    def test_discovery_port_override_is_validated(self):
        with mock.patch.dict(os.environ,
                             {"CROSSCOPY_DISCOVERY_PORT": "18000"}):
            self.assertEqual(18000, discovery.beacon_port())
        with mock.patch.dict(os.environ,
                             {"CROSSCOPY_DISCOVERY_PORT": "invalid"}):
            self.assertEqual(discovery.BEACON_PORT_DEFAULT,
                             discovery.beacon_port())

    @unittest.skipUnless(sys.platform == "win32", "native Windows socket check")
    def test_windows_udp_listener_discovers_a_real_datagram(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        instance = self.make_discovery()
        payload = json.dumps({
            "magic": discovery.BEACON_MAGIC,
            "id": "native-windows-peer",
            "name": "Windows peer",
            "platform": "win32",
            "version": "0.5.1",
            "port": 7373,
        }).encode("utf-8")
        with mock.patch.dict(os.environ,
                             {"CROSSCOPY_DISCOVERY_PORT": str(port)}), \
                mock.patch.object(discovery.config, "get_device_id",
                                  return_value="local-id"), \
                mock.patch.object(discovery.config, "get_device_name",
                                  return_value="Local Windows"), \
                mock.patch.object(discovery.config, "platform_name",
                                  return_value="win32"):
            instance._start_beacon()
            deadline = time.time() + 2.0
            while instance._beacon_socket is None and time.time() < deadline:
                time.sleep(0.02)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(payload, ("127.0.0.1", port))
                while ("native-windows-peer" not in instance._peers
                       and time.time() < deadline):
                    time.sleep(0.02)
            finally:
                sender.close()
        self.assertIn("native-windows-peer", instance._peers)


if __name__ == "__main__":
    unittest.main()
