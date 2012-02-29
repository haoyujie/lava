# Copyright (C) 2011 Linaro Limited
#
# Author: Michael Hudson-Doyle <michael.hudson@linaro.org>
# Author: Paul Larson <paul.larson@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

import contextlib
import logging
import os
import pexpect
import shutil
import subprocess
from tempfile import mkdtemp
import time
import traceback

from lava_dispatcher.utils import (
    download,
    download_with_cache,
    logging_system,
    )
from lava_dispatcher.client.base import (
    CommandRunner,
    CriticalError,
    LavaClient,
    NetworkCommandRunner,
    OperationFailed,
    )
from lava_dispatcher.client.lmc_utils import (
    generate_image,
    image_partition_mounted,
    )
from lava_dispatcher.connection import (
    LavaConmuxConnection,
    )


def _extract_partition(image, partno, tarfile):
    """Mount a partition and produce a tarball of it

    :param image: The image to mount
    :param partno: The index of the partition in the image
    :param tarfile: path and filename of the tgz to output
    """
    with image_partition_mounted(image, partno) as mntdir:
        cmd = "sudo tar -C %s -czf %s ." % (mntdir, tarfile)
        rc = logging_system(cmd)
        if rc:
            raise RuntimeError("Failed to create tarball: %s" % tarfile)


def _deploy_tarball_to_board(session, tarball_url, dest, timeout=-1):
    decompression_char = ''
    if tarball_url.endswith('.gz') or tarball_url.endswith('.tgz'):
        decompression_char = 'z'
    elif tarball_url.endswith('.bz2'):
        decompression_char = 'j'
    session.run(
        'wget -qO- %s |tar --numeric-owner -C %s -x%sf -' % (
            tarball_url, dest, decompression_char),
        timeout=timeout)

def _deploy_linaro_rootfs(session, rootfs):
    logging.info("Deploying linaro image")
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/root')
    session.run('mount /dev/disk/by-label/testrootfs /mnt/root')
    _deploy_tarball_to_board(session, rootfs, '/mnt/root', timeout=3600)

    session.run('echo linaro > /mnt/root/etc/hostname')
    #DO NOT REMOVE - diverting flash-kernel and linking it to /bin/true
    #prevents a serious problem where packages getting installed that
    #call flash-kernel can update the kernel on the master image
    session.run(
        'chroot /mnt/root dpkg-divert --local /usr/sbin/flash-kernel')
    session.run(
        'chroot /mnt/root ln -sf /bin/true /usr/sbin/flash-kernel')
    session.run('umount /mnt/root')

def _deploy_linaro_bootfs(session, bootfs):
    logging.info("Deploying linaro bootfs")
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/boot')
    session.run('mount /dev/disk/by-label/testboot /mnt/boot')
    _deploy_tarball_to_board(session, bootfs, '/mnt/boot')
    session.run('umount /mnt/boot')

def _deploy_linaro_android_testboot(session, boottbz2, pkgbz2=None):
    logging.info("Deploying test boot filesystem")
    session.run('umount /dev/disk/by-label/testboot', failok=True)
    session.run('mkfs.vfat /dev/disk/by-label/testboot '
                          '-n testboot')
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/lava/boot')
    session.run('mount /dev/disk/by-label/testboot '
                          '/mnt/lava/boot')
    _deploy_tarball_to_board(session, boottbz2, '/mnt/lava')
    if pkgbz2:
        _deploy_tarball_to_board(session, pkgbz2, '/mnt/lava')

    _recreate_uInitrd(session)

    session.run('umount /mnt/lava/boot')

