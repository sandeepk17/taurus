#! /usr/bin/env python
"""
Copyright 2015 BlazeMeter Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from io import TextIOBase
import logging
import os
import platform
import subprocess
from subprocess import CalledProcessError

from bzt.utils import shutdown_process
from bzt.engine import EngineModule
from bzt import AutomatedShutdown
from bzt.utils import ensure_is_dict


class ShellExecutor(EngineModule):
    def __init__(self):
        super(ShellExecutor, self).__init__()
        self.prepare_tasks = []
        self.startup_tasks = []
        self.check_tasks = []
        self.shutdown_tasks = []
        self.postprocess_tasks = []

    def _load_tasks(self, stage, container):
        if not isinstance(self.parameters.get(stage, []), list):
            self.parameters[stage] = [self.parameters[stage]]

        for index, stage_task in enumerate(self.parameters[stage]):
            stage_task = ensure_is_dict(self.parameters[stage], index, "command")
            container.append(Task(self.parameters[stage][index], self.log, self.engine.artifacts_dir))
            self.log.debug("Added task: %s, stage: %s", stage_task, stage)

    def prepare(self):
        """
        Configure Tasks
        :return:
        """
        self._load_tasks('prepare', self.prepare_tasks)
        self._load_tasks('startup', self.startup_tasks)
        self._load_tasks('check', self.check_tasks)
        self._load_tasks('shutdown', self.shutdown_tasks)
        self._load_tasks('post-process', self.postprocess_tasks)

        for task in self.prepare_tasks:
            task.start()

    def startup(self):
        for task in self.startup_tasks:
            task.start()

    def check(self):
        for task in self.check_tasks:
            task.start()

        for task in self.prepare_tasks + self.startup_tasks + self.check_tasks:
            task.check()

        return super(ShellExecutor, self).check()

    def shutdown(self):
        for task in self.check_tasks + self.startup_tasks:
            task.shutdown()

        for task in self.shutdown_tasks:
            task.start()

    def post_process(self):
        for task in self.shutdown_tasks + self.check_tasks + self.startup_tasks + self.prepare_tasks:
            task.shutdown()

        for task in self.postprocess_tasks:
            task.start()
            task.shutdown()


class Task(object):
    def __init__(self, config, parent_log, working_dir):
        self.log = parent_log.getChild(self.__class__.__name__)
        self.working_dir = working_dir
        self.command = config.get("command", ValueError("Parameter is required: command"))
        self.is_background = config.get("background", False)
        self.ignore_failure = config.get("ignore-failure", True)
        self.err = config.get("out", LoggingFileOutput(self.log, logging.DEBUG))
        self.out = config.get("err", LoggingFileOutput(self.log, logging.INFO))
        self.process = None
        self.ret_code = None

    def start(self):
        """
        Start task
        :return:
        """
        if self.process:
            self.check()
            self.log.info("Process still running: %s", self)
            return

        kwargs = {
            'args': self.command,
            'stdout': self.out,
            'stderr': self.err,
            'cwd': self.working_dir,
            'shell': True
        }
        if platform.system() != 'Windows':
            kwargs['preexec_fn'] = os.setpgrp
            kwargs['close_fds'] = True

        self.log.debug("Starting task: %s", self)
        if self.is_background:
            self.process = subprocess.Popen(**kwargs)
            self.log.debug("Task started, PID: %d", self.process.pid)
        elif self.ignore_failure:
            rc = subprocess.call(**kwargs)
            self.log.debug("Command finished, exit code is %s", rc)
        else:
            subprocess.check_call(**kwargs)

    def check(self):
        if self.ret_code is not None or not self.is_background:
            return  # all is done before or it's non-bg job

        self.ret_code = self.process.poll()
        if self.ret_code is not None:
            self.log.debug("Task: %s was finished with exit code: %s", self, self.ret_code)
            if not self.ignore_failure:
                raise CalledProcessError(self.ret_code, self)
            return True
        self.log.debug('Task: %s is not finished yet', self)
        return False

    def shutdown(self):
        """
        If task was not completed, kill process, provide output
        else provide output
        :return:
        """
        self.check()

        if not self.check():
            self.log.info("Background task %s was not completed, shutting it down", self)
            shutdown_process(self.process, self.log)
        self.process = None

        if self.ret_code and not self.ignore_failure:
            self.log.error("Task %s failed with ignore-failure = False, terminating", self)
            raise AutomatedShutdown

    def __repr__(self):
        return self.command


class LoggingFileOutput(TextIOBase):
    def __init__(self, log, level):
        """
        :type log: logging.Logger
        """
        super(LoggingFileOutput, self).__init__()
        self.log = log
        self.level = level

    def write(self, *args, **kwargs):
        self.log.log(self.level, "%s %s", args, kwargs)

    def fileno(self, *args, **kwargs):
        return 0
