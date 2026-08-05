"""Behavior tests for pomo/service.py: platform detection and service
file generation. Subprocess calls (launchctl/systemctl) are mocked;
pure functions are tested directly."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from _util import isolate

from pomo import service


class PlatformTests(unittest.TestCase):
    def test_platform_darwin(self):
        # When: running on macOS
        with patch.object(service.sys, "platform", "darwin"):
            # Then: _platform returns "macos"
            self.assertEqual(service._platform(), "macos")

    def test_platform_linux(self):
        # When: running on Linux
        with patch.object(service.sys, "platform", "linux"):
            # Then: _platform returns "linux"
            self.assertEqual(service._platform(), "linux")

    def test_platform_other_exits(self):
        # When: running on an unsupported OS
        with patch.object(service.sys, "platform", "win32"):
            # Then: _platform exits with error
            with self.assertRaises(SystemExit):
                service._platform()


class BinaryTests(unittest.TestCase):
    def test_binary_found(self):
        # When: pomo-agent is on PATH
        with patch.object(service.shutil, "which",
                          return_value="/usr/bin/pomo-agent"):
            path = service._binary(server=False)
            # Then: a truthy path is returned
            self.assertEqual(path, "/usr/bin/pomo-agent")

    def test_binary_not_found_exits(self):
        # When: binary is not on PATH
        with patch.object(service.shutil, "which", return_value=None):
            # Then: _binary exits with error message
            with self.assertRaises(SystemExit):
                service._binary(server=False)

    def test_binary_server(self):
        # When: requesting server binary
        with patch.object(service.shutil, "which", return_value="/usr/bin/pomo-server"):
            path = service._binary(server=True)
            # Then: returns the path and called which with correct name
            self.assertEqual(path, "/usr/bin/pomo-server")


class EnvTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.environ.pop, "POMO_SERVER_URL", None)
        self.addCleanup(os.environ.pop, "POMO_TOKEN", None)
        self.addCleanup(os.environ.pop, "POMO_DB_PATH", None)
        self.addCleanup(os.environ.pop, "OTHER_VAR", None)

    def test_pomo_env_captures_pomo_vars(self):
        # Given: POMO_* and non-POMO env vars are set
        os.environ["POMO_SERVER_URL"] = "http://example:8787"
        os.environ["POMO_TOKEN"] = "secret"
        os.environ["OTHER_VAR"] = "ignored"
        # When: _pomo_env is called
        env = service._pomo_env()
        # Then: only POMO_* vars are captured
        self.assertIn("POMO_SERVER_URL", env)
        self.assertIn("POMO_TOKEN", env)
        self.assertNotIn("OTHER_VAR", env)

    def test_pomo_env_empty_when_no_vars(self):
        # Given: no POMO_* vars set
        # When: _pomo_env is called
        env = service._pomo_env()
        # Then: empty dict
        self.assertEqual(env, {})


class PlistContentTests(unittest.TestCase):
    def test_plist_content_contains_label(self):
        # Given: binary is on PATH
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            content = service._macos_plist_content(server=False)
            # Then: plist contains the expected label
            self.assertIn("ai.pomo.agent", content)

    def test_plist_content_contains_binary(self):
        # Given: binary is on PATH
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            content = service._macos_plist_content(server=False)
            # Then: plist contains the binary path
            self.assertIn("/usr/bin/pomo-agent", content)

    def test_plist_content_contains_env(self):
        # Given: POMO_SERVER_URL is set
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            with patch.object(service, "_pomo_env",
                              return_value={"POMO_SERVER_URL": "http://s:8787"}):
                content = service._macos_plist_content(server=False)
                # Then: plist contains the env var
                self.assertIn("POMO_SERVER_URL", content)
                self.assertIn("http://s:8787", content)

    def test_plist_content_server_label(self):
        # Given: server binary
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-server"):
            content = service._macos_plist_content(server=True)
            # Then: plist uses the server label
            self.assertIn("ai.pomo.server", content)


class LinuxServiceContentTests(unittest.TestCase):
    def test_service_content_contains_binary(self):
        # Given: binary is on PATH
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            content = service._linux_service_content(server=False)
            # Then: service file contains the binary path
            self.assertIn("/usr/bin/pomo-agent", content)

    def test_service_content_contains_env(self):
        # Given: POMO_TOKEN is set
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            with patch.object(service, "_pomo_env",
                              return_value={"POMO_TOKEN": "secret"}):
                content = service._linux_service_content(server=False)
                # Then: service file contains the env var
                self.assertIn("POMO_TOKEN", content)

    def test_service_content_server(self):
        # Given: server binary
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-server"):
            content = service._linux_service_content(server=True)
            # Then: service file uses server name
            self.assertIn("pomo-server", content)
            self.assertIn("sync server", content)


class PathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = isolate(self)

    def test_macos_plist_path(self):
        # When: on macOS, requesting the agent plist path
        with patch.object(service.Path, "home", return_value=self.tmp):
            path = service._macos_plist(server=False)
            # Then: path is under ~/Library/LaunchAgents
            expected = self.tmp / "Library" / "LaunchAgents" / "ai.pomo.agent.plist"
            self.assertEqual(path, expected)

    def test_linux_service_path(self):
        # When: on Linux, requesting the agent service path
        with patch.object(service.Path, "home", return_value=self.tmp):
            path = service._linux_service_path(server=False)
            # Then: path is under ~/.config/systemd/user
            expected = self.tmp / ".config" / "systemd" / "user" / "pomo-agent.service"
            self.assertEqual(path, expected)


if __name__ == "__main__":
    unittest.main()
