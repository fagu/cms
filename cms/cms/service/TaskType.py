#!/usr/bin/python
# -*- coding: utf-8 -*-

# Programming contest management system
# Copyright © 2010-2011 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2011 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2011 Matteo Boscariol <boscarim@hotmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import subprocess
import codecs

from cms.async.AsyncLibrary import logger, async_lock
from cms.box.Sandbox import Sandbox
from cms.db.SQLAlchemyAll import Task, Executable, Evaluation
from cms.service import JobException
from cms.service.Utils import get_compilation_command, filter_ansi_escape
from cms import Config

def get_task_type_class(submission, session, service):
    if submission.task.task_type == Task.TASK_TYPE_BATCH:
        return BatchTaskType(submission, session, service)
    else:
        return None

WHITES = " \t\n"

def white_diff_canonicalize(s):
    """Convert the input string to a canonical form for the white diff
    algorithm; that is, the strings a and b are mapped to the same
    string by white_diff_canonicalize() if and only if they have to be
    considered equivalent for the purposes of the white_diff
    algorithm.

    More specificly, this function strips all the leading and trailing
    whitespaces from s and collapse all the runs of consecutive
    whitespaces into just one copy of one specific whitespace."""

    # Replace all the whitespaces with copies of the first, making the
    # rest of the algorithm simpler
    for c in WHITES[1:]:
        s = s.replace(c, WHITES[0])

    # Splits the string according to the first whitespace, filters out
    # empty tokens and join again the string using just one copy of
    # the first whitespace; this way, runs of more than one
    # whitespaces are collapsed into just one copy.
    s = WHITES[0].join(filter(lambda x: x != '', s.split(WHITES[0])))
    return s

def white_diff(output, res):
    """Compare the two input files, ignoring in each line repeated,
    heading or trailing whitespaces and empty traling lines."""

    while True:
        lout = output.readline()
        lres = res.readline()

        # Both files finished: comparison succeded
        if lres == '' and lout == '':
            return True

        # Only one file finished: ok if the other contains only blanks
        elif lres == '' or lout == '':
            lout = lout.strip(WHITES)
            lres = lres.strip(WHITES)
            if lout != '' or lres != '':
                return False

        # Both file still have lines to go: ok if they agree except
        # for the number of whitespaces
        else:
            lout = white_diff_canonicalize(lout)
            lres = white_diff_canonicalize(lres)
            if lout != lres:
                return False

