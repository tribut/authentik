"""Flow models"""
from typing import TYPE_CHECKING, Optional, Type
from uuid import uuid4

from django.db import models
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from model_utils.managers import InheritanceManager
from rest_framework.serializers import BaseSerializer
from structlog.stdlib import get_logger

from authentik.core.types import UserSettingSerializer
from authentik.lib.models import InheritanceForeignKey, SerializerModel
from authentik.policies.models import PolicyBindingModel

if TYPE_CHECKING:
    from authentik.flows.stage import StageView

LOGGER = get_logger()


class NotConfiguredAction(models.TextChoices):
    """Decides how the FlowExecutor should proceed when a stage isn't configured"""

    SKIP = "skip"
    DENY = "deny"
    CONFIGURE = "configure"


class InvalidResponseAction(models.TextChoices):
    """Configure how the flow executor should handle invalid responses to challenges"""

    RETRY = "retry"
    RESTART = "restart"
    RESTART_WITH_CONTEXT = "restart_with_context"


class FlowDesignation(models.TextChoices):
    """Designation of what a Flow should be used for. At a later point, this
    should be replaced by a database entry."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALIDATION = "invalidation"
    ENROLLMENT = "enrollment"
    UNRENOLLMENT = "unenrollment"
    RECOVERY = "recovery"
    STAGE_CONFIGURATION = "stage_configuration"


class Stage(SerializerModel):
    """Stage is an instance of a component used in a flow. This can verify the user,
    enroll the user or offer a way of recovery"""

    stage_uuid = models.UUIDField(primary_key=True, editable=False, default=uuid4)

    name = models.TextField(unique=True)

    objects = InheritanceManager()

    @property
    def type(self) -> Type["StageView"]:
        """Return StageView class that implements logic for this stage"""
        # This is a bit of a workaround, since we can't set class methods with setattr
        if hasattr(self, "__in_memory_type"):
            return getattr(self, "__in_memory_type")
        raise NotImplementedError

    @property
    def component(self) -> str:
        """Return component used to edit this object"""
        raise NotImplementedError

    @property
    def ui_user_settings(self) -> Optional[UserSettingSerializer]:
        """Entrypoint to integrate with User settings. Can either return None if no
        user settings are available, or a challenge."""
        return None

    def __str__(self):
        if hasattr(self, "__in_memory_type"):
            return f"In-memory Stage {getattr(self, '__in_memory_type')}"
        return f"Stage {self.name}"


def in_memory_stage(view: Type["StageView"]) -> Stage:
    """Creates an in-memory stage instance, based on a `view` as view."""
    stage = Stage()
    # Because we can't pickle a locally generated function,
    # we set the view as a separate property and reference a generic function
    # that returns that member
    setattr(stage, "__in_memory_type", view)
    return stage


class Flow(SerializerModel, PolicyBindingModel):
    """Flow describes how a series of Stages should be executed to authenticate/enroll/recover
    a user. Additionally, policies can be applied, to specify which users
    have access to this flow."""

    flow_uuid = models.UUIDField(primary_key=True, editable=False, default=uuid4)

    name = models.TextField()
    slug = models.SlugField(unique=True, help_text=_("Visible in the URL."))

    title = models.TextField(help_text=_("Shown as the Title in Flow pages."))

    designation = models.CharField(
        max_length=100,
        choices=FlowDesignation.choices,
        help_text=_(
            (
                "Decides what this Flow is used for. For example, the Authentication flow "
                "is redirect to when an un-authenticated user visits authentik."
            )
        ),
    )

    background = models.FileField(
        upload_to="flow-backgrounds/",
        default=None,
        null=True,
        help_text=_("Background shown during execution"),
        max_length=500,
    )

    compatibility_mode = models.BooleanField(
        default=True,
        help_text=_(
            "Enable compatibility mode, increases compatibility with "
            "password managers on mobile devices."
        ),
    )

    @property
    def background_url(self) -> str:
        """Get the URL to the background image. If the name is /static or starts with http
        it is returned as-is"""
        if not self.background:
            return "/static/dist/assets/images/flow_background.jpg"
        if self.background.name.startswith("http") or self.background.name.startswith("/static"):
            return self.background.name
        return self.background.url

    stages = models.ManyToManyField(Stage, through="FlowStageBinding", blank=True)

    @property
    def serializer(self) -> BaseSerializer:
        from authentik.flows.api.flows import FlowSerializer

        return FlowSerializer

    @staticmethod
    def with_policy(request: HttpRequest, **flow_filter) -> Optional["Flow"]:
        """Get a Flow by `**flow_filter` and check if the request from `request` can access it."""
        from authentik.policies.engine import PolicyEngine

        flows = Flow.objects.filter(**flow_filter).order_by("slug")
        for flow in flows:
            engine = PolicyEngine(flow, request.user, request)
            engine.build()
            result = engine.result
            if result.passing:
                LOGGER.debug("with_policy: flow passing", flow=flow)
                return flow
            LOGGER.warning("with_policy: flow not passing", flow=flow, messages=result.messages)
        LOGGER.debug("with_policy: no flow found", filters=flow_filter)
        return None

    def __str__(self) -> str:
        return f"Flow {self.name} ({self.slug})"

    class Meta:

        verbose_name = _("Flow")
        verbose_name_plural = _("Flows")

        permissions = [
            ("export_flow", "Can export a Flow"),
            ("view_flow_cache", "View Flow's cache metrics"),
            ("clear_flow_cache", "Clear Flow's cache metrics"),
        ]


class FlowStageBinding(SerializerModel, PolicyBindingModel):
    """Relationship between Flow and Stage. Order is required and unique for
    each flow-stage Binding. Additionally, policies can be specified, which determine if
    this Binding applies to the current user"""

    fsb_uuid = models.UUIDField(primary_key=True, editable=False, default=uuid4)

    target = models.ForeignKey("Flow", on_delete=models.CASCADE)
    stage = InheritanceForeignKey(Stage, on_delete=models.CASCADE)

    evaluate_on_plan = models.BooleanField(
        default=True,
        help_text=_(
            (
                "Evaluate policies during the Flow planning process. "
                "Disable this for input-based policies."
            )
        ),
    )
    re_evaluate_policies = models.BooleanField(
        default=False,
        help_text=_("Evaluate policies when the Stage is present to the user."),
    )

    invalid_response_action = models.TextField(
        choices=InvalidResponseAction.choices,
        default=InvalidResponseAction.RETRY,
        help_text=_(
            "Configure how the flow executor should handle an invalid response to a "
            "challenge. RETRY returns the error message and a similar challenge to the "
            "executor. RESTART restarts the flow from the beginning, and RESTART_WITH_CONTEXT "
            "restarts the flow while keeping the current context."
        ),
    )

    order = models.IntegerField()

    objects = InheritanceManager()

    @property
    def serializer(self) -> BaseSerializer:
        from authentik.flows.api.bindings import FlowStageBindingSerializer

        return FlowStageBindingSerializer

    def __str__(self) -> str:
        return f"Flow-stage binding #{self.order} to {self.target}"

    class Meta:

        ordering = ["target", "order"]

        verbose_name = _("Flow Stage Binding")
        verbose_name_plural = _("Flow Stage Bindings")
        unique_together = (("target", "stage", "order"),)


class ConfigurableStage(models.Model):
    """Abstract base class for a Stage that can be configured by the enduser.
    The stage should create a default flow with the configure_stage designation during
    migration."""

    configure_flow = models.ForeignKey(
        Flow,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=_(
            (
                "Flow used by an authenticated user to configure this Stage. "
                "If empty, user will not be able to configure this stage."
            )
        ),
    )

    class Meta:

        abstract = True
