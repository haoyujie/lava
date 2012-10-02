#!/usr/bin/python

# Copyright (C) 2012 Linaro Limited
#
# Author: Andy Doan <andy.doan@linaro.org>
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

import json
import logging
import os
import pexpect
import shutil

import lava_dispatcher.utils as utils

from lava_dispatcher.actions import BaseAction
from lava_dispatcher.device.target import Target
from lava_dispatcher.downloader import download_image

LAVA_TEST_DIR = '%s/../../lava_test_shell' % os.path.dirname(__file__)
LAVA_TEST_ANDROID = '%s/lava-test-runner-android' % LAVA_TEST_DIR
LAVA_TEST_UBUNTU = '%s/lava-test-runner-ubuntu' % LAVA_TEST_DIR
LAVA_TEST_UPSTART = '%s/lava-test-runner.conf' % LAVA_TEST_DIR
LAVA_TEST_SHELL = '%s/lava-test-shell' % LAVA_TEST_DIR

Target.android_deployment_data['lava_test_runner'] = LAVA_TEST_ANDROID
Target.android_deployment_data['lava_test_shell'] = LAVA_TEST_SHELL
Target.ubuntu_deployment_data['lava_test_runner'] = LAVA_TEST_UBUNTU
Target.ubuntu_deployment_data['lava_test_shell'] = LAVA_TEST_SHELL


def _configure_ubuntu_startup(etcdir):
    logging.info('adding ubuntu upstart job')
    shutil.copy(LAVA_TEST_UPSTART, '%s/init/' % etcdir)

Target.ubuntu_deployment_data['lava_test_configure_startup'] = \
        _configure_ubuntu_startup

def _configure_android_startup(etcdir):
    logging.info('hacking android start up job')
    with open('%s/mkshrc' % etcdir, 'a') as f:
        f.write('\n/system/lava/bin/lava-test-runner\n')

Target.android_deployment_data['lava_test_configure_startup'] = \
        _configure_android_startup

class cmd_lava_test_shell(BaseAction):

    parameters_schema = {
        'type': 'object',
        'properties': {
            'testdef_url': {'type': 'string'},
            'timeout': {'type': 'integer', 'optional': True},
            },
        'additionalProperties': False,
        }

    def run(self, testdef_url, timeout=-1):
        target = self.client.target_device
        self._assert_target(target)

        testdef = self._get_test_definition(testdef_url, target.scratch_dir)
        self._configure_target(target, testdef)

        with target.runner() as runner:
            patterns = [
                '<LAVA_TEST_RUNNER>: exiting',
                pexpect.EOF,
                pexpect.TIMEOUT,
                ]
            idx = runner._connection.expect(patterns, timeout=timeout)
            if idx == 0:
                logging.info('lava_test_shell seems to have completed')
            elif idx == 1:
                logging.warn('lava_test_shell connection dropped')
            elif idx == 2:
                logging.warn('lava_test_shell has timed out')

    def _get_test_definition(self, testdef_url, tmpdir):
        testdef_file = download_image(testdef_url, self.context, tmpdir)
        with open(testdef_file, 'r') as f:
            logging.info('loading test definition')
            return json.load(f)

    def _copy_runner(self, mntdir, target):
        runner = target.deployment_data['lava_test_runner']
        shell = target.deployment_data['lava_test_shell']
        shutil.copy(runner, '%s/bin/lava-test-runner' % mntdir)
        shutil.copy(shell, '%s/bin/lava-test-shell'% mntdir)

    def _copy_test(self, mntdir, testdef):
        tdir = '%s/tests/%s' % (mntdir, testdef['test_id'])
        utils.ensure_directory(tdir)
        with open('%s/testdef.json' % tdir, 'w') as f:
            f.write(json.dumps(testdef))

    def _mk_runner_dirs(self, mntdir):
        utils.ensure_directory('%s/bin' % mntdir)
        utils.ensure_directory('%s/tests' % mntdir)

    def _configure_target(self, target, testdef):
        with target.file_system(target.config.root_part, 'lava') as d:
            self._mk_runner_dirs(d)
            self._copy_runner(d, target)
            self._copy_test(d, testdef)

        with target.file_system(target.config.root_part, 'etc') as d:
            target.deployment_data['lava_test_configure_startup'](d)

    def _assert_target(self, target):
        ''' Ensure the target has is set up properly
        '''
        if not target.deployment_data:
            raise RuntimeError('Target includes no deployment_data')

        keys = ['lava_test_runner', 'lava_test_shell',
            'lava_test_configure_startup']
        for k in keys:
            if k not in target.deployment_data:
                raise RuntimeError('Target deployment_data missing %s' % k)
