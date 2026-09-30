#!/usr/bin/env python3
"""
oci_grab.py -- retry an OCI free-tier instance launch in the background until
capacity appears.

  pip install oci                       # once
  oci setup config                      # once, if you have not already

  ./oci_grab.py init                    # write a config template
  ./oci_grab.py discover                # fill in the OCIDs you need
  ./oci_grab.py run                     # foreground (watch it work)
  ./oci_grab.py start                   # detach into the background
  ./oci_grab.py status                  # is it alive, how many tries
  ./oci_grab.py logs                    # tail the log
  ./oci_grab.py stop                    # shut it down
  ./oci_grab.py systemd                 # print a systemd unit for boot-time start

Classifies OCI error codes rather than grepping message strings, so it keeps
retrying "out of capacity" but gives up immediately on a bad OCID or a blown
quota. Checks for an already-created instance before every attempt, so a lost
response can never leave you with two VMs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("OCI_GRAB_CONFIG", HERE / "oci_grab.json"))
STATE_DIR = Path(os.environ.get("OCI_GRAB_STATE", HERE / ".oci_grab"))
PID_FILE = STATE_DIR / "daemon.pid"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "grab.log"

try:
    import oci          # brings oci.core, oci.identity, oci.pagination, oci.retry, oci.auth
except ImportError:     # keep the module importable (and testable) without the SDK
    oci = None

SDK_HINT = "the OCI SDK is missing -- run: pip install oci"

log = logging.getLogger("oci_grab")


# --------------------------------------------------------------------- config


@dataclass
class Config:
    compartment_id: str = ""
    subnet_id: str = ""
    image_id: str = ""
    ssh_public_key_file: str = "~/.ssh/id_rsa.pub"

    shape: str = "VM.Standard.A1.Flex"
    ocpus: int = 1                 # 1 OCPU / 6 GB lands far more often than 4 / 24
    memory_gb: int = 6
    boot_volume_gb: int = 50
    display_name: str = "free-arm"
    assign_public_ip: bool = True

    # Availability domains to rotate. Empty = discover them all automatically.
    availability_domains: list[str] = field(default_factory=list)

    min_sleep_sec: int = 45        # below ~30s OCI starts answering 429
    max_sleep_sec: int = 75
    max_attempts: int = 0          # 0 = unlimited
    max_hours: float = 0           # 0 = no time limit

    oci_profile: str = "DEFAULT"
    oci_config_file: str = "~/.oci/config"
    use_instance_principal: bool = False

    # Optional success notification. Both are fired if set.
    notify_webhook: str = ""       # POSTed the message body; works with ntfy.sh
    notify_command: str = ""       # shell command; {msg} is substituted

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            die(f"no config at {path} -- run: {sys.argv[0]} init")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            die(f"{path} is not valid JSON: {e}")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            die(f"unknown config key(s): {', '.join(sorted(unknown))}")
        return cls(**raw)

    def ssh_key(self) -> str:
        p = Path(self.ssh_public_key_file).expanduser()
        if not p.is_file():
            die(f"ssh public key not found: {p}")
        text = p.read_text().strip()
        if not text.startswith(("ssh-", "ecdsa-")):
            die(f"{p} is not an SSH *public* key -- you want the .pub file")
        return text

    def validate(self) -> None:
        for name in ("compartment_id", "subnet_id", "image_id"):
            if not getattr(self, name):
                die(f"config.{name} is empty -- run: {sys.argv[0]} discover")
            if not getattr(self, name).startswith("ocid1."):
                die(f"config.{name} does not look like an OCID")
        if self.min_sleep_sec > self.max_sleep_sec:
            die("min_sleep_sec is greater than max_sleep_sec")
        if self.min_sleep_sec < 20:
            die("min_sleep_sec below 20 will get you rate-limited; use 45 or more")
        self.ssh_key()


# ---------------------------------------------------------------------- utils


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def setup_logging(to_console: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def write_state(**kw: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(kw, indent=2, default=str))
    tmp.replace(STATE_FILE)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid() -> Optional[int]:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


# ------------------------------------------------------- error classification

RETRY_CAPACITY = "capacity"
RETRY_RATELIMIT = "ratelimit"
RETRY_UNKNOWN = "unknown"
FATAL = "fatal"

# Codes that mean "your request is wrong" -- retrying cannot help.
FATAL_CODES = {
    "LimitExceeded", "QuotaExceeded", "NotAuthorizedOrNotFound",
    "NotAuthenticated", "InvalidParameter", "CannotParseRequest",
    "NotAuthorizedOrResourceAlreadyExists", "SignUpRequired",
}


def classify(exc: Exception) -> tuple[str, str]:
    """Map an SDK exception onto a retry decision. Pure -- unit-testable."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", "") or ""
    message = (getattr(exc, "message", "") or str(exc)).strip()

    if status is None:                       # connection reset, DNS, timeout
        return RETRY_UNKNOWN, f"transport error: {type(exc).__name__}: {message}"

    if status == 429 or code == "TooManyRequests":
        return RETRY_RATELIMIT, "rate limited by OCI"

    # The one we are actually here for. OCI reports it as a 500 InternalError
    # whose message is "Out of host capacity."
    if "out of host capacity" in message.lower() or "out of capacity" in message.lower():
        return RETRY_CAPACITY, "out of host capacity"

    if code in FATAL_CODES:
        return FATAL, f"{code} ({status}): {message}"

    if status in (400, 401, 403, 404):
        return FATAL, f"{code or status}: {message}"

    if status >= 500:
        return RETRY_UNKNOWN, f"server error {status} {code}: {message}"

    return RETRY_UNKNOWN, f"{status} {code}: {message}"


