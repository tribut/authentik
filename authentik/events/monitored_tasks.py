"""Monitored tasks"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from timeit import default_timer
from traceback import format_tb
from typing import Any, Optional

from celery import Task
from django.core.cache import cache
from prometheus_client import Gauge

from authentik.events.models import Event, EventAction

GAUGE_TASKS = Gauge(
    "authentik_system_tasks",
    "System tasks and their status",
    ["task_name", "task_uid", "status"],
)


class TaskResultStatus(Enum):
    """Possible states of tasks"""

    SUCCESSFUL = 1
    WARNING = 2
    ERROR = 4


@dataclass
class TaskResult:
    """Result of a task run, this class is created by the task itself
    and used by self.set_status"""

    status: TaskResultStatus

    messages: list[str] = field(default_factory=list)

    # Optional UID used in cache for tasks that run in different instances
    uid: Optional[str] = field(default=None)

    def with_error(self, exc: Exception) -> "TaskResult":
        """Since errors might not always be pickle-able, set the traceback"""
        self.messages.extend(format_tb(exc.__traceback__))
        self.messages.append(str(exc))
        return self


@dataclass
class TaskInfo:
    """Info about a task run"""

    task_name: str
    start_timestamp: float
    finish_timestamp: float
    finish_time: datetime

    result: TaskResult

    task_call_module: str
    task_call_func: str
    task_call_args: list[Any] = field(default_factory=list)
    task_call_kwargs: dict[str, Any] = field(default_factory=dict)

    task_description: Optional[str] = field(default=None)

    @property
    def html_name(self) -> list[str]:
        """Get task_name, but split on underscores, so we can join in the html template."""
        return self.task_name.split("_")

    @staticmethod
    def all() -> dict[str, "TaskInfo"]:
        """Get all TaskInfo objects"""
        return cache.get_many(cache.keys("task_*"))

    @staticmethod
    def by_name(name: str) -> Optional["TaskInfo"]:
        """Get TaskInfo Object by name"""
        return cache.get(f"task_{name}")

    def delete(self):
        """Delete task info from cache"""
        return cache.delete(f"task_{self.task_name}")

    def set_prom_metrics(self):
        """Update prometheus metrics"""
        start = default_timer()
        if hasattr(self, "start_timestamp"):
            start = self.start_timestamp
        try:
            duration = max(self.finish_timestamp - start, 0)
        except TypeError:
            duration = 0
        GAUGE_TASKS.labels(
            task_name=self.task_name,
            task_uid=self.result.uid or "",
            status=self.result.status,
        ).set(duration)

    def save(self, timeout_hours=6):
        """Save task into cache"""
        key = f"task_{self.task_name}"
        if self.result.uid:
            key += f"_{self.result.uid}"
            self.task_name += f"_{self.result.uid}"
        self.set_prom_metrics()
        cache.set(key, self, timeout=timeout_hours * 60 * 60)


class MonitoredTask(Task):
    """Task which can save its state to the cache"""

    # For tasks that should only be listed if they failed, set this to False
    save_on_success: bool

    _result: Optional[TaskResult]

    _uid: Optional[str]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.save_on_success = True
        self._uid = None
        self._result = None
        self.result_timeout_hours = 6
        self.start = default_timer()

    def set_uid(self, uid: str):
        """Set UID, so in the case of an unexpected error its saved correctly"""
        self._uid = uid

    def set_status(self, result: TaskResult):
        """Set result for current run, will overwrite previous result."""
        self._result = result

    # pylint: disable=too-many-arguments
    def after_return(self, status, retval, task_id, args: list[Any], kwargs: dict[str, Any], einfo):
        if self._result:
            if not self._result.uid:
                self._result.uid = self._uid
            if self.save_on_success:
                TaskInfo(
                    task_name=self.__name__,
                    task_description=self.__doc__,
                    start_timestamp=self.start,
                    finish_timestamp=default_timer(),
                    finish_time=datetime.now(),
                    result=self._result,
                    task_call_module=self.__module__,
                    task_call_func=self.__name__,
                    task_call_args=args,
                    task_call_kwargs=kwargs,
                ).save(self.result_timeout_hours)
        return super().after_return(status, retval, task_id, args, kwargs, einfo=einfo)

    # pylint: disable=too-many-arguments
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        if not self._result:
            self._result = TaskResult(status=TaskResultStatus.ERROR, messages=[str(exc)])
        if not self._result.uid:
            self._result.uid = self._uid
        TaskInfo(
            task_name=self.__name__,
            task_description=self.__doc__,
            start_timestamp=self.start,
            finish_timestamp=default_timer(),
            finish_time=datetime.now(),
            result=self._result,
            task_call_module=self.__module__,
            task_call_func=self.__name__,
            task_call_args=args,
            task_call_kwargs=kwargs,
        ).save(self.result_timeout_hours)
        Event.new(
            EventAction.SYSTEM_TASK_EXCEPTION,
            message=(
                f"Task {self.__name__} encountered an error: " "\n".join(self._result.messages)
            ),
        ).save()
        return super().on_failure(exc, task_id, args, kwargs, einfo=einfo)

    def run(self, *args, **kwargs):
        raise NotImplementedError


for task in TaskInfo.all().values():
    task.set_prom_metrics()