def _recreate_uInitrd(session):
    logging.debug("Recreate uInitrd")
    # Original android sdcard partition layout by l-a-m-c
    sys_part_org = session._client.device_option("sys_part_android_org")
    cache_part_org = session._client.device_option("cache_part_android_org")
    data_part_org = session._client.device_option("data_part_android_org")
    # Sdcard layout in Lava image
    sys_part_lava = session._client.device_option("sys_part_android")
    data_part_lava = session._client.device_option("data_part_android")

    session.run('mkdir -p ~/tmp/')
    session.run('mv /mnt/lava/boot/uInitrd ~/tmp')
    session.run('cd ~/tmp/')

    session.run('dd if=uInitrd of=uInitrd.data ibs=64 skip=1')
    session.run('mv uInitrd.data ramdisk.cpio.gz')
    session.run(
        'gzip -d -f ramdisk.cpio.gz; cpio -i -F ramdisk.cpio')
    session.run(
        'sed -i "/mount ext4 \/dev\/block\/mmcblk0p%s/d" init.rc'
        % cache_part_org)
#    session.run(
#        'sed -i "/mount ext4 \/dev\/block\/mmcblk0p%s/d" init.rc'
#        % data_part_org)
    session.run('sed -i "s/mmcblk0p%s/mmcblk0p%s/g" init.rc'
        % (data_part_org, data_part_lava))
    session.run('sed -i "s/mmcblk0p%s/mmcblk0p%s/g" init.rc'
        % (sys_part_org, sys_part_lava))
    # for snowball the mcvblk1 is used instead of mmcblk0.
    session.run('sed -i "s/mmcblk1p%s/mmcblk1p%s/g" init.rc'
        % (data_part_org, data_part_lava))
    session.run('sed -i "s/mmcblk1p%s/mmcblk1p%s/g" init.rc'
        % (sys_part_org, sys_part_lava))
    # failok true for this?:
    session.run(
        'sed -i "/export PATH/a \ \ \ \ export PS1 root@linaro: " init.rc')

    session.run("cat init.rc")

    session.run(
        'cpio -i -t -F ramdisk.cpio | cpio -o -H newc | \
            gzip > ramdisk_new.cpio.gz')

    session.run(
        'mkimage -A arm -O linux -T ramdisk -n "Android Ramdisk Image" \
            -d ramdisk_new.cpio.gz uInitrd')

    session.run('cd -')
    session.run('mv ~/tmp/uInitrd /mnt/lava/boot/uInitrd')
    session.run('rm -rf ~/tmp')

def _deploy_linaro_android_testrootfs(session, systemtbz2, rootfstype):
    logging.info("Deploying the test root filesystem")
#    sdcard_part_lava = session._client.device_option("sdcard_part_android")

    session.run('umount /dev/disk/by-label/testrootfs', failok=True)
    session.run(
        'mkfs -t %s -q /dev/disk/by-label/testrootfs -L testrootfs' % rootfstype)
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/lava/system')
    session.run(
        'mount /dev/disk/by-label/testrootfs /mnt/lava/system')
    _deploy_tarball_to_board(session, systemtbz2, '/mnt/lava', timeout=600)

#    sed_cmd = "/dev_mount sdcard \/mnt\/sdcard/c dev_mount sdcard /mnt/sdcard %s " \
#        "/devices/platform/omap/omap_hsmmc.0/mmc_host/mmc0" % sdcard_part_lava
#    session.run(
#        'sed -i "%s" /mnt/lava/system/etc/vold.fstab' % sed_cmd)
    session.run(
        'sed -i "s/^PS1=.*$/PS1=\'root@linaro: \'/" /mnt/lava/system/etc/mkshrc',
        failok=True)
    session.run('umount /mnt/lava/system')

def _purge_linaro_android_sdcard(session):
    logging.info("Reformatting Linaro Android sdcard filesystem")
    session.run('mkfs.vfat /dev/disk/by-label/sdcard -n sdcard')
    session.run('udevadm trigger')

def _deploy_linaro_android_data(session, datatbz2):
    ##consider the compatiblity, here use the existed sdcard partition
    data_label = 'sdcard'
    session.run('mkfs.ext4 -q /dev/disk/by-label/%s -L %s' % (data_label, data_label))
    session.run('udevadm trigger')
    session.run('mkdir -p /mnt/lava/data')
    session.run('mount /dev/disk/by-label/%s /mnt/lava/data' % (data_label))
    _deploy_tarball_to_board(session, datatbz2, '/mnt/lava', timeout=600)
    session.run('umount /mnt/lava/data')