class BatchTaskType:
    def __init__(self, submission, session, service):
        self.submission = submission
        self.session = session
        self.service = service

    def finish_compilation(self, success, compilation_success=False, text=""):
        self.safe_delete_sandbox()
        if not success:
            return False
        if compilation_success:
            self.submission.compilation_outcome = "ok"
        else:
            self.submission.compilation_outcome = "fail"
        try:
            self.submission.compilation_text = text.decode("utf-8")
        except UnicodeDecodeError:
            self.submission.compilation_text("Cannot decode compilation text.")
            with async_lock:
                logger.error("Unable to decode UTF-8 for string %s." % text)
        return True

    def finish_single_execution(self, test_number, success, outcome=0, text=""):
        self.safe_delete_sandbox()
        if not success:
            return False
        self.submission.evaluations[test_number].text = text
        self.submission.evaluations[test_number].outcome = outcome
        return True

    def finish_evaluation(self, success):
        if not success:
            return False
        return True

    def safe_delete_sandbox(self):
        if "sandbox" in self.__dict__ and not Config.keep_sandbox:
            try:
                self.sandbox.delete()
            except (IOError, OSError):
                with async_lock:
                    logger.warning("Couldn't delete sandbox")

    def safe_create_sandbox(self):
        try:
            self.sandbox = Sandbox(self.service)
        except (OSError, IOError), e:
            with async_lock:
                logger.error("Couldn't create sandbox (error: %s)" % repr(e))
            self.safe_delete_sandbox()
            raise JobException()

    def safe_create_file_from_storage(self, name, digest, executable=False):
        try:
            self.sandbox.create_file_from_storage(name, digest, executable)
        except (OSError, IOError):
            with async_lock:
                logger.error("Couldn't copy file `%s' in sandbox" % (name))
            self.safe_delete_sandbox()
            raise JobException()

    def safe_get_file_to_storage(self, name, msg = ""):
        try:
            return self.sandbox.get_file_to_storage(name, msg)
        except (IOError, OSError) as e:
            with async_lock:
                logger.error("Coudln't send file `%s' to storage" % (name))
            self.safe_delete_sandbox()
            raise JobException()

    def safe_get_file_to_string(self, name):
        try:
            return self.sandbox.get_file_to_string(name, maxlen=1024)
        except (IOError, OSError):
            with async_lock:
                logger.error("Couldn't retrieve file `%s' from storage" % (name))
            self.safe_delete_sandbox()
            raise JobException()

    def safe_get_file(self, name):
        try:
            return self.sandbox.get_file(name)
        except (IOError, OSError):
            with async_lock:
                logger.error("Couldn't retrieve file `%s' from storage" % (name))
            self.safe_delete_sandbox()
            raise JobException()

    def safe_sandbox_execute(self, command):
        try:
            self.sandbox.execute(command)
        except (OSError, IOError) as e:
            with async_lock:
                logger.error("Couldn't spawn `%s' (exception %s)" % (command[0], repr(e)))
            self.safe_delete_sandbox()
            raise JobException()

    def compile(self):
        """Tries to compile the specified submission.

        It returns True when the compilation is successful or when the
        submission cannot be compiled successfully, and False when the
        compilation fails because of environment problems (trying
        again to compile the same submission in a sane environment
        should lead to returning True).
        """

        # Detect the submission's language and check that it contains
        # exactly one source file
        valid, language = self.submission.verify_source(self.session)
        if not valid or language == None:
            with async_lock:
                logger.info("Invalid submission or couldn't detect language")
            return self.finish_compilation(True, False, "Invalid files in submission")

        if len(self.submission.files) != 1:
            with async_lock:
                logger.info("Submission contains %d files, expecting 1" % (len(self.submission.files)))
            return self.finish_compilation(True, False, "Invalid files in submission")

        source_filename = self.submission.files.keys()[0]
        executable_filename = source_filename.replace(".%s" % (language), "")

        # Setup the compilation environment
        self.safe_create_sandbox()
        self.safe_create_file_from_storage(
            source_filename,
            self.submission.files[source_filename].digest)

        command = get_compilation_command(language,
                                          source_filename,
                                          executable_filename)

        # Execute the compilation inside the sandbox
        self.sandbox.chdir = self.sandbox.path
        self.sandbox.preserve_env = True
        self.sandbox.filter_syscalls = 0
        self.sandbox.allow_fork = True
        self.sandbox.file_check = 2
        # FIXME - File access limits are not enforced on children
        # processes (like ld)
        self.sandbox.set_env['TMPDIR'] = self.sandbox.path
        self.sandbox.allow_path = ['/etc/', '/lib/', '/usr/', '%s/' % (self.sandbox.path)]
        self.sandbox.timeout = 8
        self.sandbox.wallclock_timeout = 10
        self.sandbox.address_space = 256 * 1024
        self.sandbox.stdout_file = self.sandbox.relative_path("compiler_stdout.txt")
        self.sandbox.stderr_file = self.sandbox.relative_path("compiler_stderr.txt")
        with async_lock:
            logger.info("Starting compilation")
        self.safe_sandbox_execute(command)

        # Detect the outcome of the compilation
        exit_status = self.sandbox.get_exit_status()
        exit_code = self.sandbox.get_exit_code()
        stdout = self.safe_get_file_to_string("compiler_stdout.txt")
        if stdout.strip() == "":
            stdout = "(empty)\n"
        stderr = self.safe_get_file_to_string("compiler_stderr.txt")
        if stderr.strip() == "":
            stderr = "(empty)\n"

        # Execution finished successfully: the submission was
        # correctly compiled
        if exit_status == Sandbox.EXIT_OK and exit_code == 0:
            digest = self.safe_get_file_to_storage(
                executable_filename,
                "Executable %s for submission %s" % \
                (executable_filename, self.submission.id))

            self.session.add(Executable(digest,
                                        executable_filename,
                                        self.submission))

            with async_lock:
                logger.info("Compilation successfully finished")
            return self.finish_compilation(True, True, "OK %s\nCompiler standard output:\n%s\nCompiler standard error:\n%s" % (self.sandbox.get_stats(), stdout, stderr))

        # Error in compilation: returning the error to the user
        if exit_status == Sandbox.EXIT_OK and exit_code != 0:
            with async_lock:
                logger.info("Compilation failed")
            return self.finish_compilation(True, False, "Failed %s\nCompiler standard output:\n%s\nCompiler standard error:\n%s" % (self.sandbox.get_stats(), stdout, stderr))

        # Timeout: returning the error to the user
        if exit_status == Sandbox.EXIT_TIMEOUT:
            with async_lock:
                logger.info("Compilation timed out")
            return self.finish_compilation(True, False, "Time out %s\nCompiler standard output:\n%s\nCompiler standard error:\n%s" % (self.sandbox.get_stats(), stdout, stderr))

        # Suicide with signal (probably memory limit): returning the
        # error to the user
        if exit_status == Sandbox.EXIT_SIGNAL:
            signal = self.sandbox.get_killing_signal()
            with async_lock:
                logger.info("Compilation killed with signal %d" % (signal))
            return self.finish_compilation(True, False, "Killed with signal %d %s\nThis could be triggered by violating memory limits\nCompiler standard output:\n%s\nCompiler standard error:\n%s" % (signal, self.sandbox.get_stats(), stdout, stderr))

        # Sandbox error: this isn't a user error, the administrator
        # needs to check the environment
        if exit_status == Sandbox.EXIT_SANDBOX_ERROR:
            with async_lock:
                logger.error("Compilation aborted because of sandbox error")
            return self.finish_compilation(False)

        # Forbidden syscall: this shouldn't happen, probably the
        # administrator should relax the syscall constraints
        if exit_status == Sandbox.EXIT_SYSCALL:
            with async_lock:
                logger.error("Compilation aborted because of forbidden syscall")
            return self.finish_compilation(False)

        # Forbidden file access: this could be triggered by the user
        # including a forbidden file or too strict sandbox contraints;
        # the administrator should have a look at it
        if exit_status == Sandbox.EXIT_FILE_ACCESS:
            with async_lock:
                logger.error("Compilation aborted because of forbidden file access")
            return self.finish_compilation(False)

        # Why the exit status hasn't been captured before?
        with async_lock:
            logger.error("Shouldn't arrive here, failing")
        return self.finish_compilation(False)

    def execute_single(self, test_number):
        self.safe_create_sandbox()
        self.safe_create_file_from_storage(self.executable_filename, self.submission.executables[self.executable_filename].digest, executable = True)
        self.safe_create_file_from_storage("input.txt", self.submission.task.testcases[test_number].input)

        self.sandbox.chdir = self.sandbox.path
        self.sandbox.filter_syscalls = 2
        self.sandbox.timeout = self.submission.task.time_limit
        self.sandbox.address_space = self.submission.task.memory_limit * 1024
        self.sandbox.file_check = 1
        self.sandbox.allow_path = ["input.txt", "output.txt"]
        stdout_filename = os.path.join(self.sandbox.path, "submission_stdout.txt")
        stderr_filename = os.path.join(self.sandbox.path, "submission_stderr.txt")
        self.sandbox.stdout_file = stdout_filename
        self.sandbox.stderr_file = stderr_filename

        # These syscalls and path are used by executables generated by fpc
        self.sandbox.allow_path += ["/proc/self/exe"]
        self.sandbox.allow_syscall += ["getrlimit", "rt_sigaction"]

        # This one seems to be used for a C++ executable.
        self.sandbox.allow_path += ["/proc/meminfo"]

        self.safe_sandbox_execute([self.sandbox.relative_path(self.executable_filename)])

        # Detect the outcome of the execution
        exit_status = self.sandbox.get_exit_status()
        exit_code = self.sandbox.get_exit_code()

        # Timeout: returning the error to the user
        if exit_status == Sandbox.EXIT_TIMEOUT:
            with async_lock:
                logger.info("Execution timed out")
            return self.finish_single_execution(test_number, True, 0.0, "Execution timed out\n")

        # Suicide with signal (memory limit, segfault, abort):
        # returning the error to the user
        if exit_status == Sandbox.EXIT_SIGNAL:
            signal = self.sandbox.get_killing_signal()
            with async_lock:
                logger.info("Execution killed with signal %d" % (signal))
            return self.finish_single_execution(test_number, True, 0.0, "Execution killed with signal %d\n" % (signal))

        # Sandbox error: this isn't a user error, the administrator
        # needs to check the environment
        if exit_status == Sandbox.EXIT_SANDBOX_ERROR:
            with async_lock:
                logger.error("Evaluation aborted because of sandbox error")
            return self.finish_single_execution(test_number, False)

        # Forbidden syscall: returning the error to the user
        # FIXME - Tell which syscall raised this error
        if exit_status == Sandbox.EXIT_SYSCALL:
            with async_lock:
                logger.info("Execution killed because of forbidden syscall")
            return self.finish_single_execution(test_number, True, 0.0, "Execution killed because of forbidden syscall")

        # Forbidden file access: returning the error to the user
        # FIXME - Tell which file raised this error
        if exit_status == Sandbox.EXIT_FILE_ACCESS:
            with async_lock:
                logger.info("Execution killed because of forbidden file access")
            return self.finish_single_execution(test_number, True, 0.0, "Execution killed because of forbidden file access")

        # Last check before assuming that execution finished
        # successfully; we accept the execution even if the exit code
        # isn't 0
        if exit_status != Sandbox.EXIT_OK:
            with async_lock:
                logger.error("Shouldn't arrive here, failing")
            return self.finish_single_execution(test_number, False)

        if not self.sandbox.file_exists("output.txt"):
            outcome = 0.0
            text = "Execution didn't produce file output.txt"
            return self.finish_single_execution(test_number, True, outcome, text)

        self.safe_create_file_from_storage("res.txt", self.submission.task.testcases[test_number].output)
        self.sandbox.filter_syscalls = 2
        self.sandbox.timeout = 0
        self.sandbox.address_space = None
        self.sandbox.file_check = 1
        self.sandbox.allow_path = ["input.txt", "output.txt", "res.txt"]
        stdout_filename = os.path.join(self.sandbox.path, "manager_stdout.txt")
        stderr_filename = os.path.join(self.sandbox.path, "manager_stderr.txt")
        self.sandbox.stdout_file = stdout_filename
        self.sandbox.stderr_file = stderr_filename

        # No manager: I'll do a white_diff between output.txt and res.txt
        if len(self.submission.task.managers) == 0:
            out_file = self.safe_get_file("output.txt")
            res_file = self.safe_get_file("res.txt")
            if white_diff(out_file, res_file):
                outcome = 1.0
                text = "Output file is correct"
            else:
                outcome = 0.0
                text = "Output file isn't correct"

        # Manager present: wonderful, he'll do all the job
        else:
            manager_filename = self.submission.task.managers.keys()[0]
            self.safe_create_file_from_storage(manager_filename, self.submission.task.managers[manager_filename].digest, executable = True)
            manager_popen = self.safe_sandbox_execute(["./%s" % (manager_filename), "input.txt", "res.txt", "output.txt"])
            with codecs.open(stdout_filename, "r", "utf-8") as stdout_file:
                with codecs.open(stderr_filename, "r", "utf-8") as stderr_file:
                    try:
                        outcome = stdout_file.readline().strip()
                    except UnicodeDecodeError as e:
                        with async_lock:
                            logger.error("Unable to interpret manager stdout " +
                                         "(outcome) as unicode. %s" % repr(e))
                        return self.finish_single_execution(test_number, False)
                    try:
                        text = filter_ansi_escape(stderr_file.readline())
                    except UnicodeDecodeError as e:
                        with async_lock:
                            logger.error("Unable to interpret manager stderr " +
                                         "(text) as unicode. %s" % repr(e))
                        return self.finish_single_execution(test_number, False)
            try:
                outcome = float(outcome)
            except ValueError:
                with async_lock:
                    logger.error("Wrong outcome `%s' from manager" % (outcome))
                return self.finish_single_execution(test_number, False)

        # Finally returns the result
        return self.finish_single_execution(test_number, True, outcome, text)

    def execute(self):
        if len(self.submission.executables) != 1:
            with async_lock:
                logger.info("Submission contains %d executables, expecting 1" % (len(self.submission.executables)))
            return self.finish_evaluation(False)

        self.executable_filename = self.submission.executables.keys()[0]

        for test_number in xrange(len(self.submission.evaluations), len(self.submission.task.testcases)):
                self.session.add(Evaluation(text=None, outcome=None, num=test_number, submission=self.submission))

        for test_number in xrange(len(self.submission.task.testcases)):
            success = self.execute_single(test_number)
            if not success:
                return self.finish_evaluation(False)
        return self.finish_evaluation(True)
