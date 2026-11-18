"""Exercise the pinned image's two startup phases without a Docker daemon."""
import subprocess
import unittest
from unittest.mock import patch

from scripts import verify_public_database as rehearsal


class DatabaseReadinessTests(unittest.TestCase):
    def test_only_final_socket_can_release_migrations(self):
        # The image entrypoint overrides the temporary server's port with 5432.
        bootstrap_port = "5432"
        phase = "bootstrap"
        server_port = None
        probes = []

        def docker(*args, **kwargs):
            nonlocal server_port
            output = ""
            if args[0] == "create":
                server_port = args[args.index("-p") + 1] if "-p" in args else bootstrap_port
                output = "container-id"
            elif args[0] == "inspect":
                output = "running"
            elif args[0] == "exec" and "psql" in args:
                port = args[args.index("-p") + 1] if "-p" in args else bootstrap_port
                probes.append((phase, port))
                listening = bootstrap_port if phase == "bootstrap" else server_port
                if port != listening:
                    raise subprocess.CalledProcessError(2, args)
                output = "postgres"
            elif args[0] == "exec" and "/proc/1/comm" in args:
                output = "bash" if phase == "bootstrap" else ".postgres-wrapp"
            return subprocess.CompletedProcess(args, 0, stdout=output)

        def finish_bootstrap(_seconds):
            nonlocal phase
            phase = "final"

        with (patch.object(rehearsal, "docker", side_effect=docker),
              patch.object(rehearsal.time, "sleep", side_effect=finish_bootstrap),
              patch.object(rehearsal.time, "monotonic", side_effect=[0, 1, 2, 181]),
              patch.object(rehearsal, "verify") as verify):
            rehearsal.main()

        self.assertNotEqual(server_port, bootstrap_port)
        self.assertEqual(probes, [("bootstrap", server_port), ("final", server_port)])
        verify.assert_called_once_with("container-id")

    def test_unavailable_final_server_times_out_without_applying_sql_files(self):
        with (patch.object(rehearsal, "docker", return_value=subprocess.CompletedProcess([], 0, stdout="running")),
              patch.object(rehearsal, "sql", side_effect=subprocess.CalledProcessError(2, "psql")) as probe,
              patch.object(rehearsal.time, "monotonic", side_effect=[0, 1, 181]),
              patch.object(rehearsal.time, "sleep"),
              self.assertRaisesRegex(RuntimeError, "Timed out")):
            rehearsal.await_database("container-id")
        probe.assert_called_once_with("container-id", "select current_user;")
