import asyncio
import errno
import functools
import grp
import os
import pwd
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from aiodocker import Docker
from aiodocker.containers import DockerContainer
from aiodocker.exceptions import DockerContainerError, DockerError
from aiodocker.volumes import DockerVolume
from sqlalchemy import schema
from sqlalchemy.sql.operators import exists
from traitlets import Integer, List, Unicode, Bool

from ..traitlets import Type
from .base import ClusterConfig
from .db_base import DBBackendBase

__all__ = ("OsparcClusterConfig", "OsparcBackend", "UnsafeOsparcBackend")


class OsparcClusterConfig(ClusterConfig):
    """Dask cluster configuration options when running as osparc processes"""

    pass


def _signal(pid, sig):
    """Send given signal to a pid.

    Returns True if the process still exists, False otherwise."""
    try:
        os.kill(pid, sig)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        raise
    return True


def is_running(pid):
    return _signal(pid, 0)


async def wait_is_shutdown(pid, timeout=10):
    """Wait for a pid to shutdown, using exponential backoff"""
    pause = 0.1
    while timeout >= 0:
        if not _signal(pid, 0):
            return True
        await asyncio.sleep(pause)
        timeout -= pause
        pause *= 2
    return False


@functools.lru_cache()
def getpwnam(username):
    return pwd.getpwnam(username)


