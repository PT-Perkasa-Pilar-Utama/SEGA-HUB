import subprocess
import unittest
from updater.server import UpdaterConfig, UpdaterService


class UpdaterServiceTests(unittest.TestCase):
    def test_get_status_detects_newer_semver(self):
        cfg = UpdaterConfig()
        svc = UpdaterService(
            cfg,
            remote_fetcher=lambda image: {
                "image": image,
                "latest_version": "v0.2.0",
                "latest_digest": "sha256:abc",
            },
        )
        status = svc.get_status("sega", current_version="v0.1.0")
        self.assertTrue(status["update_available"])
        self.assertEqual(status["latest_version"], "v0.2.0")

    def test_get_status_up_to_date(self):
        cfg = UpdaterConfig()
        svc = UpdaterService(
            cfg,
            remote_fetcher=lambda image: {
                "image": image,
                "latest_version": "v0.1.0",
                "latest_digest": "sha256:abc",
            },
        )
        status = svc.get_status("bas-gateway", current_version="v0.1.0")
        self.assertFalse(status["update_available"])

    def test_rejects_disallowed_service(self):
        cfg = UpdaterConfig(allowed_services=("sega",))
        svc = UpdaterService(cfg)
        with self.assertRaises(ValueError):
            svc.get_status("postgres")
        with self.assertRaises(ValueError):
            svc.trigger_update("postgres")

    def test_trigger_update_runs_pull_and_up(self):
        commands = []

        def fake_runner(cmd):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        cfg = UpdaterConfig(compose_file="/compose/docker-compose.yml")
        svc = UpdaterService(cfg, runner=fake_runner)
        res = svc.trigger_update("sega", tag="latest", sync_recreate=True)
        self.assertEqual(res["status"], "redeploying")
        self.assertEqual(
            commands,
            [
                ["docker", "compose", "-f", "/compose/docker-compose.yml", "pull", "sega"],
                ["docker", "compose", "-f", "/compose/docker-compose.yml", "up", "-d", "--no-deps", "sega"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