def load_oci_config(cfg: "Config") -> dict:
    """Read ~/.oci/config with errors a human can act on."""
    path = Path(cfg.oci_config_file).expanduser()
    try:
        conf = oci.config.from_file(str(path), cfg.oci_profile)
        oci.config.validate_config(conf)
        return conf
    except oci.exceptions.ConfigFileNotFound:
        die(f"no OCI credentials at {path}.\n"
            "  Create an API key: cloud.oracle.com -> profile icon -> My profile\n"
            "  -> API keys -> Add API key -> download the private key, then copy the\n"
            "  config snippet it shows you into that file. Or run: oci setup config")
    except oci.exceptions.ProfileNotFound:
        die(f"profile [{cfg.oci_profile}] not found in {path}")
    except oci.exceptions.InvalidKeyFilePath as e:
        die(f"the key_file path in {path} does not exist: {e}\n"
            "  key_file must be the ABSOLUTE path to the .pem private key you\n"
            "  downloaded when you created the API key.")
    except oci.exceptions.InvalidConfig as e:
        die(f"{path} is incomplete or malformed: {e}\n"
            "  It needs user, fingerprint, tenancy, region and key_file.")
    except (OSError, ValueError) as e:
        die(f"could not read the private key named by key_file in {path}: {e}")


# -------------------------------------------------------------------- grabber