class OsparcBackend(DBBackendBase):
    """A cluster backend that launches osparc processes.

    Requires super-user permissions in order to run processes for the
    requesting username.
    """

    cluster_config_class = Type(
        "dask_gateway_server.backends.osparc.OsparcClusterConfig",
        klass="dask_gateway_server.backends.base.ClusterConfig",
        help="The cluster config class to use",
        config=True,
    )

    sigint_timeout = Integer(
        10,
        help="""
        Seconds to wait for process to stop after SIGINT.

        If the process has not stopped after this time, a SIGTERM is sent.
        """,
        config=True,
    )

    sigterm_timeout = Integer(
        5,
        help="""
        Seconds to wait for process to stop after SIGTERM.

        If the process has not stopped after this time, a SIGKILL is sent.
        """,
        config=True,
    )

    sigkill_timeout = Integer(
        5,
        help="""
        Seconds to wait for process to stop after SIGKILL.

        If the process has not stopped after this time, a warning is logged and
        the process is deemed a zombie process.
        """,
        config=True,
    )

    clusters_directory = Unicode(
        help="""
        The base directory for cluster working directories.

        A subdirectory will be created for each new cluster which will serve as
        the working directory for that cluster. On cluster shutdown the
        subdirectory will be removed.

        If not specified, a temporary directory will be used for each cluster.
        """,
        config=True,
    )

    inherited_environment = List(
        [
            "PATH",
            "PYTHONPATH",
            "CONDA_ROOT",
            "CONDA_DEFAULT_ENV",
            "VIRTUAL_ENV",
            "LANG",
            "LC_ALL",
        ],
        help="""
        Whitelist of environment variables for the scheduler and worker
        processes to inherit from the Dask-Gateway process.
        """,
        config=True,
    )

    run_on_host = Bool(
        False,
        help="""
        yada
        """,
        config=True,
    )

    run_in_swarm = Bool(
        True,
        help="""
        yada
        """,
        config=True,
    )

    # default_host = "dask-gateway-server-osparc"
    default_host = "0.0.0.0"
    # default_host = "172.16.8.64"
    containers = {}

    #run_on_host = False
    #run_in_swarm = True

    def set_file_permissions(self, paths, username):
        pwnam = getpwnam(username)
        for p in paths:
            os.chown(p, pwnam.pw_uid, pwnam.pw_gid)

    def make_preexec_fn(self, cluster):  # pragma: nocover
        # Borrowed and modified from jupyterhub/spawner.py
        pwnam = getpwnam(cluster.username)
        uid = pwnam.pw_uid
        gid = pwnam.pw_gid
        groups = [g.gr_gid for g in grp.getgrall() if cluster.username in g.gr_mem]
        workdir = cluster.state["workdir"]

        def preexec():
            os.setgid(gid)
            try:
                os.setgroups(groups)
            except Exception as e:
                print("Failed to set groups %s" % e, file=sys.stderr)
            os.setuid(uid)
            os.chdir(workdir)

        return preexec

    def setup_working_directory(self, cluster):  # pragma: nocover
        if self.clusters_directory:
            workdir = os.path.join(self.clusters_directory, cluster.name)
        else:
            workdir = tempfile.mkdtemp(prefix="dask", suffix=cluster.name)
        certsdir = self.get_certs_directory(workdir)
        logsdir = self.get_logs_directory(workdir)

        paths = [workdir, certsdir, logsdir]
        for path in paths:
            os.makedirs(path, 0o700, exist_ok=True)

        cert_path, key_path = self._get_tls_paths(workdir)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for path, data in [(cert_path, cluster.tls_cert), (key_path, cluster.tls_key)]:
            with os.fdopen(os.open(path, flags, 0o600), "wb") as fil:
                fil.write(data)
                paths.extend(path)

        self.set_file_permissions(paths, cluster.username)

        self.log.debug(
            "Working directory %s for cluster %s created", workdir, cluster.name
        )
        return workdir

    def cleanup_working_directory(self, workdir):
        if os.path.exists(workdir):
            try:
                shutil.rmtree(workdir)
                self.log.debug("Working directory %s removed", workdir)
            except Exception:  # pragma: nocover
                self.log.error("Failed to remove working directory %r", workdir)

    def get_certs_directory(self, workdir):
        return os.path.join(workdir, ".certs")

    def get_logs_directory(self, workdir):
        return os.path.join(workdir, "logs")

    def _get_tls_paths(self, workdir):
        certsdir = self.get_certs_directory(workdir)
        cert_path = os.path.join(certsdir, "dask.crt")
        key_path = os.path.join(certsdir, "dask.pem")
        return cert_path, key_path

    def get_tls_paths(self, cluster):
        return self._get_tls_paths(cluster.state["workdir"])

    def get_env(self, cluster):
        env = super().get_env(cluster)
        for key in self.inherited_environment:
            if key in os.environ:
                env[key] = os.environ[key]
        # env["USER"] = cluster.username
        return env

    async def start_process(self, cluster, cmd, env, name):
        workdir = cluster.state["workdir"]
        logsdir = self.get_logs_directory(workdir)
        log_path = os.path.join(logsdir, name + ".log")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = None
        try:
            fd = os.open(log_path, flags, 0o755)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                preexec_fn=self.make_preexec_fn(cluster),
                start_new_session=True,
                env=env,
                stdout=fd,
                stderr=asyncio.subprocess.STDOUT,
            )
        finally:
            if fd is not None:
                os.close(fd)
        return proc.pid

    async def stop_process(self, pid):
        methods = [
            ("SIGINT", signal.SIGINT, self.sigint_timeout),
            ("SIGTERM", signal.SIGTERM, self.sigterm_timeout),
            ("SIGKILL", signal.SIGKILL, self.sigkill_timeout),
        ]

        for msg, sig, timeout in methods:
            self.log.debug("Sending %s to process %d", msg, pid)
            _signal(pid, sig)
            if await wait_is_shutdown(pid, timeout):
                return

        if is_running(pid):
            # all attempts failed, zombie process
            self.log.warn("Failed to stop process %d", pid)

    async def do_start_cluster(self, cluster):
        workdir = self.setup_working_directory(cluster)
        yield {"workdir": workdir}

        self.log.info(self.get_scheduler_command(cluster))
        self.log.info(self.get_scheduler_env(cluster))
        pid = await self.start_process(
            cluster,
            self.get_scheduler_command(cluster),
            self.get_scheduler_env(cluster),
            "scheduler",
        )
        yield {"workdir": workdir, "pid": pid}

    async def do_stop_cluster(self, cluster):
        pid = cluster.state.get("pid")
        if pid is not None:
            await self.stop_process(pid)

        workdir = cluster.state.get("workdir")
        if workdir is not None:
            self.cleanup_working_directory(workdir)

    def _check_status(self, o):
        pid = o.state.get("pid")
        return pid is not None and is_running(pid)

    async def do_check_clusters(self, clusters):
        return [self._check_status(c) for c in clusters]

    async def do_start_worker(self, worker):
        cmd = self.get_worker_command(worker.cluster, worker.name)
        env = self.get_worker_env(worker.cluster)

        if not self.run_on_host:
            scheduler_url = urlsplit(worker.cluster.scheduler_address)
            port = scheduler_url.netloc.split(":")[1]
            netloc = "dask-gateway-server-osparc" + ":" + port

            scheduler_address = urlunsplit(scheduler_url._replace(netloc=netloc))
        else:
            scheduler_address = (
                worker.cluster.scheduler_address
            )  # urlsplit(worker.cluster.scheduler_address)
            # scheduler_address =  urlunsplit(scheduler_url._replace(scheme="tcp"))

        db_address = f"{self.default_host}:8787"
        workdir = worker.cluster.state.get("workdir")

        self.log.info("Workdir: %s", workdir)
        self.log.info("scheduler_address: %s", scheduler_address)

        env.update(
            {
                "DASK_SCHEDULER_ADDRESS": scheduler_address,
                "DASK_DASHBOARD_ADDRESS": db_address,
                "DASK_WORKER_NAME": f"{worker.name}",
                "GATEWAY_WORK_FOLDER": f"{workdir}",
            }
        )
        if self.run_on_host:
            if "PATH" in env:
                del env["PATH"]

        docker_image = "local/dask-sidecar:production"
        workdir = worker.cluster.state.get("workdir")

        container_config = {}
        try:
            async with Docker() as docker_client:
                if not self.run_in_swarm:
                    env_vars = [f"{key}={value}" for key, value in env.items()]
                    container_config = {
                        "Env": env_vars,
                        "Image": docker_image,
                        "HostConfig": {
                            "Init": True,
                            "AutoRemove": False,
                            "Binds": [
                                f"{workdir}/input:/input",
                                f"{workdir}/output:/output",
                                f"{workdir}/log:/log",
                                f"{workdir}:{workdir}",
                                "/var/run/docker.sock:/var/run/docker.sock",
                            ],
                            "NetworkMode": "dask-gateway_dask_net"
                            if not self.run_on_host
                            else "",
                        },
                    }

                    for network_details in await docker_client.networks.list():
                        n = network_details["Name"]
                        self.log.info(n)
                        id = network_details["Id"]

                    container = await docker_client.containers.create(
                        config=container_config
                    )
                    await container.start()
                    self.containers[container.id] = container
                    container_data = await container.show()
                    counter = 0
                    await asyncio.sleep(2)
                    while not container_data["State"]["Running"] and counter < 10:
                        await asyncio.sleep(2)
                        container_data = await container.show()
                        counter + counter + 1

                    yield {"container_id": container.id}
                else:
                    for folder in [
                        f"{workdir}/input",
                        f"{workdir}/output",
                        f"{workdir}/log",
                    ]:
                        p = Path(folder)
                        p.mkdir(parents=True, exist_ok=True)

                    volume_attributes = await DockerVolume(
                        docker_client, "dask-gateway_gateway_data"
                    ).show()
                    vol_mount_point = volume_attributes["Mountpoint"]

                    # env.update( {"GATEWAY_WORK_FOLDER": f"{vol_mount_point}/{worker.cluster.name}"})

                    mounts = [
                        # docker socket needed to use the docker api
                        {
                            "Source": "/var/run/docker.sock",
                            "Target": "/var/run/docker.sock",
                            "Type": "bind",
                            "ReadOnly": True,
                        },
                        {
                            "Source": f"{vol_mount_point}/{worker.cluster.name}/input",
                            "Target": "/input",
                            "Type": "bind",
                            "ReadOnly": False,
                        },
                        {
                            "Source": f"{vol_mount_point}/{worker.cluster.name}/output",
                            "Target": "/output",
                            "Type": "bind",
                            "ReadOnly": False,
                        },
                        {
                            "Source": f"{vol_mount_point}/{worker.cluster.name}/log",
                            "Target": "/log",
                            "Type": "bind",
                            "ReadOnly": False,
                        },
                        {
                            "Source": f"{vol_mount_point}/{worker.cluster.name}",
                            "Target": f"{workdir}",
                            "Type": "bind",
                            "ReadOnly": False,
                        },
                    ]

                    container_config = {
                        "Env": env,
                        "Image": docker_image,
                        "Init": True,
                        "Mounts": mounts,
                    }

                    service_name = worker.name
                    service_parameters = {
                        "name": service_name,
                        "task_template": {
                            "ContainerSpec": container_config,
                        },
                        "networks": ["dask-gateway_dask_net"],
                    }

                    self.log.info("Starting service %s", service_name)

                    service = await docker_client.services.create(**service_parameters)
                    self.log.info("Service %s started", service_name)

                    if "ID" not in service:
                        # error while starting service
                        self.log.error("OOPS service not created")

                    # get the full info from docker
                    service = await docker_client.services.inspect(service["ID"])
                    service_name = service["Spec"]["Name"]
                    self.log.info("Waiting for service %s to start", service_name)
                    while True:
                        tasks = await docker_client.tasks.list(
                            filters={"service": service_name}
                        )
                        if tasks and len(tasks) == 1:
                            task = tasks[0]
                            task_state = task["Status"]["State"]
                            self.log.info("%s %s", service["ID"], task_state)
                            if task_state in ("failed", "rejected"):
                                self.log.error(
                                    "Error while waiting for service with %s",
                                    task["Status"],
                                )
                            if task_state in ("running", "complete"):
                                break
                        await asyncio.sleep(1)

                    self.log.info("Service %s is running", worker.name)
                    yield {"service_id": service["ID"]}

        except DockerContainerError:
            self.log.exception(
                "Error while running %s with parameters %s",
                docker_image,
                container_config,
            )
            raise
        except DockerError:
            self.log.exception(
                "Unknown error while trying to run %s with parameters %s",
                docker_image,
                container_config,
            )
            raise
        except asyncio.CancelledError:
            self.log.warn("Container run was cancelled")
            raise

    async def _stop_container(self, worker):
        container_id = worker.state.get("container_id")
        if container_id is not None:
            self.log.info("Stopping container %s", container_id)
            try:
                async with Docker() as docker_client:
                    container = await docker_client.containers.get(container_id)
                    await container.stop()
                    await container.delete()

            except DockerContainerError:
                self.log.exception(
                    "Error while stopping container with id %s", container_id
                )

    async def _stop_service(self, worker):
        service_id = worker.state.get("service_id")
        if service_id is not None:
            self.log.info("Stopping service %s", service_id)
            try:
                async with Docker() as docker_client:
                    await docker_client.services.delete(service_id)

            except DockerContainerError:
                self.log.exception(
                    "Error while stopping service with id %s", service_id
                )

    async def do_stop_worker(self, worker):
        if self.run_in_swarm:
            await self._stop_service(worker)
        else:
            await self._stop_container(worker)

    async def _check_container_status(self, worker):
        container_id = worker.state.get("container_id")
        if container_id:
            try:
                async with Docker() as docker_client:
                    container = await docker_client.containers.get(container_id)
                    container_data = await container.show()
                    return container_data["State"]["Running"]
            except DockerContainerError:
                self.log.exception(
                    "Error while checking container with id %s", container_id
                )

        return False

    async def _check_service_status(self, worker):
        service_id = worker.state.get("service_id")
        if service_id:
            try:
                async with Docker() as docker_client:
                    service = await docker_client.services.inspect(service_id)
                    if service:
                        service_name = service["Spec"]["Name"]
                        tasks = await docker_client.tasks.list(
                            filters={"service": service_name}
                        )
                        if tasks and len(tasks) == 1:
                            service_state = tasks[0]["Status"]["State"]
                            self.log.log(
                                "State of %s  is %s", service_name, service_state
                            )
                            return service_state == "running"
            except DockerContainerError:
                self.log.exception(
                    "Error while checking container with id %s", service_id
                )

        return False

    async def do_check_workers(self, workers):
        ok = [False] * len(workers)
        for i, w in enumerate(workers):
            if self.run_in_swarm:
                ok[i] = await self._check_service_status(w)
            else:
                ok[i] = await self._check_container_status(w)

        return ok


class UnsafeOsparcBackend(OsparcBackend):
    """A version of OsparcBackend that doesn't set permissions.

    FOR TESTING ONLY! This provides no user separations - clusters run with the
    same level of permission as the gateway.
    """

    def make_preexec_fn(self, cluster):
        workdir = cluster.state["workdir"]

        def preexec():  # pragma: nocover
            os.chdir(workdir)

        return preexec

    def set_file_permissions(self, paths, username):
        pass
