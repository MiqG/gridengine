import os
import sys
import time
from gridengine import job, settings


# ----------------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------------
class TimeoutError(Exception):
  pass


# ----------------------------------------------------------------------------
# Generic scheduler interface
# ----------------------------------------------------------------------------
class Scheduler(object):
  """A generic scheduler interface"""
  def schedule(self, submission_host, job_queue, **kwargs):
    raise NotImplementedError()
  def join(self, timeout=None):
    raise NotImplementedError()
  def killall(self):
    raise NotImplementedError()

def best_available():
  """Return the best available scheduler for a system"""
  try:
    return GridEngineScheduler()
  except RuntimeError:
    return ProcessScheduler()


# ----------------------------------------------------------------------------
# MultiProcess Scheduler
# ----------------------------------------------------------------------------
class ProcessScheduler(Scheduler):
  """
  A Scheduler that schedules jobs as multiple processes on a multi-core CPU.
  Requires ZeroMQ, but not a Sun Grid Engine (drmaa).
  """
  def __init__(self, max_threads=None):
    import multiprocessing
    self.multiprocessing = multiprocessing
    # set the threads to the cpu count
    self.max_threads = max_threads if max_threads else self.multiprocessing.cpu_count()

  def __del__(self):
    self.killall()

  def schedule(self, submission_host, job_queue, **kwargs):
    """schedule the jobs (dict of {jobid, job.Job}) to run asynchronously

    Args:
      submission_host: the address of the submission host (job.JobDispatcher.address)
      job_queue: the dict of {jobid, job.Job{ items to run

    Keyword Args:
      ignored (for compatibility)
    """

    self.pool = self.multiprocessing.Pool(processes=self.max_threads)
    args = (['', submission_host] for jobid in range(1,len(job_queue)+1))
    self.result = self.pool.map_async(job.run_from_command_line, args)
    print('ProcessScheduler: submitted {0} jobs across {1} concurrent processes'
          .format(len(job_queue), self.max_threads))

  def join(self, timeout=None):
    """Wait until the jobs terminate

    This blocks the calling thread until the jobs terminate - either
    normally or through an unhandled exception - or until the optional
    timeout occurs.

    Raises:
      TimeoutError: If the jobs have not finished before the specified timeout
    """
    try:
      self.result.get(timeout=timeout)
      self.pool.close()
      self.pool.join()
    except self.multiprocessing.TimeoutError:
      raise TimeoutError('call to join() timed out before jobs finished')
    except (KeyboardInterrupt, Exception) as e:
      self.pool.terminate()
      self.pool.join()
      raise

  def killall(self):
    try:
      self.pool.terminate()
      self.pool.join()
    except (AttributeError, RuntimeError):
      pass


# ----------------------------------------------------------------------------
# Grid Engine Scheduler
# ----------------------------------------------------------------------------
class GridEngineScheduler(Scheduler):
  """
  A Scheduler that schedules jobs on a Sun Grid Engine (SGE) using the drmaa
  library
  """

  def __init__(self, **resources):
    """Initialize a GridEngineScheduler instance

    Only one instance may run per Python process, since the underlying drmaa
    layer is a singleton.
    
    Keyword Args:
      Resources to be passed to qsub commands. These override any
      arguments that were given to the constructor:
        `-l` command:
          h_cpu: maximum time expressed in format '02:00:00' (2 hours)
          h_vmem: maximum memory allocation before job is killed in format '10G' (10GB)
          virtual_free: memory free on host BEFORE job can be allocated
        `-pe` command:
          pe_type: either 'smp' or 'ompi' for shared memory or distributed memory.
          n_slots: the number of slots to request to the grid engine.
    """
    import drmaa
    self.drmaa = drmaa

    # pass-through options to the jobs
    self.resources = settings.DEFAULT_RESOURCES
    self.resources.update(resources)
    self.session = drmaa.Session()
    self.session.initialize()
    self.sgeids = []

  def __del__(self):
    if hasattr(self, 'drmaa'):
      try:
        self.killall()
        self.session.exit()
      except (TypeError, self.drmaa.errors.NoActiveSessionException):
        pass

  def schedule(self, submission_host, job_queue, **resources):
    """schedule the jobs (dict of {jobid, job.Job}) to run

    Args:
      submission_host: the address of the submission host (job.JobDispatcher.address)
      job_queue: the dict of {jobid, job.Job} items to run

    Keyword Args:
      Resources to be passed to qsub commands. These override any
      arguments that were given to the constructor:
        `-l` command:
          h_cpu: maximum time expressed in format '02:00:00' (2 hours)
          h_vmem: maximum memory allocation before job is killed in format '10G' (10GB)
          virtual_free: memory free on host BEFORE job can be allocated
        `-pe` command:
          pe_type: either 'smp' or 'ompi' for shared memory or distributed memory.
          n_slots: the number of slots to request to the grid engine.
    """

    # dont spin up the scheduler if there's nothing to do
    if not job_queue: return

    # update the keyword resources
    resources = {**self.resources, **resources}

    # retrieve the job target
    target = job_queue[0].target
    target = target.__module__ + '.' + target.__name__

    # build the homogenous job template and submit array
    with self.session.createJobTemplate() as jt:
      jt.jobEnvironment = os.environ

      jt.remoteCommand = os.path.expanduser(settings.WRAPPER)
      jt.args = [submission_host]
      jt.jobName = resources.pop('name',target)
      jt.jobName = ''.join(jt.jobName.split())[:15]
      ## prepare -pe
      if 'pe_type' in resources.keys() and 'n_slots' in resources.keys(): 
        jt.nativeSpecification = '-pe '+' '.join([resources.pop('pe_type'),
                                                  resources.pop('n_slots')])+' '
      else:
        jt.nativeSpecification = ''
      ## prepare -l  
      jt.nativeSpecification += '-l ' + ','.join(
        resource + '=' + str(value) for resource,value in resources.items()
      ) if resources else ''
            
      jt.joinFiles = True
      jt.outputPath = ':'+os.path.expanduser(settings.TEMPDIR)
      jt.errorPath  = ':'+os.path.expanduser(settings.TEMPDIR)

      self.sgeids  = self.session.runBulkJobs(jt, 1, len(job_queue), 1)
      self.arrayid = self.sgeids[0].split('.')[0]
      print('GridEngineScheduler: submitted {0} jobs in array {1}'
            .format(len(job_queue), self.arrayid))

  def join(self, timeout=None):
    """Wait until the jobs terminate

    This blocks the calling thread until the jobs terminate - either
    normally or through an unhandled exception - or until the optional
    timeout occurs.

    Args:
      timeout (int): The time to wait for the jobs to join before raising

    Raises:
      TimeoutError: If the jobs have not finished before the specified timeout
    """
    timeout = float('inf') if timeout is None else int(timeout)
    start_time = time.time()
    while True:
      try:
        self.session.synchronize(self.sgeids, timeout=min(1,timeout), dispose=True)
      except self.drmaa.ExitTimeoutException:
        if time.time() - start_time > timeout:
          raise TimeoutError('call to join() timed out before jobs finished')
      except (KeyboardInterrupt, Exception) as e:
        self.killall()
        raise
      else:
        break

  def killall(self, verbose=False):
    """Terminate any running jobs"""
    self.session.control(self.drmaa.Session.JOB_IDS_SESSION_ALL,
                         self.drmaa.JobControlAction.TERMINATE)