class PrefixCommandRunner(CommandRunner):
    """A CommandRunner that prefixes every command run with a given string.

    The motivating use case is to prefix every command with 'chroot
    $LOCATION'.
    """

    def __init__(self, prefix, connection, prompt_str):
        super(PrefixCommandRunner, self).__init__(connection, prompt_str)
        if not prefix.endswith(' '):
            prefix += ' '
        self._prefix = prefix

    def run(self, cmd, response=None, timeout=-1, failok=False):
        return super(PrefixCommandRunner, self).run(
            self._prefix + cmd, response, timeout, failok)


class MasterCommandRunner(NetworkCommandRunner):
    """A CommandRunner to use when the board is booted into the master image.

    See `LavaClient.master_session`.
    """

    def __init__(self, client):
        super(MasterCommandRunner, self).__init__(client, client.master_str)

    def get_master_ip(self):
        #get master image ip address
        try:
            self.wait_network_up()
        except:
            logging.warning(traceback.format_exc())
            return None
        #tty device uses minimal match, see pexpect wiki
        pattern1 = "<(\d?\d?\d?\.\d?\d?\d?\.\d?\d?\d?\.\d?\d?\d?)>"
        cmd = ("ifconfig %s | grep 'inet addr' | awk -F: '{print $2}' |"
                "awk '{print \"<\" $1 \">\"}'" % self._client.default_network_interface)
        self.run(
            cmd, [pattern1, pexpect.EOF, pexpect.TIMEOUT], timeout=5)
        if self.match_id == 0:
            ip = self.match.group(1)
            logging.debug("Master image IP is %s" % ip)
            return ip
        return None


