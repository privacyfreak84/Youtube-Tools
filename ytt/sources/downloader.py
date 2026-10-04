"""Downloading a queue of videos, several at a time, with the failure rules of DESIGN.md section 13:

- a failed download is reported and the next video in the queue takes its place;
- never more than `target` videos are downloaded successfully;
- results are handed over one at a time, in the order the videos were queued even if they finish out of order,
  so whatever records them (the library) sees them in the order the person chose;
- Ctrl-C stops running downloads quickly, removes what they left behind, hands over everything that did finish,
  and then raises KeyboardInterrupt again.

Nothing here touches the database or prints: the caller gets `on_result(outcome)` calls from the main thread.
"""
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from ytt.sources.errors import DownloadError, DownloadStopped
from ytt.sources.files import download_clip


@dataclass
class Job:
    """One video to get. `path` forces the exact file name (a clip being fetched again keeps its recorded name)."""
    video_id: str
    label: str = ""                 # what a person would call it (the title)
    position: int = None            # its place in the sorted list, when it came from one
    path: Path = None
    data: dict = field(default_factory=dict)      # anything the caller wants back with the outcome


@dataclass
class Outcome:
    job: Job
    status: str                     # completed | failed
    path: Path = None
    info: dict = field(default_factory=dict)
    detail: str = ""


def download_all(backend, queue, *, target, folder, max_height=1080, workers=1, on_result=None):
    """Work through `queue` (a list of Job, in the order wanted) until `target` have been downloaded: a job that
    fails is replaced by the next one in the queue. A caller that wants no replacements passes a queue that is
    exactly `target` long. Returns every Outcome in queue order. Raises KeyboardInterrupt after handing over
    what finished."""
    queue = list(queue)
    outcomes = {}                   # queue index -> Outcome
    handed = 0                      # outcomes below this index have been handed to on_result
    stop = threading.Event()
    inflight = {}                   # future -> queue index
    next_index = 0
    successes = 0

    def hand_over(upto_gaps_ok=False):
        nonlocal handed
        while True:
            if handed in outcomes:
                if on_result:
                    on_result(outcomes[handed])
                handed += 1
            elif upto_gaps_ok and any(i >= handed for i in outcomes):
                handed += 1                     # a job that never finished (cancelled): skip its place
            else:
                break

    def work(job):
        final, info = download_clip(backend, job.video_id, folder, exact_path=job.path,
                                    max_height=max_height, stop=stop)
        return final, info

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        while True:
            while (next_index < len(queue) and successes + len(inflight) < target
                   and len(inflight) < max(1, workers)):
                inflight[pool.submit(work, queue[next_index])] = next_index
                next_index += 1
            if not inflight:
                break
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in finished:
                index = inflight.pop(fut)
                job = queue[index]
                try:
                    final, info = fut.result()
                    outcomes[index] = Outcome(job, "completed", final, info)
                    successes += 1
                except DownloadStopped:
                    pass
                except DownloadError as e:
                    outcomes[index] = Outcome(job, "failed", detail=str(e))
            hand_over()
    except KeyboardInterrupt:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        for fut, index in list(inflight.items()):        # downloads that finished while we were stopping still count
            if fut.done() and not fut.cancelled():
                try:
                    final, info = fut.result()
                    outcomes[index] = Outcome(queue[index], "completed", final, info)
                except BaseException:
                    pass
        hand_over(upto_gaps_ok=True)
        raise
    pool.shutdown(wait=True)
    hand_over(upto_gaps_ok=True)
    return [outcomes[i] for i in sorted(outcomes)]
