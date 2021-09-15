import asyncio
import enum
import errno
import functools
import grp
import os
import pwd
import shutil
import signal
import sys
import tempfile
from urllib.parse import urlparse

from aiodocker import Docker
from aiodocker.containers import DockerContainer
from aiodocker.exceptions import DockerContainerError, DockerError
from traitlets import Integer, List, Unicode
import docker


from ..traitlets import Command, Type
from .base import ClusterConfig
from .db_base import DBBackendBase

__all__ = ("OsparcClusterConfig", "OsparcBackend", "UnsafeOsparcBackend")


class OsparcClusterConfig(ClusterConfig):
    """Dask cluster configuration options when running as osparc processes"""

    # worker_cmd = Command(
    #     "docker run -d local/dask-sidecar:production", help="Shell command to start a dask worker.", config=True
    # )
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

    default_host = "127.0.0.1"

    containers = {}

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

        paths = [workdir, certsdir, logsdir, os.path.join(workdir, "input"), os.path.join(workdir, "output"), os.path.join(workdir, "log")]
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
                self.log.warn("Failed to remove working directory %r", workdir)

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
        env["USER"] = cluster.username
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
        print( self.get_scheduler_command(cluster))
        print(self.get_scheduler_env(cluster))
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

    async def _check_container_status(self, o):
        container_id = o.state.get("container_id")
        if container_id:
            try:
                async with Docker() as docker_client:
                    container = await docker_client.containers.get(container_id)
                    container_data = await container.show()
                    return container_data["State"]["Running"]
            except DockerContainerError:
                self.log.exception(
                    "Error while checking container with id %s",
                    container_id
                )
        
        return False

    async def _stop_container(self, container_id):
        try:
            async with Docker() as docker_client:
                container = await docker_client.containers.get(container_id)
                await container.stop()
        except DockerContainerError:
            self.log.exception(
                "Error while stopping container with id %s",
                container_id
            )


    async def do_check_clusters(self, clusters):
        return [self._check_status(c) for c in clusters]

    async def do_start_worker(self, worker):
        cmd = self.get_worker_command(worker.cluster, worker.name)
        print("+++++++++", cmd)
        docker_image = "local/dask-sidecar:production"
        workdir = worker.cluster.state.get("workdir")
        url = urlparse(worker.cluster.scheduler_address)
        env_vars = [f"DASK_SCHEDULER_ADDRESS=172.17.0.1:{url.port}",
                    f"DASK_DASHBOARD_ADDRESS={self.default_host}:0",
                    f"DASK_WORKER_NAME={worker.name}",
                    ]
        print(env_vars)
        return

        container_config = {
            "Env": env_vars,
            #"Cmd": "run",
            "Image": docker_image,
            "HostConfig": {
                "Init": True,
                "AutoRemove": False,
                "Binds": [
                    # NOTE: the docker engine is mounted, so only named volumes are usable. Therefore for a selective
                    # subfolder mount we need to get the path as seen from the host computer (see https://github.com/ITISFoundation/osparc-simcore/issues/1723)
                    f"{workdir}/input:/input",
                    f"{workdir}/output:/output",
                    f"{workdir}/log:/log",
                    "/var/run/docker.sock:/var/run/docker.sock",
                ],
            },
        } 
        try:
            async with Docker() as docker_client:
                container = await docker_client.containers.create(
                    config=container_config
                    )
                await container.start()
                self.containers[container.id] = container
                yield {"container_id": container.id}
                while(True):
                    await asyncio.sleep(2)
                
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
            self.log.warning("Container run was cancelled")
            raise

        # cmd = self.get_worker_command(worker.cluster, worker.name)
        # env = self.get_worker_env(worker.cluster)
        # pid = await self.start_process(
        #     worker.cluster, cmd, env, "worker-%s" % worker.name
        # )
        yield {"container_id": container.id}

    async def do_stop_worker(self, worker):
        container_id = worker.state.get("container_id")
        if container_id is not None:
            await self._stop_container(container_id)

    async def do_check_workers(self, workers):
        ok = [False] * len(workers)
        for i, w in enumerate(workers):
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
