"""In-process job queue: one worker thread, first in first out.

Every analysis in this process goes through here, v1 jobs and the wrapped
legacy /api/analyze* calls alike, because the pipeline writes fixed filenames
into one shared WORK_DIR (see CLAUDE.md). The worker holds `pipeline_lock`
while it runs; legacy routes that read WORK_DIR files take the same lock.

Job records are persisted as <jobs_dir>/<id>/job.json so finished jobs survive
a restart. Jobs that were queued or running at restart are marked failed.
"""
import collections
import datetime as dt
import json
import os
import shutil
import threading
import time
import traceback
import uuid

from .problems import ApiProblem

ACTIVE = ('queued', 'running')


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec='seconds').replace('+00:00', 'Z') if t else None


class _Item:
    __slots__ = ('job_id', 'fn', 'done', 'value', 'error')

    def __init__(self, job_id=None, fn=None):
        self.job_id, self.fn = job_id, fn
        self.done = threading.Event()
        self.value = self.error = None


class JobManager:
    def __init__(self, settings, executor):
        self.settings = settings
        self.executor = executor              # executor(job, job_dir) -> (outputs, result)
        self.pipeline_lock = threading.RLock()
        self._cv = threading.Condition()
        self._queue = collections.deque()
        self._jobs = {}
        self._expired = set()
        self._running = None
        self._stopping = False
        self._thread = None
        os.makedirs(settings.jobs_dir, exist_ok=True)
        self._load_existing()

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._worker, name='wisar-job-worker', daemon=True)
        self._thread.start()
        threading.Thread(target=self._sweeper, name='wisar-job-sweeper', daemon=True).start()

    def stop(self, timeout=30):
        """Finish the item in progress, then stop taking work. Queued jobs stay
        queued on disk and are marked interrupted at the next start."""
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)

    def _load_existing(self):
        for name in os.listdir(self.settings.jobs_dir):
            path = os.path.join(self.settings.jobs_dir, name, 'job.json')
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    job = json.load(f)
            except (OSError, ValueError):
                continue
            if job.get('status') in ACTIVE:
                self._finish(job, error={'type': 'about:blank', 'title': 'Interrupted', 'status': 503,
                                         'detail': 'The service restarted before this job finished. Submit it again.'})
            self._jobs[job['id']] = job
        self.sweep()

    # ---- submission ------------------------------------------------------
    def submit(self, job_type, request_body, resolved, owner):
        now = utcnow()
        job = {'id': str(uuid.uuid4()), 'type': job_type, 'status': 'queued', 'owner': owner,
               'created_at': iso(now), 'started_at': None, 'finished_at': None, 'expires_at': None,
               'request': request_body, 'resolved': resolved, 'result': None, 'outputs': None, 'error': None}
        with self._cv:
            queued = sum(1 for i in self._queue if i.job_id)
            if queued >= self.settings.max_queued:
                raise ApiProblem(503, 'Queue full',
                                 f'{queued} jobs are already waiting. Try again shortly.',
                                 headers={'Retry-After': 60})
            self._jobs[job['id']] = job
            self._persist(job)
            self._queue.append(_Item(job_id=job['id']))
            self._cv.notify()
        return job

    def run_inline(self, fn):
        """Queue a callable (a wrapped legacy request) and block until it has run."""
        item = _Item(fn=fn)
        with self._cv:
            self._queue.append(item)
            self._cv.notify()
        item.done.wait()
        if item.error is not None:
            raise item.error
        return item.value

    # ---- queries ---------------------------------------------------------
    def get(self, job_id):
        with self._cv:
            job = self._jobs.get(job_id)
            if job is None:
                if job_id in self._expired:
                    raise ApiProblem(410, 'Gone', 'This job expired and its outputs were deleted.')
                raise ApiProblem(404, 'Not found', 'No job with this id.')
            view = json.loads(json.dumps(job))
            view['queue_position'] = self._position(job_id)
            return view

    def list_for(self, owner, status=None, job_type=None):
        with self._cv:
            jobs = [j for j in self._jobs.values() if j['owner'] == owner
                    and (status is None or j['status'] == status)
                    and (job_type is None or j['type'] == job_type)]
            jobs.sort(key=lambda j: j['created_at'], reverse=True)
            out = []
            for j in jobs:
                v = json.loads(json.dumps(j))
                v['queue_position'] = self._position(j['id'])
                out.append(v)
            return out

    def stats(self):
        with self._cv:
            return {'queued': sum(1 for i in self._queue),
                    'running': 1 if self._running is not None else 0,
                    'max_queued': self.settings.max_queued}

    def job_dir(self, job_id):
        return os.path.join(self.settings.jobs_dir, job_id)

    def _position(self, job_id):
        for n, item in enumerate(self._queue, start=1):
            if item.job_id == job_id:
                return n
        return None

    # ---- deletion --------------------------------------------------------
    def delete(self, job_id, requester):
        with self._cv:
            job = self._jobs.get(job_id)
            if job is None:
                raise ApiProblem(410 if job_id in self._expired else 404,
                                 'Gone' if job_id in self._expired else 'Not found')
            if job['owner'] != requester:
                raise ApiProblem(403, 'Forbidden', 'Only the user who created a job can delete it.')
            if job['status'] == 'running':
                raise ApiProblem(409, 'Job is running', 'A running job cannot be interrupted; delete it after it finishes.')
            self._queue = collections.deque(i for i in self._queue if i.job_id != job_id)
            del self._jobs[job_id]
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    def sweep(self):
        now = utcnow()
        with self._cv:
            expired = [j['id'] for j in self._jobs.values()
                       if j.get('expires_at') and _parse(j['expires_at']) <= now]
            for jid in expired:
                del self._jobs[jid]
                self._expired.add(jid)
        for jid in expired:
            shutil.rmtree(self.job_dir(jid), ignore_errors=True)
        return expired

    # ---- worker ----------------------------------------------------------
    def _worker(self):
        while True:
            with self._cv:
                while not self._queue and not self._stopping:
                    self._cv.wait()
                if self._stopping:
                    return
                item = self._queue.popleft()
                self._running = item
                job = self._jobs.get(item.job_id) if item.job_id else None
                if job is not None:
                    job['status'] = 'running'
                    job['started_at'] = iso(utcnow())
                    self._persist(job)
            try:
                with self.pipeline_lock:
                    if job is not None:
                        self._run_job(job)
                    elif item.fn is not None:
                        try:
                            item.value = item.fn()
                        except BaseException as e:  # handed back to the waiting request
                            item.error = e
            finally:
                with self._cv:
                    self._running = None
                item.done.set()

    def _run_job(self, job):
        try:
            outputs, result = self.executor(job, self.job_dir(job['id']))
        except Exception as e:
            traceback.print_exc()
            with self._cv:
                self._finish(job, error={'type': 'about:blank', 'title': 'Analysis failed', 'status': 500,
                                         'detail': str(e) or type(e).__name__})
            return
        base = f"/api/v1/jobs/{job['id']}/outputs/"
        for name, meta in outputs.items():
            meta['href'] = base + name
        with self._cv:
            job['outputs'] = outputs
            job['result'] = result
            self._finish(job)

    def _finish(self, job, error=None):
        now = utcnow()
        job['status'] = 'failed' if error else 'succeeded'
        job['error'] = error
        job['finished_at'] = iso(now)
        job['expires_at'] = iso(now + dt.timedelta(hours=self.settings.job_ttl_hours))
        self._persist(job)

    def _persist(self, job):
        d = self.job_dir(job['id'])
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, 'job.json.part')
        with open(tmp, 'w') as f:
            json.dump(job, f)
        os.replace(tmp, os.path.join(d, 'job.json'))

    def _sweeper(self):
        while not self._stopping:
            time.sleep(self.settings.sweep_interval_seconds)
            try:
                self.sweep()
            except Exception:
                traceback.print_exc()


def _parse(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
