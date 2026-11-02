#!/usr/bin/env python3
"""Run an auto-pegi3s project (auto-phylo, auto-p2docking, ...) on a remote computer.

Copies a project folder to a remote computer over SSH/SFTP, points the "dir"
variable of its config file to the new location, runs the image there, and
copies the whole folder back next to the original, with the "_results" suffix.
The original folder is never modified.

The container runs detached on the remote computer, so a dropped SSH connection
does not stop it: packnrun reconnects and carries on following the run.
"""
import argparse
import getpass
import os
import re
import shlex
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import paramiko

REQUIRED_FILES = ("pipeline", "config")
IMAGE_RE = re.compile(r"^(?:pegi3s/)?(auto-[a-z0-9][a-z0-9._-]*)(?::([\w][\w.-]*))?$")
DIR_LINE_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?dir[ \t]*=(.*)$", re.MULTILINE)
CONNECTION_ERRORS = (paramiko.SSHException, OSError, EOFError)
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAY = 15


class PacknrunError(Exception):
    """An error reported to the user as a plain message."""


def log(message):
    print(f"[packnrun] {message}", flush=True)


def parse_image(image):
    """Returns the full image name, e.g. auto-phylo:4.1.1 -> pegi3s/auto-phylo:4.1.1."""
    match = IMAGE_RE.match(image)
    if not match:
        raise PacknrunError(
            f"'{image}' is not an auto-pegi3s image. Expected something like "
            "auto-phylo:4.1.1 or auto-p2docking:latest."
        )
    return f"pegi3s/{match.group(1)}:{match.group(2) or 'latest'}"


def check_project(folder):
    """Returns the resolved project folder, once checked it has the required files."""
    path = Path(folder).resolve()
    if not path.is_dir():
        raise PacknrunError(f"Project folder not found: {path}")
    missing = [name for name in REQUIRED_FILES if not (path / name).is_file()]
    if missing:
        raise PacknrunError(f"{path} does not contain: {', '.join(missing)}")
    return path


def rewrite_config(text, remote_dir):
    """Returns the config text with its "dir" variable set to remote_dir.

    Config files are sourced by bash, so the value is quoted for bash. Any other
    variable that used the old path is updated too. If "dir" is missing it is added.
    """
    new_line = f"dir={shlex.quote(remote_dir)}"
    match = DIR_LINE_RE.search(text)
    if not match:
        return f"{text.rstrip()}\n\n# Added by packnrun\n{new_line}\n"

    old_dir = match.group(1).strip().strip("'\"").rstrip("/")
    text = DIR_LINE_RE.sub(lambda _: new_line, text)
    if old_dir:
        text = re.sub(re.escape(old_dir) + r"(?=[/\"'\s]|$)", lambda _: remote_dir, text, flags=re.MULTILINE)
    return text