class Grabber:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.attempts = 0
        self.capacity_misses = 0
        self.backoff = 1
        self.started = time.time()
        self.last_reason = ""
        if oci is None:
            die(SDK_HINT)

    # -- auth ---------------------------------------------------------------

    def _clients(self):
        if self.cfg.use_instance_principal:
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            base = {"config": {}, "signer": signer}
        else:
            base = {"config": load_oci_config(self.cfg)}
        compute = oci.core.ComputeClient(**base)
        identity = oci.identity.IdentityClient(**base)
        network = oci.core.VirtualNetworkClient(**base)
        # We do our own pacing; do not let the SDK silently retry 429s too.
        for c in (compute, identity, network):
            c.base_client.retry_strategy = oci.retry.NoneRetryStrategy()
        return compute, identity, network

    # -- helpers ------------------------------------------------------------

    def _ads(self, identity) -> list[str]:
        if self.cfg.availability_domains:
            return list(self.cfg.availability_domains)
        ads = identity.list_availability_domains(
            compartment_id=self.cfg.compartment_id
        ).data
        if not ads:
            die("no availability domains returned -- check compartment_id")
        return [a.name for a in ads]

    def _already_have_one(self, compute):
        """Guard against duplicates: a launch may succeed while the response is
        lost, and blindly retrying would then create a second billable VM."""
        try:
            instances = oci.pagination.list_call_get_all_results(
                compute.list_instances, compartment_id=self.cfg.compartment_id
            ).data
        except Exception as e:                       # non-fatal: just skip the check
            log.warning("could not list instances for the duplicate check: %s", e)
            return None
        for inst in instances:
            if inst.display_name == self.cfg.display_name and inst.lifecycle_state in (
                "PROVISIONING", "STARTING", "RUNNING"
            ):
                return inst
        return None

    def _launch_details(self, ad: str):
        d = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=self.cfg.compartment_id,
            shape=self.cfg.shape,
            display_name=self.cfg.display_name,
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=self.cfg.subnet_id,
                assign_public_ip=self.cfg.assign_public_ip,
            ),
            metadata={"ssh_authorized_keys": self.cfg.ssh_key()},
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=self.cfg.image_id,
                boot_volume_size_in_gbs=self.cfg.boot_volume_gb,
            ),
        )
        if self.cfg.shape.endswith(".Flex"):        # fixed shapes reject shape_config
            d.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=self.cfg.ocpus, memory_in_gbs=self.cfg.memory_gb
            )
        return d

    def _public_ip(self, network, compute, instance_id: str) -> str:
        for _ in range(10):
            try:
                vnics = compute.list_vnic_attachments(
                    compartment_id=self.cfg.compartment_id, instance_id=instance_id
                ).data
                if vnics and vnics[0].vnic_id:
                    ip = network.get_vnic(vnics[0].vnic_id).data.public_ip
                    if ip:
                        return ip
            except Exception:
                pass
            if self.stop_event.wait(6):
                break
        return "(not assigned yet)"

    def _notify(self, msg: str) -> None:
        if self.cfg.notify_webhook:
            try:
                import urllib.request
                req = urllib.request.Request(
                    self.cfg.notify_webhook, data=msg.encode(), method="POST"
                )
                urllib.request.urlopen(req, timeout=15).read()
                log.info("webhook notified")
            except Exception as e:
                log.warning("webhook failed: %s", e)
        if self.cfg.notify_command:
            try:
                import subprocess
                subprocess.run(
                    self.cfg.notify_command.replace("{msg}", msg), shell=True, timeout=30
                )
            except Exception as e:
                log.warning("notify_command failed: %s", e)

    def _save(self, status: str) -> None:
        write_state(
            status=status,
            pid=os.getpid(),
            attempts=self.attempts,
            capacity_misses=self.capacity_misses,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            elapsed_min=round((time.time() - self.started) / 60, 1),
            last_reason=self.last_reason,
            shape=f"{self.cfg.shape} {self.cfg.ocpus}ocpu/{self.cfg.memory_gb}GB",
        )

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        self.cfg.validate()
        compute, identity, network = self._clients()
        ads = self._ads(identity)

        existing = self._already_have_one(compute)
        if existing:
            log.info("an instance named %r is already %s (%s) -- nothing to do",
                     self.cfg.display_name, existing.lifecycle_state, existing.id)
            self._save("already-exists")
            return 0

        log.info("shape=%s %socpu/%sGB  name=%s", self.cfg.shape, self.cfg.ocpus,
                 self.cfg.memory_gb, self.cfg.display_name)
        log.info("rotating ADs: %s", ", ".join(ads))
        log.info("polling every %s-%ss  (pid %s)", self.cfg.min_sleep_sec,
                 self.cfg.max_sleep_sec, os.getpid())

        i = 0
        while not self.stop_event.is_set():
            if self.cfg.max_attempts and self.attempts >= self.cfg.max_attempts:
                log.info("reached max_attempts=%s -- stopping", self.cfg.max_attempts)
                self._save("gave-up"); return 2
            if self.cfg.max_hours and (time.time() - self.started) > self.cfg.max_hours * 3600:
                log.info("reached max_hours=%s -- stopping", self.cfg.max_hours)
                self._save("gave-up"); return 2

            ad = ads[i % len(ads)]
            i += 1
            self.attempts += 1

            try:
                resp = compute.launch_instance(
                    self._launch_details(ad),
                    # makes this one attempt idempotent if the SDK's own transport
                    # ends up replaying it
                    opc_retry_token=uuid.uuid4().hex,
                )
                inst = resp.data
                msg = (f"OCI instance created: {inst.display_name} in {ad} "
                       f"after {self.attempts} attempts")
                log.info("SUCCESS -- %s", msg)
                log.info("id=%s state=%s", inst.id, inst.lifecycle_state)
                ip = self._public_ip(network, compute, inst.id)
                log.info("public ip: %s", ip)
                self.last_reason = "created"
                self._save("success")
                self._notify(f"{msg}\nssh ubuntu@{ip}")
                return 0

            except Exception as exc:
                outcome, reason = classify(exc)
                self.last_reason = reason

                if outcome == FATAL:
                    log.error("FATAL -- %s", reason)
                    log.error("retrying cannot fix this; fix the config and start again")
                    self._save("fatal")
                    return 3

                if outcome == RETRY_RATELIMIT:
                    self.backoff = min(self.backoff * 2, 8)
                    log.warning("%s -- backing off %sx", reason, self.backoff)
                elif outcome == RETRY_CAPACITY:
                    self.capacity_misses += 1
                    self.backoff = 1
                    log.info("attempt %s in %s: out of capacity (miss %s) -- normal",
                             self.attempts, ad, self.capacity_misses)
                else:
                    self.backoff = 1
                    log.warning("attempt %s in %s: %s", self.attempts, ad, reason)
                    # An unknown failure might have created the VM anyway.
                    got = self._already_have_one(compute)
                    if got:
                        log.info("SUCCESS -- found %r %s after an ambiguous error",
                                 got.display_name, got.lifecycle_state)
                        self._save("success")
                        self._notify(f"OCI instance created: {got.display_name}")
                        return 0

            self._save("running")
            nap = random.uniform(self.cfg.min_sleep_sec, self.cfg.max_sleep_sec) * self.backoff
            log.info("sleeping %.0fs", nap)
            self.stop_event.wait(nap)

        log.info("stopped by signal after %s attempts", self.attempts)
        self._save("stopped")
        return 0