class LavaMasterImageClient(LavaClient):

    def __init__(self, context, config):
        super(LavaMasterImageClient, self).__init__(context, config)
        self.proc = LavaConmuxConnection(config, self.sio)

    @property
    def master_str(self):
        return self.device_option("MASTER_STR")

    def decompress(self, image_file):
        for suffix, command in [('.gz', 'gunzip'),
                                ('.xz', 'unxz'),
                                ('.bz2', 'bunzip2')]:
            if image_file.endswith(suffix):
                logging.info("Uncompressing %s with %s", image_file, command)
                uncompressed_name = image_file[:-len(suffix)]
                subprocess.check_call(
                    [command, '-c', image_file], stdout=open(uncompressed_name, 'w'))
                return uncompressed_name
        return image_file


    def deploy_linaro(self, hwpack=None, rootfs=None, image=None,
                      kernel_matrix=None, use_cache=True, rootfstype='ext3'):
        LAVA_IMAGE_TMPDIR = self.context.lava_image_tmpdir
        LAVA_IMAGE_URL = self.context.lava_image_url
        try:
            if image is None:
                if hwpack is None or rootfs is None:
                    raise CriticalError(
                        "must specify both hwpack and rootfs when not specifying image")
                else:
                    image_file = generate_image(self, hwpack, rootfs, kernel_matrix, use_cache)
            else:
                if hwpack is not None or rootfs is not None or kernel_matrix is not None:
                    raise CriticalError(
                        "cannot specify hwpack or rootfs when specifying image")
                tarball_dir = mkdtemp(dir=LAVA_IMAGE_TMPDIR)
                os.chmod(tarball_dir, 0755)
                if use_cache:
                    lava_cachedir = self.context.lava_cachedir
                    image_file = download_with_cache(image, tarball_dir, lava_cachedir)
                else:
                    image_file = download(image, tarball_dir)
                image_file = self.decompress(image_file)
            boot_tgz, root_tgz = self._generate_tarballs(image_file)
        except CriticalError:
            raise
        except:
            logging.error("Deployment tarballs preparation failed")
            tb = traceback.format_exc()
            self.sio.write(tb)
            raise CriticalError("Deployment tarballs preparation failed")

        logging.info("Booting master image")
        try:
            self.boot_master_image()
            boot_tarball = boot_tgz.replace(LAVA_IMAGE_TMPDIR, '')
            root_tarball = root_tgz.replace(LAVA_IMAGE_TMPDIR, '')
            boot_url = '/'.join(u.strip('/') for u in [
                LAVA_IMAGE_URL, boot_tarball])
            root_url = '/'.join(u.strip('/') for u in [
                LAVA_IMAGE_URL, root_tarball])
            with self._master_session() as session:
                self._format_testpartition(session, rootfstype)

                logging.info("Waiting for network to come up")
                try:
                    session.wait_network_up()
                except:
                    logging.error("Unable to reach LAVA server, check network")
                    tb = traceback.format_exc()
                    self.sio.write(tb)
                    raise CriticalError("Unable to reach LAVA server, check network")

                try:
                    _deploy_linaro_rootfs(session, root_url)
                    _deploy_linaro_bootfs(session, boot_url)
                except:
                    logging.error("Deployment failed")
                    tb = traceback.format_exc()
                    self.sio.write(tb)
                    raise CriticalError("Deployment failed")
        finally:
            shutil.rmtree(os.path.dirname(boot_tgz))

    def deploy_linaro_android(self, boot, system, data, pkg=None, use_cache=True, rootfstype='ext4'):
        LAVA_IMAGE_TMPDIR = self.context.lava_image_tmpdir
        LAVA_IMAGE_URL = self.context.lava_image_url
        logging.info("Deploying Android on %s" % self.hostname)
        logging.info("  boot: %s" % boot)
        logging.info("  system: %s" % system)
        logging.info("  data: %s" % data)
        logging.info("Boot master image")
        try:
            self.boot_master_image()
            with self._master_session() as session:
                logging.info("Waiting for network to come up...")
                try:
                    session.wait_network_up()
                except:
                    logging.error("Unable to reach LAVA server, check network")
                    tb = traceback.format_exc()
                    self.sio.write(tb)
                    raise CriticalError("Unable to reach LAVA server, check network")

                try:
                    boot_tbz2, system_tbz2, data_tbz2, pkg_tbz2 = \
                        self._download_tarballs(boot, system, data, pkg, use_cache)
                except:
                    logging.error("Unable to download artifacts for deployment")
                    tb = traceback.format_exc()
                    self.sio.write(tb)
                    raise CriticalError("Unable to download artifacts for deployment")

                boot_tarball = boot_tbz2.replace(LAVA_IMAGE_TMPDIR, '')
                system_tarball = system_tbz2.replace(LAVA_IMAGE_TMPDIR, '')
                data_tarball = data_tbz2.replace(LAVA_IMAGE_TMPDIR, '')

                boot_url = '/'.join(u.strip('/') for u in [
                    LAVA_IMAGE_URL, boot_tarball])
                system_url = '/'.join(u.strip('/') for u in [
                    LAVA_IMAGE_URL, system_tarball])
                data_url = '/'.join(u.strip('/') for u in [
                    LAVA_IMAGE_URL, data_tarball])

                if pkg_tbz2:
                    pkg_tarball = pkg_tbz2.replace(LAVA_IMAGE_TMPDIR, '')
                    pkg_url = '/'.join(u.strip('/') for u in [
                        LAVA_IMAGE_URL, pkg_tarball])
                else:
                    pkg_url = None

                try:
                    _deploy_linaro_android_testboot(session, boot_url, pkg_url)
                    _deploy_linaro_android_testrootfs(session, system_url, rootfstype)
