# project aquarium's backend
# Copyright (C) 2021 SUSE, LLC.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

import shlex
from logging import Logger
from pathlib import Path
from typing import Dict, List, Optional

from fastapi.logger import logger as fastapi_logger
from pydantic.main import BaseModel

from gravel.controllers.errors import GravelError
from gravel.controllers.gstate import GlobalState
from gravel.controllers.inventory.disks import DiskDevice
from gravel.controllers.inventory.nodeinfo import NodeInfoModel
from gravel.controllers.utils import aqr_run_cmd

logger: Logger = fastapi_logger


class UnknownDeviceError(GravelError):
    pass


class UnavailableDeviceError(GravelError):
    pass


class MountError(GravelError):
    pass


class LVMError(GravelError):
    pass


class SystemDiskNotMountedError(GravelError):
    pass


class OverlayError(GravelError):
    pass


class MountEntry(BaseModel):
    source: str
    dest: str


AQR_SYSTEM_PATH = "/var/lib/aquarium-system"


def get_mounts() -> List[MountEntry]:
    proc: Path = Path("/proc/mounts")
    assert proc.exists()

    lst: List[MountEntry] = []
    with proc.open(mode="r", encoding="utf-8") as f:
        for line in f.readlines():
            fields: List[str] = line.split(" ")
            if len(fields) < 2:
                continue
            src = fields[0].strip()
            dst = fields[1].strip()
            lst.append(MountEntry(source=src, dest=dst))
    return lst