# ------------------------------------------------------------------ commands


TEMPLATE_NOTE = """\
Edit this file, then run `oci_grab.py discover` to get the three OCIDs.
Keep ocpus/memory_gb at 1/6 for the best chance; you can launch four such
instances and still stay inside the Always Free 4 OCPU / 24 GB allowance.
"""


def cmd_init(_args) -> int:
    if CONFIG_PATH.exists():
        die(f"{CONFIG_PATH} already exists -- delete it first if you want a fresh one")
    CONFIG_PATH.write_text(json.dumps(asdict(Config()), indent=2) + "\n")
    print(f"wrote {CONFIG_PATH}\n\n{TEMPLATE_NOTE}")
    return 0


def cmd_discover(_args) -> int:
    if oci is None:
        die(SDK_HINT)

    cfg = Config.load(CONFIG_PATH) if CONFIG_PATH.exists() else Config()
    conf = load_oci_config(cfg)
    tenancy = conf["tenancy"]
    identity = oci.identity.IdentityClient(conf)
    compute = oci.core.ComputeClient(conf)
    network = oci.core.VirtualNetworkClient(conf)

    print(f'\n"compartment_id": "{tenancy}"   # tenancy root is fine\n')

    print("# availability domains (left empty in config = all of them, rotated)")
    for a in identity.list_availability_domains(compartment_id=tenancy).data:
        print(f"  {a.name}")

    print("\n# subnets -- pick one")
    subnets = oci.pagination.list_call_get_all_results(
        network.list_subnets, compartment_id=tenancy
    ).data
    if not subnets:
        print("  (none -- create a VCN with the console's wizard first)")
    for s in subnets:
        public = "public" if not s.prohibit_public_ip_on_vnic else "PRIVATE (no public IP!)"
        print(f'  "{s.id}"\n      {s.display_name}  [{public}]')

    print(f"\n# newest Ubuntu 22.04 image for {cfg.shape}")
    imgs = compute.list_images(
        compartment_id=tenancy, shape=cfg.shape,
        operating_system="Canonical Ubuntu", operating_system_version="22.04",
        sort_by="TIMECREATED", sort_order="DESC", limit=3,
    ).data
    for im in imgs:
        print(f'  "{im.id}"\n      {im.display_name}')

    print(f"\nPaste compartment_id / subnet_id / image_id into {CONFIG_PATH}")
    return 0


