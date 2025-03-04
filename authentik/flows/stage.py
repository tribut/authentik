"""authentik stage Base view"""
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.http.request import QueryDict
from django.http.response import HttpResponse
from django.urls import reverse
from django.views.generic.base import View
from rest_framework.request import Request
from structlog.stdlib import get_logger

from authentik.core.models import DEFAULT_AVATAR, User
from authentik.flows.challenge import (
    Challenge,
    ChallengeResponse,
    ContextualFlowInfo,
    HttpChallengeResponse,
    WithUserInfoChallenge,
)
from authentik.flows.models import InvalidResponseAction
from authentik.flows.planner import PLAN_CONTEXT_APPLICATION, PLAN_CONTEXT_PENDING_USER
from authentik.flows.views import FlowExecutorView

PLAN_CONTEXT_PENDING_USER_IDENTIFIER = "pending_user_identifier"
LOGGER = get_logger()


class StageView(View):
    """Abstract Stage, inherits TemplateView but can be combined with FormView"""

    executor: FlowExecutorView

    request: HttpRequest = None

    def __init__(self, executor: FlowExecutorView, **kwargs):
        self.executor = executor
        super().__init__(**kwargs)

    def get_pending_user(self, for_display=False) -> User:
        """Either show the matched User object or show what the user entered,
        based on what the earlier stage (mostly IdentificationStage) set.
        _USER_IDENTIFIER overrides the first User, as PENDING_USER is used for
        other things besides the form display.

        If no user is pending, returns request.user"""
        if PLAN_CONTEXT_PENDING_USER_IDENTIFIER in self.executor.plan.context and for_display:
            return User(
                username=self.executor.plan.context.get(PLAN_CONTEXT_PENDING_USER_IDENTIFIER),
                email="",
            )
        if PLAN_CONTEXT_PENDING_USER in self.executor.plan.context:
            return self.executor.plan.context[PLAN_CONTEXT_PENDING_USER]
        return self.request.user


class ChallengeStageView(StageView):
    """Stage view which response with a challenge"""

    response_class = ChallengeResponse

    def get_response_instance(self, data: QueryDict) -> ChallengeResponse:
        """Return the response class type"""
        return self.response_class(None, data=data, stage=self)

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        """Return a challenge for the frontend to solve"""
        challenge = self._get_challenge(*args, **kwargs)
        if not challenge.is_valid():
            LOGGER.warning(
                "f(ch): Invalid challenge",
                binding=self.executor.current_binding,
                errors=challenge.errors,
                stage_view=self,
                challenge=challenge,
            )
        return HttpChallengeResponse(challenge)

    # pylint: disable=unused-argument
    def post(self, request: Request, *args, **kwargs) -> HttpResponse:
        """Handle challenge response"""
        challenge: ChallengeResponse = self.get_response_instance(data=request.data)
        if not challenge.is_valid():
            if self.executor.current_binding.invalid_response_action in [
                InvalidResponseAction.RESTART,
                InvalidResponseAction.RESTART_WITH_CONTEXT,
            ]:
                keep_context = (
                    self.executor.current_binding.invalid_response_action
                    == InvalidResponseAction.RESTART_WITH_CONTEXT
                )
                LOGGER.debug(
                    "f(ch): Invalid response, restarting flow",
                    binding=self.executor.current_binding,
                    stage_view=self,
                    keep_context=keep_context,
                )
                return self.executor.restart_flow(keep_context)
            return self.challenge_invalid(challenge)
        return self.challenge_valid(challenge)

    def format_title(self) -> str:
        """Allow usage of placeholder in flow title."""
        return self.executor.flow.title % {
            "app": self.executor.plan.context.get(PLAN_CONTEXT_APPLICATION, "")
        }

    def _get_challenge(self, *args, **kwargs) -> Challenge:
        challenge = self.get_challenge(*args, **kwargs)
        if "flow_info" not in challenge.initial_data:
            flow_info = ContextualFlowInfo(
                data={
                    "title": self.format_title(),
                    "background": self.executor.flow.background_url,
                    "cancel_url": reverse("authentik_flows:cancel"),
                }
            )
            flow_info.is_valid()
            challenge.initial_data["flow_info"] = flow_info.data
        if isinstance(challenge, WithUserInfoChallenge):
            # If there's a pending user, update the `username` field
            # this field is only used by password managers.
            # If there's no user set, an error is raised later.
            if user := self.get_pending_user(for_display=True):
                challenge.initial_data["pending_user"] = user.username
            challenge.initial_data["pending_user_avatar"] = DEFAULT_AVATAR
            if not isinstance(user, AnonymousUser):
                challenge.initial_data["pending_user_avatar"] = user.avatar
        return challenge

    def get_challenge(self, *args, **kwargs) -> Challenge:
        """Return the challenge that the client should solve"""
        raise NotImplementedError

    def challenge_valid(self, response: ChallengeResponse) -> HttpResponse:
        """Callback when the challenge has the correct format"""
        raise NotImplementedError

    def challenge_invalid(self, response: ChallengeResponse) -> HttpResponse:
        """Callback when the challenge has the incorrect format"""
        challenge_response = self._get_challenge()
        full_errors = {}
        for field, errors in response.errors.items():
            for error in errors:
                full_errors.setdefault(field, [])
                full_errors[field].append(
                    {
                        "string": str(error),
                        "code": error.code,
                    }
                )
        challenge_response.initial_data["response_errors"] = full_errors
        if not challenge_response.is_valid():
            LOGGER.warning(
                "f(ch): invalid challenge response",
                binding=self.executor.current_binding,
                errors=challenge_response.errors,
                stage_view=self,
            )
        return HttpChallengeResponse(challenge_response)