def safe_name(name):
    """Returns name reduced to characters safe in remote paths and container names."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class Remote:
    """An SSH connection to the remote computer that can be re-established."""

    def __init__(self, args):
        self.args = args
        self.client = None
        self.sftp = None

    def connect(self):
        self.close()
        client = paramiko.SSHClient()
        if self.args.known_hosts:
            client.load_host_keys(self.args.known_hosts)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(_WarnAndAcceptHostKey())
        client.connect(
            self.args.host,
            port=self.args.port,
            username=self.args.user,
            # With a key, this is the passphrase of the key (if it has one).
            password=self.args.password,
            key_filename=self.args.key,
            allow_agent=False,
            look_for_keys=False,
            timeout=30,
        )
        client.get_transport().set_keepalive(30)
        self.client = client
        self.sftp = client.open_sftp()

    def close(self):
        if self.client:
            self.client.close()
        self.client = self.sftp = None

    def reconnect(self):
        """Retries the connection until it works or --reconnect-timeout expires."""
        deadline = time.monotonic() + self.args.reconnect_timeout
        while True:
            try:
                self.connect()
                log("Reconnected.")
                return
            except paramiko.AuthenticationException:
                raise
            except CONNECTION_ERRORS as e:
                if time.monotonic() >= deadline:
                    raise PacknrunError(f"Could not reconnect to {self.args.host}: {e}")
                log(f"Connection lost ({e}). Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

    def run(self, command):
        """Runs a command and returns (exit code, stdout + stderr)."""
        _, stdout, _ = self.client.exec_command(command)
        stdout.channel.set_combine_stderr(True)
        output = stdout.read().decode("utf-8", "replace").strip()
        return stdout.channel.recv_exit_status(), output

    def check(self, command):
        """Runs a command that must work and returns its output."""
        code, output = self.run(command)
        if code != 0:
            raise PacknrunError(f"Remote command failed: {command}\n{output}")
        return output

    def stream(self, command):
        """Runs a command, printing its output as it arrives, and returns its exit code."""
        transport = self.client.get_transport()
        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(command)
        while True:
            data = channel.recv(65536)
            if not data:
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        if not transport.is_active():
            raise EOFError("connection closed")
        return channel.recv_exit_status()


class _WarnAndAcceptHostKey(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        log(f"WARNING: accepting unknown host key of {hostname}: "
            f"{key.get_name()} {key.get_fingerprint().hex()}. Use --known-hosts to verify it.")


def check_docker(remote):
    code, output = remote.run("docker version --format '{{.Server.Version}}'")
    if code != 0:
        raise PacknrunError(
            f"Docker is not usable by {remote.args.user} on {remote.args.host}. Check that it is "
            f"installed and that the user is in the docker group.\n{output}"
        )
    log(f"Remote Docker version: {output}")


def pull_image(remote, image):
    log(f"Pulling {image} on the remote computer...")
    if remote.stream(f"docker pull {shlex.quote(image)}") == 0:
        return
    code, _ = remote.run(f"docker image inspect {shlex.quote(image)}")
    if code != 0:
        raise PacknrunError(f"Image {image} could not be pulled and is not on the remote computer.")
    log("WARNING: pull failed, using the copy already on the remote computer.")


def upload(sftp, local_dir, remote_dir, config_text):
    """Copies local_dir to remote_dir, using config_text as the content of the config file."""
    sftp.mkdir(remote_dir)
    for root, dirs, files in os.walk(local_dir):
        relative = Path(root).relative_to(local_dir)
        remote_root = PurePosixPath(remote_dir, *relative.parts)

        for name in list(dirs):
            local_path = Path(root, name)
            remote_path = str(remote_root / name)
            if local_path.is_symlink():
                sftp.symlink(os.readlink(local_path), remote_path)
                dirs.remove(name)
            else:
                sftp.mkdir(remote_path)
                sftp.chmod(remote_path, stat.S_IMODE(local_path.stat().st_mode))

        for name in files:
            local_path = Path(root, name)
            remote_path = str(remote_root / name)
            if local_path.is_symlink():
                sftp.symlink(os.readlink(local_path), remote_path)
            elif not local_path.is_file():
                log(f"Skipping special file: {local_path}")
            elif not relative.parts and name == "config":
                with sftp.open(remote_path, "wb") as f:
                    f.write(config_text.encode("utf-8"))
            else:
                sftp.put(str(local_path), remote_path)
                sftp.chmod(remote_path, stat.S_IMODE(local_path.stat().st_mode))


def download(sftp, remote_dir, local_dir):
    """Copies remote_dir to local_dir. Files already copied are skipped, so it can be retried."""
    local_dir.mkdir(exist_ok=True)
    for entry in sftp.listdir_attr(remote_dir):
        remote_path = f"{remote_dir}/{entry.filename}"
        local_path = local_dir / entry.filename
        if stat.S_ISLNK(entry.st_mode):
            if not local_path.is_symlink():
                os.symlink(sftp.readlink(remote_path), local_path)
        elif stat.S_ISDIR(entry.st_mode):
            download(sftp, remote_path, local_path)
            local_path.chmod(stat.S_IMODE(entry.st_mode) | stat.S_IRWXU)
        elif stat.S_ISREG(entry.st_mode):
            if local_path.is_file() and local_path.stat().st_size == entry.st_size:
                continue
            sftp.get(remote_path, str(local_path))
            local_path.chmod(stat.S_IMODE(entry.st_mode) | stat.S_IRUSR | stat.S_IWUSR)


def container_state(remote, name):
    """Returns (status, exit code) of a container."""
    status, code = remote.check(
        f"docker inspect --format '{{{{.State.Status}}}} {{{{.State.ExitCode}}}}' {name}"
    ).split()
    return status, int(code)


def follow_run(remote, name):
    """Prints the output of a container until it ends and returns its exit code."""
    since = ""
    while True:
        try:
            remote.stream(f"docker logs -f {since} {name}")
            status, code = container_state(remote, name)
            if status in ("exited", "dead"):
                return code
            time.sleep(RETRY_DELAY)
        except CONNECTION_ERRORS:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            since = f"--since {timestamp}"
            log(f"Connection lost, the run continues on {remote.args.host}.")
            remote.reconnect()


def fetch_results(remote, remote_dir, partial_dir):
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            download(remote.sftp, remote_dir, partial_dir)
            return
        except CONNECTION_ERRORS as e:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise PacknrunError(
                    f"Copying the results failed: {e}\nThe partial copy is in {partial_dir} and "
                    f"the complete results are in {remote.args.host}:{remote_dir}"
                )
            log(f"Copy interrupted ({e}), retrying...")
            remote.reconnect()


def execute(args, remote, image, project, partial_dir):
    """Does the remote work and returns the exit code of the run."""
    remote.connect()
    check_docker(remote)
    pull_image(remote, image)

    base_dir = args.remote_dir or f"{remote.sftp.normalize('.')}/packnrun"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = f"{safe_name(project.name)}_{stamp}"
    container = f"packnrun-{run_name}"

    remote.check(f"mkdir -p {shlex.quote(base_dir)}")
    remote_dir = f"{remote.sftp.normalize(base_dir)}/{run_name}"
    config_text = rewrite_config((project / "config").read_text(encoding="utf-8-sig"), remote_dir)

    log(f"Copying {project} to {args.host}:{remote_dir} ...")
    upload(remote.sftp, project, remote_dir, config_text)

    data_dir = shlex.quote(remote_dir)
    log(f"Starting {image} as container {container} ...")
    remote.check(
        f"docker run -d --name {container} -v {data_dir}:/data "
        f"-v /var/run/docker.sock:/var/run/docker.sock {shlex.quote(image)}"
    )
    log(f"If packnrun is interrupted, the run continues on {args.host}. "
        f"To stop it there: docker rm -f {container}")

    exit_code = follow_run(remote, container)
    remote.run(f"docker rm {container}")
    log(f"Run finished with exit code {exit_code}.")

    # The images run as root, so the login user may be unable to read or delete the outputs.
    owner = remote.check("echo $(id -u):$(id -g)")
    code, output = remote.run(
        f"docker run --rm --entrypoint chown -v {data_dir}:/data {shlex.quote(image)} -R {owner} /data"
    )
    if code != 0:
        log(f"WARNING: could not change the owner of the results: {output}")

    log(f"Copying the results to {partial_dir} ...")
    fetch_results(remote, remote_dir, partial_dir)
    partial_dir.rename(args.results)
    log(f"Results are in {args.results}")

    if args.cleanup:
        remote.check(f"rm -rf {data_dir}")
        log(f"Removed {args.host}:{remote_dir}")
    else:
        log(f"The remote copy was kept in {args.host}:{remote_dir}")
    return exit_code


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run an auto-pegi3s project on a remote computer and copy the results back."
    )
    parser.add_argument("image", help="image and version, e.g. auto-phylo:4.1.1 or auto-p2docking:latest")
    parser.add_argument("folder", help="project folder, containing at least the pipeline and config files")
    parser.add_argument("--host", required=True, help="name or IP address of the remote computer")
    parser.add_argument("--user", required=True, help="user name on the remote computer")
    parser.add_argument("--password", default=os.environ.get("PACKNRUN_PASSWORD"),
                        help="password, or passphrase of --key. Prefer the PACKNRUN_PASSWORD environment "
                             "variable, or leave it out to be asked (command lines are visible to other users)")
    parser.add_argument("--key", help="private SSH key file (e.g. the .pem file of an AWS key pair)")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--remote-dir", help="absolute remote folder for the runs (default: ~/packnrun)")
    parser.add_argument("--cleanup", action="store_true",
                        help="delete the remote copy once the results have been copied back")
    parser.add_argument("--known-hosts",
                        help="known_hosts file used to verify the remote computer; unknown hosts are refused")
    parser.add_argument("--reconnect-timeout", type=int, default=3600,
                        help="seconds to keep trying to reconnect after losing the connection (default: 3600)")
    args = parser.parse_args(argv)

    if args.remote_dir and not args.remote_dir.startswith("/"):
        parser.error("--remote-dir must be an absolute path")
    return args


def main(argv=None):
    args = parse_args(argv)
    remote = Remote(args)
    partial_dir = None
    try:
        image = parse_image(args.image)
        project = check_project(args.folder)
        args.results = project.with_name(f"{project.name}_results")
        partial_dir = project.with_name(f"{project.name}_results.partial")

        # Checked now to avoid finding out after a run that may take days.
        for path in (args.results, partial_dir):
            if path.exists():
                raise PacknrunError(f"{path} already exists. Move or delete it first.")
        if not os.access(project.parent, os.W_OK):
            raise PacknrunError(f"Cannot write the results to {project.parent}")
        if args.key and not os.path.isfile(args.key):
            raise PacknrunError(f"Key file not found: {args.key}")
        if not args.key and args.password is None:
            args.password = getpass.getpass(f"Password for {args.user}@{args.host}: ")

        exit_code = execute(args, remote, image, project, partial_dir)
        if exit_code != 0:
            log(f"WARNING: the pipeline failed (exit code {exit_code}). "
                "The folder was copied back so that the logs can be inspected.")
        return 1 if exit_code != 0 else 0
    except PacknrunError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except paramiko.AuthenticationException:
        print(f"ERROR: authentication failed for {args.user}@{args.host}", file=sys.stderr)
        return 1
    except paramiko.BadHostKeyException as e:
        print(f"ERROR: the host key of {e.hostname} changed. Not connecting.", file=sys.stderr)
        return 1
    except CONNECTION_ERRORS as e:
        print(f"ERROR: connection to {args.host} failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        remote.close()


if __name__ == "__main__":
    sys.exit(main())