def _install_signals(g: Grabber) -> None:
    def handler(signum, _frame):
        log.info("got signal %s -- finishing up", signal.Signals(signum).name)
        g.stop_event.set()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, handler)


def cmd_run(_args) -> int:
    setup_logging(to_console=True)
    g = Grabber(Config.load(CONFIG_PATH))
    _install_signals(g)
    return g.run()


def cmd_start(_args) -> int:
    if (pid := running_pid()):
        die(f"already running as pid {pid} -- use `status` or `stop`")
    cfg = Config.load(CONFIG_PATH)
    cfg.validate()                      # fail loudly *before* detaching
    if os.name != "posix":
        die("`start` needs POSIX fork; on Windows use `run` inside a background job")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if os.fork() > 0:                                   # parent
        time.sleep(1.0)
        pid = running_pid()
        print(f"started in background as pid {pid}" if pid else
              "child exited immediately -- check the log", file=sys.stderr)
        print(f"log:    {LOG_FILE}\nstatus: {sys.argv[0]} status")
        return 0 if pid else 1

    os.setsid()                                         # detach from the terminal
    if os.fork() > 0:
        os._exit(0)
    os.chdir(str(HERE))
    os.umask(0o077)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    PID_FILE.write_text(str(os.getpid()))
    setup_logging(to_console=False)
    try:
        g = Grabber(cfg)
        _install_signals(g)
        rc = g.run()
    except SystemExit as e:
        rc = int(e.code or 0)
    except Exception:
        log.exception("crashed")
        rc = 1
    finally:
        PID_FILE.unlink(missing_ok=True)
    os._exit(rc)


def cmd_stop(_args) -> int:
    pid = running_pid()
    if not pid:
        PID_FILE.unlink(missing_ok=True)
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not pid_alive(pid):
            print(f"stopped pid {pid}")
            PID_FILE.unlink(missing_ok=True)
            return 0
        time.sleep(0.25)
    os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    print(f"killed pid {pid}")
    return 0


def cmd_status(_args) -> int:
    pid = running_pid()
    print(f"daemon:  {'running, pid ' + str(pid) if pid else 'not running'}")
    st = read_state()
    if not st:
        print("state:   (nothing recorded yet)")
        return 0
    for k in ("status", "attempts", "capacity_misses", "elapsed_min",
              "started_at", "shape", "last_reason"):
        if k in st:
            print(f"{k + ':':16}{st[k]}")
    return 0


def cmd_logs(args) -> int:
    if not LOG_FILE.exists():
        die(f"no log yet at {LOG_FILE}")
    if args.follow:
        os.execvp("tail", ["tail", "-f", str(LOG_FILE)])
    os.execvp("tail", ["tail", "-n", str(args.lines), str(LOG_FILE)])


def cmd_systemd(_args) -> int:
    print(f"""\
# Survives reboots and restarts on crash. Save as
#   ~/.config/systemd/user/oci-grab.service
# then:
#   systemctl --user daemon-reload
#   systemctl --user enable --now oci-grab
#   journalctl --user -u oci-grab -f
#   loginctl enable-linger {os.environ.get('USER', 'youruser')}   # keep it running after logout

[Unit]
Description=OCI free-tier capacity grabber
After=network-online.target

[Service]
Type=simple
ExecStart={sys.executable} {Path(__file__).resolve()} run
Restart=on-failure
RestartSec=120
WorkingDirectory={HERE}

[Install]
WantedBy=default.target""")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Retry an OCI free-tier instance launch until capacity appears.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")
    for name, fn, help_ in [
        ("init", cmd_init, "write a config template"),
        ("discover", cmd_discover, "print the OCIDs you need"),
        ("run", cmd_run, "retry in the foreground"),
        ("start", cmd_start, "retry in the background"),
        ("stop", cmd_stop, "stop the background daemon"),
        ("status", cmd_status, "show daemon and attempt status"),
        ("systemd", cmd_systemd, "print a systemd user unit"),
    ]:
        sub.add_parser(name, help=help_).set_defaults(func=fn)
    lg = sub.add_parser("logs", help="show the log")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("-n", "--lines", type=int, default=40)
    lg.set_defaults(func=cmd_logs)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