#                    _purge_linaro_android_sdcard(session)
                    _deploy_linaro_android_data(session, data_url)
                except:
                    logging.error("Android deployment failed")
                    tb = traceback.format_exc()
                    self.sio.write(tb)
                    raise CriticalError("Android deployment failed")
        finally:
            shutil.rmtree(self.tarball_dir)
            logging.info("Android image deployment exiting")

    def _download_tarballs(self, boot_url, system_url, data_url, pkg_url=None,
            use_cache=True):
        """Download tarballs from a boot, system and data tarball url

        :param boot_url: url of the Linaro Android boot tarball to download
        :param system_url: url of the Linaro Android system tarball to download
        :param data_url: url of the Linaro Android data tarball to download
        :param pkg_url: url of the custom kernel tarball to download
        :param use_cache: whether or not to use the cached copy (if it exists)
        """
        lava_cachedir = self.context.lava_cachedir
        LAVA_IMAGE_TMPDIR = self.context.lava_image_tmpdir
        self.tarball_dir = mkdtemp(dir=LAVA_IMAGE_TMPDIR)
        tarball_dir = self.tarball_dir
        os.chmod(tarball_dir, 0755)
        logging.info("Downloading the image files")

        if use_cache:
            boot_path = download_with_cache(boot_url, tarball_dir, lava_cachedir)
            system_path = download_with_cache(system_url, tarball_dir, lava_cachedir)
            data_path = download_with_cache(data_url, tarball_dir, lava_cachedir)
            if pkg_url:
                pkg_path = download_with_cache(pkg_url, tarball_dir)
            else:
                pkg_path = None
        else:
            boot_path = download(boot_url, tarball_dir)
            system_path = download(system_url, tarball_dir)
            data_path = download(data_url, tarball_dir)
            if pkg_url:
                pkg_path = download(pkg_url, tarball_dir)
            else:
                pkg_path = None
        logging.info("Downloaded the image files")
        return  boot_path, system_path, data_path, pkg_path

    def boot_master_image(self):
        """
        reboot the system, and check that we are in a master shell
        """
        logging.info("Boot the system master image")
        self.proc.soft_reboot()
        try:
            self.proc.expect("Starting kernel")
            self._in_master_shell(300)
        except:
            logging.exception("in_master_shell failed")
            self.proc.hard_reboot()
            self._in_master_shell(300)
        self.proc.sendline('export PS1="$PS1 [rc=$(echo \$?)]: "')
        self.proc.expect(self.master_str, timeout=10, lava_no_logging=1)
        logging.info("System is in master image now")

    def _format_testpartition(self, session, fstype):
        logging.info("Format testboot and testrootfs partitions")
        session.run('umount /dev/disk/by-label/testrootfs', failok=True)
        session.run(
            'mkfs -t %s -q /dev/disk/by-label/testrootfs -L testrootfs'
            % fstype)
        session.run('umount /dev/disk/by-label/testboot', failok=True)
        session.run('mkfs.vfat /dev/disk/by-label/testboot -n testboot')

    def _generate_tarballs(self, image_file):
        """Generate tarballs from a hwpack and rootfs url

        :param hwpack_url: url of the Linaro hwpack to download
        :param rootfs_url: url of the Linaro image to download
        """
        tarball_dir = os.path.dirname(image_file)
        boot_tgz = os.path.join(tarball_dir, "boot.tgz")
        root_tgz = os.path.join(tarball_dir, "root.tgz")
        try:
            _extract_partition(image_file, self.boot_part, boot_tgz)
            _extract_partition(image_file, self.root_part, root_tgz)
        except:
            logging.error("Failed to generate tarballs")
            shutil.rmtree(tarball_dir)
            tb = traceback.format_exc()
            self.sio.write(tb)
            raise
        return boot_tgz, root_tgz

    def reliable_session(self):
        return self._partition_session('testrootfs')

    def retrieve_results(self, result_disk):
        with self._master_session() as session:

            session.run('mkdir -p /mnt/root')
            session.run(
                'mount /dev/disk/by-label/%s /mnt/root' % result_disk)
            # Clean results directory on master image
            session.run(
                'rm -rf /tmp/lava_results.tgz /tmp/%s' % self.context.lava_result_dir)
            session.run('mkdir -p /tmp/%s' % self.context.lava_result_dir)
            session.run(
                'cp /mnt/root/%s/*.bundle /tmp/%s' % (self.context.lava_result_dir,
                    self.context.lava_result_dir))
            # Clean result bundle on test image
            session.run(
                'rm -f /mnt/root/%s/*.bundle' % (self.context.lava_result_dir))
            session.run('umount /mnt/root')

            # Create tarball of all results
            logging.info("Creating lava results tarball")
            session.run('cd /tmp')
            session.run(
                'tar czf /tmp/lava_results.tgz -C /tmp/%s .' % self.context.lava_result_dir)

            # start gather_result job, status
            err_msg = ''
            master_ip = session.get_master_ip()
            if not master_ip:
                err_msg = (err_msg + "Getting master image IP address failed, "
                           "no test case result retrieved.")
                logging.warning(err_msg)
                return 'fail', err_msg, None
            # Set 80 as server port
            session.run('python -m SimpleHTTPServer 80 &> /dev/null &')
            try:
                time.sleep(3)

                result_tarball = "http://%s/lava_results.tgz" % master_ip
                tarball_dir = mkdtemp(dir=self.context.lava_image_tmpdir)
                os.chmod(tarball_dir, 0755)

                # download test result with a retry mechanism
                # set retry timeout to 5 mins
                logging.info("About to download the result tarball to host")
                now = time.time()
                timeout = 300
                tries = 0

                while True:
                    try:
                        result_path = download(
                            result_tarball, tarball_dir,False)
                        return 'pass', '', result_path
                    except RuntimeError:
                        tries += 1
                        if time.time() >= now + timeout:
                            logging.error(
                                "download '%s' failed. Nr tries = %s" % (
                                    result_tarball, tries))
                            return 'fail', err_msg, None
                        else:
                            logging.info(
                                "Sleep one minute and retry (%d)" % tries)
                            time.sleep(60)
            finally:
                session.run('kill %1')
                session.run('')


    @contextlib.contextmanager
    def _partition_session(self, partition):
        """A session that can be used to run commands in a given test
        partition.

        Anything that uses this will have to be done differently for images
        that are not deployed via a master image (e.g. using a JTAG to blow
        the image onto the card or testing under QEMU).
        """
        with self._master_session() as master_session:
            directory = '/mnt/' + partition
            master_session.run('mkdir -p %s' % directory)
            master_session.run('mount /dev/disk/by-label/%s %s' % (partition, directory))
            master_session.run(
                'cp -f %s/etc/resolv.conf %s/etc/resolv.conf.bak' % (
                    directory, directory))
            master_session.run('cp -L /etc/resolv.conf %s/etc' % directory)
            #eliminate warning: Can not write log, openpty() failed
            #                   (/dev/pts not mounted?), does not work
            master_session.run('mount --rbind /dev %s/dev' % directory)
            try:
                yield PrefixCommandRunner(
                    'chroot ' + directory, self.proc, self.master_str)
            finally:
                master_session.run(
                    'cp -f %s/etc/resolv.conf.bak %s/etc/resolv.conf' % (
                        directory, directory))
                cmd = ('cat /proc/mounts | awk \'{print $2}\' | grep "^%s/dev"'
                       '| sort -r | xargs umount' % directory)
                master_session.run(cmd)
                master_session.run('umount ' + directory)

    def _in_master_shell(self, timeout=10):
        """
        Check that we are in a shell on the master image
        """
        self.proc.sendline("")
        match_id = self.proc.expect(
            [self.master_str, pexpect.TIMEOUT], timeout=timeout, lava_no_logging=1)
        if match_id == 1:
            raise OperationFailed
        
    @contextlib.contextmanager
    def _master_session(self):
        """A session that can be used to run commands in the master image.

        Anything that uses this will have to be done differently for images
        that are not deployed via a master image (e.g. using a JTAG to blow
        the image onto the card or testing under QEMU).
        """
        try:
            self._in_master_shell()
        except OperationFailed:
            self.boot_master_image()
        yield MasterCommandRunner(self)
