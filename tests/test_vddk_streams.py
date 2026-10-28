#!/usr/bin/env python3
# tests/test_vddk_streams.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Run the datastore-direct stream tasks against real nbdkit and qemu-img.

The tasks are shell scripts inside YAML, so these tests execute them through
ansible-playbook instead of reading their text: a converted disk, a failed
read whose nbdkit message reaches the failure, and a stop-streams pass that
stops only this run's servers.
"""

import json
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "playbooks" / "tasks" / "migration" / "openstack"
TOOLS = ("ansible-playbook", "nbdkit", "qemu-img")

CONVERT_PLAY = """
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    # set_fact cannot override an extra var, so the job map starts here.
    openstack_convert_jobs: {{}}
  tasks:
    - ansible.builtin.include_tasks: {tasks}/convert-disk-vddk.yml
    - ansible.builtin.async_status:
        jid: "{{{{ openstack_convert_jobs['0'] }}}}"
      register: result
      until: result.finished | bool
      retries: 60
      delay: 1
      failed_when: false
    - ansible.builtin.copy:
        content: "{{{{ {{'rc': result.rc | default(1), 'stderr': result.stderr | default('')}} | to_json }}}}"
        dest: {out}
"""

STOP_PLAY = """
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - ansible.builtin.import_tasks: {tasks}/stop-streams-vddk.yml
"""

def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False

@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs " + ", ".join(TOOLS))
class StreamTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vddk-test-"))
        # Unix socket paths are short, so the socket directory sits in /tmp.
        self.sockets = Path(tempfile.mkdtemp(prefix=".vddk-test-", dir="/tmp"))
        self.run_dir = self.tmp / "run"
        (self.run_dir / "disks").mkdir(parents=True)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.sockets, ignore_errors=True)

    def playbook(self, text, **extra_vars):
        play = self.tmp / "play.yml"
        play.write_text(text)
        variables = {"openstack_vddk_socket_dir": str(self.sockets), **extra_vars}
        return subprocess.run(["ansible-playbook", str(play), "-e", json.dumps(variables)],
                              capture_output=True, text=True, check=False)

    def convert(self, **overrides):
        out = self.tmp / "result.json"
        variables = {
            "migration_run_dir": str(self.run_dir),
            "openstack_staging_dir": str(self.run_dir / "disks"),
            "openstack_convert_disk": {"index": 0, "path": str(self.tmp / "disk.raw")},
            "openstack_convert_args": [],
            "openstack_convert_compress": False,
            "openstack_convert_async_timeout": 120,
            "openstack_vddk_local": True,
            "openstack_vddk_backend_effective": "curl",
            "openstack_vddk_nbdkit_bin": "nbdkit",
            "openstack_vddk_libdir": "",
            "openstack_vddk_thumbprint": "",
            "openstack_vddk_transports": "nbdssl",
            "openstack_vddk_ca_bundle": "",
            "qemu_img_bin": "qemu-img",
            "vcenter_hostname": "127.0.0.1:1",
            "vcenter_username": "test",
            "vcenter_password": "test",
            "datacenter": "DC",
            "validate_certs": False,
            **overrides,
        }
        played = self.playbook(CONVERT_PLAY.format(tasks=TASKS, out=out), **variables)
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        return json.loads(out.read_text())

    def test_converts_a_disk(self):
        (self.tmp / "disk.raw").write_bytes(b"\xab" * 1024 * 1024)
        result = self.convert()
        self.assertEqual(result["rc"], 0, result["stderr"])
        qcow2 = self.run_dir / "disks" / "disk-0.qcow2"
        compared = subprocess.run(["qemu-img", "compare", "-f", "raw", "-F", "qcow2",
                                   str(self.tmp / "disk.raw"), str(qcow2)],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(compared.returncode, 0, compared.stdout + compared.stderr)
        self.assertFalse((self.run_dir / "disks" / "disk-0.qcow2.tmp").exists())
        self.assertFalse((self.sockets / "config-0").exists())

    def test_vddk_passes_two_session_config_to_nbdkit(self):
        # Capture the real VDDK argv, then serve a local raw file instead:
        # exercise configuration and conversion without a vCenter or VixDiskLib.
        raw = self.tmp / "disk.raw"
        raw.write_bytes(b"\xab" * 1024 * 1024)
        captured = self.tmp / "nbdkit-args.json"
        wrapper = self.tmp / "nbdkit-wrapper"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "config = Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('config=')))\n"
            f"Path({str(captured)!r}).write_text(json.dumps({{'args': args, 'config': config.read_text()}}))\n"
            "plugin = args.index('vddk')\n"
            f"os.execv({shutil.which('nbdkit')!r}, ['nbdkit', *args[:plugin], 'file', {str(raw)!r}])\n"
        )
        wrapper.chmod(0o700)

        result = self.convert(openstack_vddk_local=False,
                              openstack_vddk_backend_effective="vddk",
                              openstack_vddk_nbdkit_bin=str(wrapper))
        self.assertEqual(result["rc"], 0, result["stderr"])
        invocation = json.loads(captured.read_text())
        self.assertIn(f"config={self.sockets}/config-0", invocation["args"])
        self.assertIn("transports=nbdssl", invocation["args"])
        self.assertEqual(invocation["config"], 'OpenVDDK.nbd.sessions = "2"\n')
        self.assertEqual((self.sockets / "config-0").stat().st_mode & 0o777, 0o600)

    def test_converts_a_compressed_disk(self):
        (self.tmp / "disk.raw").write_bytes(b"\xab" * 1024 * 1024 + bytes(range(256)) * 4096)
        result = self.convert(openstack_convert_compress=True)
        self.assertEqual(result["rc"], 0, result["stderr"])
        qcow2 = self.run_dir / "disks" / "disk-0.qcow2"
        compared = subprocess.run(["qemu-img", "compare", "-f", "raw", "-F", "qcow2",
                                   str(self.tmp / "disk.raw"), str(qcow2)],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(compared.returncode, 0, compared.stdout + compared.stderr)
        info = json.loads(subprocess.run(["qemu-img", "info", "--output=json", str(qcow2)],
                                         capture_output=True, text=True, check=True).stdout)
        # Small clusters make each compressed read a separate slow request.
        self.assertEqual(info["cluster-size"], 2 * 1024 * 1024)

    def test_startup_failure_quotes_nbdkit(self):
        # The file plugin refuses a missing file before it serves anything.
        result = self.convert()
        self.assertNotEqual(result["rc"], 0)
        self.assertIn("nbdkit exited before serving", result["stderr"])
        self.assertIn("disk.raw", result["stderr"])

    def test_read_failure_quotes_nbdkit(self):
        # The curl plugin starts, then fails when qemu-img connects: the reason
        # is only in nbdkit's own log, which the failure must carry.
        result = self.convert(openstack_vddk_local=False)
        self.assertNotEqual(result["rc"], 0)
        # Check that nbdkit's own log is quoted, not libcurl's wording, which
        # changes between versions: the curl plugin names itself and the
        # unreachable vCenter address from the variables above.
        self.assertRegex(result["stderr"], r"nbdkit log \([^)]*\): \S")
        quoted = result["stderr"].split("nbdkit log (", 1)[1].split("): ", 1)[1]
        self.assertIn("curl", quoted.lower())
        self.assertIn("127.0.0.1", quoted)
        self.assertFalse((self.run_dir / "disks" / "disk-0.qcow2").exists())
        self.assertFalse((self.sockets / "config-0").exists())

    def start_nbdkit(self, socket, pidfile):
        process = subprocess.Popen(["nbdkit", "-f", "-r", "-U", str(socket), "--pidfile", str(pidfile),
                                    "memory", "1M"], stderr=subprocess.DEVNULL)
        self.processes.append(process)
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                return process
            time.sleep(0.1)
        self.fail(f"nbdkit did not write {pidfile}")

    def test_stops_only_this_runs_streams(self):
        ours = self.start_nbdkit(self.sockets / "disk-0.sock", self.sockets / "disk-0.pid")
        # An nbdkit serving another socket, and an unrelated process, behind
        # pidfiles of this run: a stale pidfile whose pid was reused.
        other = self.start_nbdkit(self.tmp / "other.sock", self.sockets / "disk-1.pid")
        unrelated = subprocess.Popen(["sleep", "60"])
        self.processes.append(unrelated)
        (self.sockets / "disk-2.pid").write_text(f"{unrelated.pid}\n")
        (self.sockets / "disk-3.pid").write_text("garbage\n")

        played = self.playbook(STOP_PLAY.format(tasks=TASKS))
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        ours.wait(timeout=5)
        self.assertFalse(alive(ours.pid))
        self.assertTrue(alive(other.pid))
        self.assertTrue(alive(unrelated.pid))
        self.assertIn("changed=1", played.stdout)

if __name__ == "__main__":
    unittest.main()
