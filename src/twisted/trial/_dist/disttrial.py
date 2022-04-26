# -*- test-case-name: twisted.trial._dist.test.test_disttrial -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
This module contains the trial distributed runner, the management class
responsible for coordinating all of trial's behavior at the highest level.

@since: 12.3
"""

import os
import sys
from functools import partial
from typing import (
    Iterable,
    List,
    Optional,
    Sequence,
    TextIO,
    Union,
    cast,
)
from unittest import TestSuite

from attrs import define

from twisted.internet.defer import Deferred
from twisted.internet.interfaces import IReactorCore, IReactorProcess
from twisted.logger import Logger
from twisted.python.failure import Failure
from twisted.python.filepath import FilePath
from twisted.python.lockfile import FilesystemLock
from twisted.python.modules import theSystemPath
from .._asyncrunner import _iterateTests
from ..itrial import IReporter, ITestCase
from ..reporter import UncleanWarningsReporterWrapper
from ..runner import TestHolder
from ..util import _unusedTestDirectory, openTestLog
from . import _WORKER_AMP_STDIN, _WORKER_AMP_STDOUT
from .distreporter import DistReporter
from .functional import countingCalls, fromOptional, iterateWhile, parallel, void
from .worker import WorkerAction, LocalWorker, LocalWorkerAMP


class IDistTrialReactor(IReactorCore, IReactorProcess):
    """
    The reactor interfaces required by disttrial.
    """


@define
class WorkerPoolConfig:
    """
    Configuration parameters for a pool of test-running workers.

    @ivar numWorkers: The number of workers in the pool.

    @ivar workingDirectory: A directory in which working directories for each
        of the workers will be created.

    @ivar workerArguments: Extra arguments to pass the worker process in its
        argv.

    @ivar logFile: The basename of the overall test log file.
    """

    numWorkers: int
    workingDirectory: FilePath
    workerArguments: List[str]
    logFile: str


@define
class StartedWorkerPool:
    """
    A pool of workers which have already been started.

    @ivar workingDirectory: A directory holding the working directories for
        each of the workers.

    @ivar testDirLock: An object representing the cooperative lock this pool
        holds on its working directory.

    @ivar testLog: The open overall test log file.

    @ivar workers: Objects corresponding to the worker child processes and
        adapting between process-related interfaces and C{IProtocol}.

    @ivar ampWorkers: AMP protocol instances corresponding to the worker child
        processes.
    """

    workingDirectory: FilePath
    testDirLock: FilesystemLock
    testLog: TextIO
    workers: List[LocalWorker]
    ampWorkers: List[LocalWorkerAMP]

    _logger = Logger()

    async def run(self, workerAction: WorkerAction) -> None:
        """
        Run an action on all of the workers in the pool.
        """
        await parallel(void(workerAction(worker)) for worker in self.ampWorkers)
        return None

    async def join(self) -> None:
        """
        Shut down all of the workers in the pool.

        The pool is unusable after this method is called.
        """
        for worker in self.workers:
            try:
                await worker.exit()
            except Exception:
                self._logger.failure("joining disttrial worker failed")

        del self.workers[:]
        del self.ampWorkers[:]
        self.testLog.close()
        self.testDirLock.unlock()


@define
class WorkerPool:
    """
    Manage a fixed-size collection of child processes which can run tests.

    @ivar _config: Configuration for the precise way in which the pool is run.
    """

    _config: WorkerPoolConfig

    def createLocalWorkers(
        self,
        protocols: Iterable[LocalWorkerAMP],
        workingDirectory: FilePath,
        logFile: TextIO,
    ) -> List[LocalWorker]:
        """
        Create local worker protocol instances and return them.

        @param protocols: The process/protocol adapters to use for the created
        workers.

        @param workingDirectory: The base path in which we should run the
            workers.

        @param logFile: The test log, for workers to write to.

        @return: A list of C{quantity} C{LocalWorker} instances.
        """
        return [
            LocalWorker(protocol, workingDirectory.child(str(x)), logFile)
            for x, protocol in enumerate(protocols)
        ]

    def _launchWorkerProcesses(self, spawner, protocols, arguments):
        """
        Spawn processes from a list of process protocols.

        @param spawner: A C{IReactorProcess.spawnProcess} implementation.

        @param protocols: An iterable of C{ProcessProtocol} instances.

        @param arguments: Extra arguments passed to the processes.
        """
        workertrialPath = theSystemPath["twisted.trial._dist.workertrial"].filePath.path
        childFDs = {
            0: "w",
            1: "r",
            2: "r",
            _WORKER_AMP_STDIN: "w",
            _WORKER_AMP_STDOUT: "r",
        }
        environ = os.environ.copy()
        # Add an environment variable containing the raw sys.path, to be used
        # by subprocesses to try to make it identical to the parent's.
        environ["PYTHONPATH"] = os.pathsep.join(sys.path)
        for worker in protocols:
            args = [sys.executable, workertrialPath]
            args.extend(arguments)
            spawner(worker, sys.executable, args=args, childFDs=childFDs, env=environ)

    async def start(self, reactor: IReactorProcess) -> StartedWorkerPool:
        """
        Launch all of the workers for this pool.

        @return: A started pool object that can run jobs using the workers.
        """
        testDir, testDirLock = _unusedTestDirectory(
            self._config.workingDirectory,
        )

        # Open a log file in the chosen working directory (not necessarily the
        # same as our configured working directory, if that path was in use).
        testLog = openTestLog(testDir.child(self._config.logFile))

        ampWorkers = [LocalWorkerAMP() for x in range(self._config.numWorkers)]
        workers = self.createLocalWorkers(
            ampWorkers,
            testDir,
            testLog,
        )
        self._launchWorkerProcesses(
            reactor.spawnProcess,
            workers,
            self._config.workerArguments,
        )

        return StartedWorkerPool(
            testDir,
            testDirLock,
            testLog,
            workers,
            ampWorkers,
        )


def shouldContinue(untilFailure: bool, result: IReporter) -> bool:
    """
    Determine whether the test suite should be iterated again.

    @param untilFailure: C{True} if the suite is supposed to run until
        failure.

    @param result: The test result of the test suite iteration which just
        completed.
    """
    return untilFailure and result.wasSuccessful()


class DistTrialRunner:
    """
    A specialized runner for distributed trial. The runner launches a number of
    local worker processes which will run tests.

    @ivar _maxWorkers: the number of workers to be spawned.
    @type _maxWorkers: C{int}

    @ivar _stream: stream which the reporter will use.

    @ivar _reporterFactory: the reporter class to be used.
    """

    _distReporterFactory = DistReporter
    _logger = Logger()

    def _makeResult(self) -> DistReporter:
        """
        Make reporter factory, and wrap it with a L{DistReporter}.
        """
        reporter = self._reporterFactory(
            self._stream, self._tbformat, realtime=self._rterrors
        )
        if self._uncleanWarnings:
            reporter = UncleanWarningsReporterWrapper(reporter)
        return self._distReporterFactory(reporter)

    def __init__(
        self,
        reporterFactory,
        maxWorkers,
        workerArguments,
        stream=None,
        tracebackFormat="default",
        realTimeErrors=False,
        uncleanWarnings=False,
        logfile="test.log",
        workingDirectory="_trial_temp",
        workerPoolFactory=WorkerPool,
    ):
        self._maxWorkers = maxWorkers
        self._workerArguments = workerArguments
        self._reporterFactory = reporterFactory
        if stream is None:
            stream = sys.stdout
        self._stream = stream
        self._tbformat = tracebackFormat
        self._rterrors = realTimeErrors
        self._uncleanWarnings = uncleanWarnings
        self._result = None
        self._workingDirectory = workingDirectory
        self._logFile = logfile
        self._logFileObserver = None
        self._logFileObject = None
        self._logWarnings = False
        self._workerPoolFactory = workerPoolFactory

    def writeResults(self, result):
        """
        Write test run final outcome to result.

        @param result: A C{TestResult} which will print errors and the summary.
        """
        result.done()

    async def _driveWorker(
        self,
        result: DistReporter,
        testCases: Sequence[ITestCase],
        worker: LocalWorkerAMP,
    ) -> None:
        """
        Drive a L{LocalWorkerAMP} instance, iterating the tests and calling
        C{run} for every one of them.

        @param worker: The L{LocalWorkerAMP} to drive.

        @param result: The global L{DistReporter} instance.

        @param testCases: The global list of tests to iterate.

        @return: A coroutine that completes after all of the tests have
            completed.
        """

        async def task(case):
            try:
                await worker.run(case, result)
            except Exception:
                result.original.addFailure(case, Failure())

        for case in testCases:
            await task(case)

    async def runAsync(
        self,
        suite: TestSuite,
        reactor: IReactorProcess,
        untilFailure: bool = False,
    ) -> DistReporter:
        """
        Spawn local worker processes and load tests. After that, run them.

        @param suite: A tests suite to be run.

        @param reactor: The reactor to use, to be customized in tests.

        @param untilFailure: If C{True}, continue to run the tests until they
            fail.

        @return: A coroutine that completes with the test result.
        """

        # Realize a concrete set of tests to run.
        testCases = list(_iterateTests(suite))

        # Create a worker pool to use to execute them.
        poolStarter = self._workerPoolFactory(
            WorkerPoolConfig(
                # Don't make it larger than is useful or allowed.
                min(len(testCases), self._maxWorkers),
                FilePath(self._workingDirectory),
                self._workerArguments,
                self._logFile,
            ),
        )

        async def runTests(
            pool: StartedWorkerPool,
            testCases: Sequence[ITestCase],
            n: int,
        ) -> DistReporter:
            if untilFailure:
                # If and only if we're running the suite more than once,
                # provide a report about which run this is.
                self._stream.write(f"Test Pass {n + 1}\n")

            # Create a brand new test result object for this iteration and run the
            # test suite with it.
            result = self._makeResult()

            try:
                # Run the tests using the worker pool.
                await pool.run(partial(self._driveWorker, result, iter(testCases)))
            except Exception:
                # Exceptions from test code are handled somewhere else.  An
                # exception here is a bug in the runner itself.  The only
                # convenient place to put it is in the result, though.
                result.original.addError(TestHolder("<runTests>"), Failure())

            # Report the result.
            self.writeResults(result)

            # Make it available elsewhere, eg to determine if another run
            # should be performed.
            return result

        # Announce that we're beginning.  countTestCases result is preferred
        # (over len(testCases)) because testCases may contain synthetic cases
        # for error reporting purposes.
        self._stream.write(f"Running {suite.countTestCases()} tests.\n")

        # Start the worker pool.
        startedPool = await poolStarter.start(reactor)
        try:
            # Start submitting tests to workers in the pool.  Perhaps repeat
            # the whole test suite more than once, if appropriate for our
            # configuration.
            return await iterateWhile(
                partial(shouldContinue, untilFailure),
                countingCalls(partial(runTests, startedPool, testCases)),
            )
        finally:
            # Shut down the worker pool.
            await startedPool.join()

    def run(
        self,
        suite: TestSuite,
        reactor: Optional[IDistTrialReactor] = None,
        *args: tuple,
        **kwargs: dict,
    ) -> DistReporter:
        """
        Run a reactor and a test suite.

        @param suite: The test suite to run.

        @param reactor: If not None, the reactor to run.  Otherwise, the
            global reactor will be used.
        """
        import twisted.internet.reactor as defaultReactor

        defaultReactor_ = IReactorCore(IReactorProcess(defaultReactor, None), None)
        if defaultReactor_ is not None:
            r = fromOptional(
                cast(IDistTrialReactor, defaultReactor),
                reactor,
            )
        else:
            raise TypeError("Reactor does not provide the right interfaces")

        return self._run(
            suite,
            r,
            args,
            kwargs,
        )

    def _run(
        self,
        suite: TestSuite,
        reactor: IDistTrialReactor,
        args: tuple,
        kwargs: dict,
    ) -> DistReporter:
        """
        See ``run``.
        """
        result: Union[Failure, DistReporter]

        def capture(r):
            nonlocal result
            result = r

        d = Deferred.fromCoroutine(self.runAsync(suite, reactor, *args, **kwargs))
        d.addBoth(capture)
        d.addBoth(lambda ignored: reactor.stop())
        reactor.run()

        if isinstance(result, Failure):
            result.raiseException()

        # mypy can't see that raiseException raises an exception so we can
        # only get here if result is not a Failure, so tell mypy to ignore the
        # error it sees here (of returning a Union[Failure, DistReporter] when
        # it wants a DistReporter).
        return result  # type: ignore [return-value]

    def runUntilFailure(self, suite):
        """
        Run the tests with local worker processes until they fail.

        @param suite: A tests suite to be run.
        """
        return self.run(suite, untilFailure=True)