class SystemDisk:

    _gstate: GlobalState
    _overlaydirs: Dict[str, str] = {
        "etc": "/etc",
        "logs": "/var/log",
        "aquarium": "/var/lib/aquarium",
        "roothome": "/root",
        "sysctl": "/usr/lib/sysctl.d",
    }
    _bindmounts: Dict[str, str] = {"ceph": "/var/lib/ceph"}

    def __init__(self, gstate: GlobalState) -> None:
        self._gstate = gstate

    @property
    def mounted(self):
        mounts: List[MountEntry] = get_mounts()
        for entry in mounts:
            if (
                entry.source == "/dev/mapper/aquarium-systemdisk"
                and entry.dest == AQR_SYSTEM_PATH
            ):
                return True
        return False

    async def lvm(self, args: str) -> None:
        cmd: List[str] = ["lvm"] + shlex.split(args)
        retcode, _, err = await aqr_run_cmd(cmd)

        if retcode != 0:
            raise LVMError(msg=err)

    async def create(self, devicestr: str) -> None:

        logger.debug(f"prepare system disk: {devicestr}")
        inventory: Optional[NodeInfoModel] = self._gstate.inventory.latest
        assert inventory is not None

        device: Optional[DiskDevice] = next(
            (d for d in inventory.disks if d.path == devicestr), None
        )

        # ascertain whether we can use this device
        if not device:
            raise UnknownDeviceError(f"Device {devicestr} not known.")
        elif not device.available:
            raise UnavailableDeviceError(
                f"Device {devicestr} is not available."
            )

        # create partitions
        logger.debug(f"prepare system disk: device: {device}")
        devpath = device.path

        def _create_overlay_dir(path: Path, dirname: str) -> None:
            assert path.exists()
            dirpath: Path = path.joinpath(dirname)
            assert not dirpath.exists()
            dirpath.mkdir()
            dirpath.joinpath("overlay").mkdir()
            dirpath.joinpath("temp").mkdir()

        try:
            # create lvm volume
            await self.lvm(f"pvcreate {devpath}")
            await self.lvm(f"vgcreate aquarium {devpath} --addtag @aquarium")
            await self.lvm("lvcreate -l 50%VG -n systemdisk aquarium")
            await self.lvm("lvcreate -l 50%VG -n containers aquarium")

            lvmdev: Path = Path("/dev/mapper/aquarium-systemdisk")
            assert lvmdev.exists()
            ctrdev: Path = Path("/dev/mapper/aquarium-containers")
            assert ctrdev.exists()

            # format systemdisk with xfs
            await aqr_run_cmd(shlex.split(f"mkfs.xfs -m bigtime=1 {lvmdev}"))
            # format containers lv with btrfs
            await aqr_run_cmd(shlex.split(f"mkfs.btrfs {ctrdev}"))

            aqrmntpath: Path = Path(AQR_SYSTEM_PATH)
            aqrmntpath.mkdir(exist_ok=True, parents=True)

            await self.mount()

            for d in self._overlaydirs.keys():
                _create_overlay_dir(aqrmntpath, d)

            # some directories (like ceph's) can't be overlayed due to weirdness
            # ensued, so we'll bind mount them later.
            for d in self._bindmounts.keys():
                ourpath: Path = aqrmntpath.joinpath(d)
                assert not ourpath.exists()
                ourpath.mkdir()

            await self.unmount()

        except Exception as e:
            logger.error(f"prepare system disk > {str(e)}")
            logger.exception(e)
            raise e

    async def mount(self) -> None:
        await self._mount("systemdisk", Path(AQR_SYSTEM_PATH))
        await self._mount("containers", Path("/var/lib/containers"))

    async def _mount(self, src: str, dest: Path) -> None:
        devpath = Path(f"/dev/mapper/aquarium-{src}")
        assert devpath.exists()
        dest.mkdir(exist_ok=True, parents=True)

        aqrdev: str = devpath.as_posix()
        aqrmnt: str = dest.as_posix()

        try:
            await aqr_run_cmd(shlex.split(f"mount {aqrdev} {aqrmnt}"))
        except Exception as e:
            raise MountError(msg=str(e))

    async def unmount(self) -> None:
        await self._unmount(Path(AQR_SYSTEM_PATH))
        await self._unmount(Path("/var/lib/containers"))

    async def _unmount(self, mnt: Path) -> None:
        if not mnt.exists():
            raise MountError(msg=f"The {mnt} mount point does not exist.")

        try:
            await aqr_run_cmd(shlex.split(f"umount {mnt}"))
        except Exception as e:
            raise MountError(msg=str(e))

    async def enable(self) -> None:
        if not self.mounted:
            try:
                await self.mount()
            except MountError as e:
                raise OverlayError(msg=e.message)

        async def _overlay(lower: str, upper: str, work: str):
            mntcmd = (
                "mount -t overlay "
                f"-o lowerdir={lower},upperdir={upper},workdir={work} "
                f"overlay {lower}"
            )
            await aqr_run_cmd(shlex.split(mntcmd))

        for upper, lower in self._overlaydirs.items():
            aqrpath: Path = Path(AQR_SYSTEM_PATH)
            upperpath: Path = aqrpath.joinpath(upper)
            overlaypath: Path = upperpath.joinpath("overlay")
            temppath: Path = upperpath.joinpath("temp")
            lowerpath: Path = Path(lower)
            assert overlaypath.exists() and overlaypath.is_dir()
            assert temppath.exists() and temppath.is_dir()

            lowerpath.mkdir(parents=True, exist_ok=True)
            assert lowerpath.exists() and lowerpath.is_dir()

            try:
                await _overlay(
                    lower, overlaypath.as_posix(), temppath.as_posix()
                )
            except Exception as e:
                raise OverlayError(
                    f"Unable to overlay {upper} on {lower}: {str(e)}"
                )

        for ours, theirs in self._bindmounts.items():
            ourpath: Path = Path(AQR_SYSTEM_PATH).joinpath(ours)
            theirpath: Path = Path(theirs)
            assert ourpath.exists()
            theirpath.mkdir(parents=True, exist_ok=True)

            try:
                await aqr_run_cmd(
                    [
                        "mount",
                        "--bind",
                        ourpath.as_posix(),
                        theirpath.as_posix(),
                    ]
                )
            except Exception as e:
                raise MountError(
                    f"Unable to bind mount {ourpath} to {theirpath}: {str(e)}"
                )
