import asyncio
from aiodocker import Docker
from aiodocker.exceptions import DockerContainerError, DockerError

import time


async def do_start_worker(delay, what):
    docker_image = "local/dask-sidecar:production"
    await asyncio.sleep(delay)
    print(what)
    container_config = {
        # "Env": env_vars,
        # "Cmd": "run",
        "Image": docker_image,
        "HostConfig": {
            "Init": True,
            "AutoRemove": False,
            "Binds": [
                # NOTE: the docker engine is mounted, so only named volumes are usable. Therefore for a selective
                # subfolder mount we need to get the path as seen from the host computer (see https://github.com/ITISFoundation/osparc-simcore/issues/1723)
                "/var/run/docker.sock:/var/run/docker.sock",
            ],
        },
    }
    async with Docker() as docker_client:
        container = await docker_client.containers.create(config=container_config)
        await container.start()
        return {"container_id": container.id}

async def main():
    print(f"started at {time.strftime('%X')}")

    await do_start_worker(1, "hello")
    await do_start_worker(2, "world")

    print(f"finished at {time.strftime('%X')}")


asyncio.run(main())
