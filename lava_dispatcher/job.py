# Copyright (C) 2014 Linaro Limited
#
# Author: Neil Williams <neil.williams@linaro.org>
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

import atexit
import errno
import shutil
import tempfile
import datetime
import time
import pytz
import traceback
import os
from lava_common.exceptions import (
    LAVABug,
    LAVAError,
    JobError,
)
from lava_dispatcher.logical import PipelineContext
from lava_dispatcher.diagnostics import DiagnoseNetwork
from lava_dispatcher.protocols.multinode import MultinodeProtocol  # pylint: disable=unused-import
from lava_common.constants import DISPATCHER_DOWNLOAD_DIR
from lava_dispatcher.utils.filesystem import debian_package_version


class ZMQConfig(object):
    """
    Namespace for the ZMQ logging configuration
    """
    def __init__(self, logging_url, master_cert, slave_cert, ipv6):
        self.logging_url = logging_url
        self.master_cert = master_cert
        self.slave_cert = slave_cert
        self.ipv6 = ipv6


class Job(object):  # pylint: disable=too-many-instance-attributes
    """
    Populated by the parser, the Job contains all of the
    Actions and their pipelines.
    parameters provides the immutable data about this job:
        action_timeout
        job_name
        priority
        device_type (mapped to target by scheduler)
        yaml_line
        logging_level
        job_timeout
    Job also provides the primary access to the Device.
    The NewDevice class only loads the specific configuration of the
    device for this job - one job, one device.
    """

    def __init__(self, job_id, parameters, logger):  # pylint: disable=too-many-arguments
        self.job_id = job_id
        self.logger = logger
        self.device = None
        self.parameters = parameters
        self.__context__ = PipelineContext()
        self.pipeline = None
        self.connection = None
        self.triggers = []  # actions can add trigger strings to the run a diagnostic
        self.diagnostics = [
            DiagnoseNetwork,
        ]
        self.timeout = None
        self.protocols = []
        self.compatibility = 2
        # Was the job cleaned
        self.cleaned = False
        # Root directory for the job tempfiles
        self.tmp_dir = None
        # override in use
        self.base_overrides = {}
        self.started = False

    @property
    def context(self):
        return self.__context__.pipeline_data

    @context.setter
    def context(self, data):
        self.__context__.pipeline_data.update(data)

    def diagnose(self, trigger):
        """
        Looks up the class to execute to diagnose the problem described by the
         specified trigger.
        """
        trigger_tuples = [(cls.trigger(), cls) for cls in self.diagnostics]
        for diagnostic in trigger_tuples:
            if trigger is diagnostic[0]:
                return diagnostic[1]()
        return None

    def describe(self):
        return {'device': self.device,
                'job': self.parameters,
                'compatibility': self.compatibility,
                'pipeline': self.pipeline.describe()}

    def mkdtemp(self, action_name, override=None):
        """
        Create a tmp directory in DISPATCHER_DOWNLOAD_DIR/{job_id}/ because
        this directory will be removed when the job finished, making cleanup
        easier.
        """
        if override is None:
            if self.tmp_dir is None:
                create_base_dir = True
                base_dir = DISPATCHER_DOWNLOAD_DIR
            else:
                create_base_dir = False
                base_dir = self.tmp_dir
        else:
            if override in self.base_overrides:
                create_base_dir = False
                base_dir = self.base_overrides[override]
            else:
                create_base_dir = True
                base_dir = override

        if create_base_dir:
            # Try to create the directory.
            base_dir = os.path.join(base_dir, str(self.job_id))
            try:
                os.makedirs(base_dir, mode=0o755)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    # When running unit tests
                    base_dir = tempfile.mkdtemp(prefix='pipeline-')
                    atexit.register(shutil.rmtree, base_dir, ignore_errors=True)
            # Save the path for the next calls (only if that's not an override)
            if override is None:
                self.tmp_dir = base_dir
            else:
                self.base_overrides[override] = base_dir

        # Create the sub-directory
        tmp_dir = tempfile.mkdtemp(prefix=action_name + '-', dir=base_dir)
        os.chmod(tmp_dir, 0o755)
        return tmp_dir

    def _validate(self):
        """
        Validate the pipeline and raise an exception (that inherit from
        LAVAError) if it fails.
        """
        self.logger.info("Start time: %s (UTC)", pytz.utc.localize(datetime.datetime.utcnow()))
        for protocol in self.protocols:
            try:
                protocol.configure(self.device, self)
            except LAVAError:
                self.logger.error("Configuration failed for protocol %s", protocol.name)
                raise
            except Exception as exc:
                self.logger.error("Configuration failed for protocol %s", protocol.name)
                self.logger.exception(traceback.format_exc())
                raise LAVABug(exc)

            if not protocol.valid:
                msg = "protocol %s has errors: %s" % (protocol.name, protocol.errors)
                self.logger.exception(msg)
                raise JobError(msg)

        # Check that namespaces are used in all actions or none
        namespaces = set()
        for action in self.parameters["actions"]:
            action_name = list(action.keys())[0]
            namespaces.add(action[action_name]["namespace"])

        # 'common' is a reserved namespace that should not be present with
        # other namespaces.
        if len(namespaces) > 1 and 'common' in namespaces:
            msg = "'common' is a reserved namespace that should not be present with other namespaces"
            self.logger.error(msg)
            self.logger.debug("Namespaces: %s", ", ".join(namespaces))
            raise JobError(msg)

        # validate the pipeline
        self.pipeline.validate_actions()

    def validate(self):
        """
        Public wrapper for the pipeline validation.
        Send a "fail" results if needed.
        """
        label = "lava-dispatcher, installed at version: %s" % debian_package_version(split=False)
        self.logger.info(label)
        self.logger.info("start: 0 validate")
        start = time.time()

        success = False
        try:
            self._validate()
        except LAVAError as exc:
            raise
        except Exception as exc:
            # provide useful info on command line, e.g. failed unit tests.
            self.logger.exception(traceback.format_exc())
            raise LAVABug(exc)
        else:
            success = True
        finally:
            if not success:
                self.cleanup(connection=None)
            self.logger.info("validate duration: %.02f", time.time() - start)
            self.logger.results({"definition": "lava",
                                 "case": "validate",
                                 "result": "pass" if success else "fail"})

    def _run(self):
        """
        Run the pipeline under the run() wrapper that will catch the exceptions
        """
        self.started = True

        # Setup the protocols
        for protocol in self.protocols:
            try:
                protocol.set_up()
            except LAVAError:
                raise
            except Exception as exc:
                self.logger.error("Unable to setup the protocols")
                self.logger.exception(traceback.format_exc())
                raise LAVABug(exc)

            if not protocol.valid:
                msg = "protocol %s has errors: %s" % (protocol.name, protocol.errors)
                self.logger.exception(msg)
                raise JobError(msg)

        # Run the pipeline and wait for exceptions
        with self.timeout() as max_end_time:
            self.pipeline.run_actions(self.connection, max_end_time)

    def run(self):
        """
        Top level routine for the entire life of the Job, using the job level timeout.
        Python only supports one alarm on SIGALRM - any Action without a connection
        will have a default timeout which will use SIGALRM. So the overarching Job timeout
        can only stop processing actions if the job wide timeout is exceeded.
        """
        try:
            self._run()
        finally:
            # Cleanup now
            self.cleanup(self.connection)

    def cleanup(self, connection):
        if self.cleaned:
            self.logger.info("Cleanup already called, skipping")
            return

        # exit out of the pipeline & run the Finalize action to close the
        # connection and poweroff the device (the cleanup action will do that
        # for us)
        self.logger.info("Cleaning after the job")
        self.pipeline.cleanup(connection)

        for tmp_dir in self.base_overrides.values():
            self.logger.info("Override tmp directory removed at %s", tmp_dir)
            try:
                shutil.rmtree(tmp_dir)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    self.logger.error("Unable to remove the directory: %s",
                                      exc.strerror)

        if self.tmp_dir is not None:
            self.logger.info("Root tmp directory removed at %s", self.tmp_dir)
            try:
                shutil.rmtree(self.tmp_dir)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    self.logger.error("Unable to remove the directory: %s",
                                      exc.strerror)

        # Mark cleanup as done to avoid calling it many times
        self.cleaned = True