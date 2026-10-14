"""Own one CARLA process for one response, keeping other services untouched."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time


class OwnedCarlaSession:
    def __init__(self, platform, directory, graphics_adapter):
        self.platform = platform
        self.directory = Path(directory)
        self.adapter = graphics_adapter
        self.process = None
        self.receipt = {}

    def save(self):
        (self.directory / "session.json").write_text(json.dumps(self.receipt, indent=2) + "\n")

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        endpoint = self.platform["carla_endpoint"]
        port = endpoint["port"]
        for candidate in (port, port + 1, port + 2):
            with socket.socket() as probe:
                probe.bind((endpoint["host"], candidate))
        binary = Path(self.platform["server_binary"]["path"])
        server = binary.parents[3]
        argv = [str(binary), "-RenderOffScreen", "-nosound", "-vulkan",
                "-graphicsadapter={}".format(self.adapter), "-quality-level=Low", "-carla-port={}".format(port)]
        environment = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH") if name in os.environ}
        with (self.directory / "server.log").open("xb") as stream:
            self.process = subprocess.Popen(argv, cwd=server, stdin=subprocess.DEVNULL,
                                            stdout=stream, stderr=subprocess.STDOUT,
                                            env=environment, start_new_session=True)
        self.receipt = {"development_only": True, "scope": "one-process-per-response",
                        "pid": self.process.pid, "argv": argv, "process_stopped": False,
                        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        probe_code = "import carla,sys; c=carla.Client('127.0.0.1',int(sys.argv[1])); c.set_timeout(1); print(c.get_server_version())"
        deadline = time.monotonic() + 120
        try:
            self.save()
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("owned CARLA exited during startup; see its server log")
                try:
                    result = subprocess.run([self.platform["worker"]["interpreter"], "-I", "-c", probe_code, str(port)],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4,
                                            env=environment, check=False)
                    if result.returncode == 0:
                        self.receipt["ready_server_version"] = result.stdout.decode().strip()
                        self.save()
                        return self
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(1)
            raise RuntimeError("owned CARLA did not become ready within 120 seconds")
        except BaseException:
            self.stop()
            raise

    def verify_identity(self):
        command = Path("/proc") / str(self.process.pid) / "cmdline"
        actual = command.read_bytes().split(b"\0")
        if not actual[0]:
            status = command.with_name("stat").read_text().rsplit(")", 1)[1].split()[0]
            if self.process.poll() is not None or status == "Z":
                self.process.wait(timeout=1)
                return False
        if actual[0].decode() != self.receipt["argv"][0] or self.receipt["argv"][-1].encode() not in actual:
            raise RuntimeError("owned CARLA identity changed; refusing to stop this process")
        return True

    def stop(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                if self.verify_identity():
                    os.kill(self.process.pid, signal.SIGTERM)
                    try:
                        self.process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        # Only this disposable owned server is stopped. Runtime
                        # evidence is persisted before context cleanup begins.
                        if self.verify_identity():
                            self.receipt["forced_stop"] = True
                            os.kill(self.process.pid, signal.SIGKILL)
                            self.process.wait(timeout=5)
        except (FileNotFoundError, ProcessLookupError) as exc:
            if self.process.poll() is None:
                self.receipt["cleanup_error"] = "{}: {}".format(type(exc).__name__, exc)
                raise
        except BaseException as exc:
            self.receipt["cleanup_error"] = "{}: {}".format(type(exc).__name__, exc)
            raise
        finally:
            stopped = self.process.poll() is not None
            self.receipt.update(process_stopped=stopped, exit_code=self.process.returncode,
                                stopped_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if stopped else None)
            self.save()

    def __exit__(self, kind, value, traceback):
        self.stop()
