"""passbook multi-factor authentication engine"""
from logging import getLogger

from django.contrib.auth import login
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect, reverse
from django.views.generic import View

from passbook.core.models import Factor, User
from passbook.core.views.utils import PermissionDeniedView
from passbook.lib.utils.reflection import class_to_path, path_to_class

LOGGER = getLogger(__name__)


class AuthenticationView(UserPassesTestMixin, View):
    """Wizard-like Multi-factor authenticator"""

    SESSION_FACTOR = 'passbook_factor'
    SESSION_PENDING_FACTORS = 'passbook_pending_factors'
    SESSION_PENDING_USER = 'passbook_pending_user'
    SESSION_USER_BACKEND = 'passbook_user_backend'

    pending_user = None
    pending_factors = []

    _current_factor = None

    # Allow only not authenticated users to login
    def test_func(self):
        return self.request.user.is_authenticated is False

    def handle_no_permission(self):
        # Function from UserPassesTestMixin
        if 'next' in self.request.GET:
            return redirect(self.request.GET.get('next'))
        return redirect(reverse('passbook_core:overview'))

    def dispatch(self, request, *args, **kwargs):
        # Extract pending user from session (only remember uid)
        if AuthenticationView.SESSION_PENDING_USER in request.session:
            self.pending_user = get_object_or_404(
                User, id=self.request.session[AuthenticationView.SESSION_PENDING_USER])
        else:
            # No Pending user, redirect to login screen
            return redirect(reverse('passbook_core:auth-login'))
        # Write pending factors to session
        if AuthenticationView.SESSION_PENDING_FACTORS in request.session:
            self.pending_factors = request.session[AuthenticationView.SESSION_PENDING_FACTORS]
        else:
            # Get an initial list of factors which are currently enabled
            # and apply to the current user. We check policies here and block the request
            _all_factors = Factor.objects.filter(enabled=True)
            self.pending_factors = []
            for factor in _all_factors:
                if factor.passes(self.pending_user):
                    self.pending_factors.append(_all_factors)
            # self.pending_factors = Factor
        # Read and instantiate factor from session
        factor_class = None
        if AuthenticationView.SESSION_FACTOR not in request.session:
            factor_class = self.pending_factors[0]
        else:
            factor_class = request.session[AuthenticationView.SESSION_FACTOR]
        # Instantiate Next Factor and pass request
        factor = path_to_class(factor_class)
        self._current_factor = factor(self)
        self._current_factor.request = request
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        """pass get request to current factor"""
        LOGGER.debug("Passing GET to %s", class_to_path(self._current_factor.__class__))
        return self._current_factor.get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """pass post request to current factor"""
        LOGGER.debug("Passing POST to %s", class_to_path(self._current_factor.__class__))
        return self._current_factor.post(request, *args, **kwargs)

    def user_ok(self):
        """Redirect to next Factor"""
        LOGGER.debug("Factor %s passed", class_to_path(self._current_factor.__class__))
        # Remove passed factor from pending factors
        if class_to_path(self._current_factor.__class__) in self.pending_factors:
            self.pending_factors.remove(class_to_path(self._current_factor.__class__))
        next_factor = None
        if self.pending_factors:
            next_factor = self.pending_factors.pop()
            self.request.session[AuthenticationView.SESSION_PENDING_FACTORS] = \
                self.pending_factors
            self.request.session[AuthenticationView.SESSION_FACTOR] = next_factor
            LOGGER.debug("Rendering Factor is %s", next_factor)
            return redirect(reverse('passbook_core:auth-process', kwargs={'factor': next_factor}))
        # User passed all factors
        LOGGER.debug("User passed all factors, logging in")
        return self._user_passed()

    def user_invalid(self):
        """Show error message, user cannot login.
        This should only be shown if user authenticated successfully, but is disabled/locked/etc"""
        LOGGER.debug("User invalid")
        return redirect(reverse('passbook_core:auth-denied'))

    def _user_passed(self):
        """User Successfully passed all factors"""
        # user = authenticate(request=self.request, )
        backend = self.request.session[AuthenticationView.SESSION_USER_BACKEND]
        login(self.request, self.pending_user, backend=backend)
        LOGGER.debug("Logged in user %s", self.pending_user)
        # Cleanup
        self._cleanup()
        return redirect(reverse('passbook_core:overview'))

    def _cleanup(self):
        """Remove temporary data from session"""
        session_keys = ['SESSION_FACTOR', 'SESSION_PENDING_FACTORS',
                        'SESSION_PENDING_USER', 'SESSION_USER_BACKEND', ]
        for key in session_keys:
            if key in self.request.session:
                del self.request.session[key]
        LOGGER.debug("Cleaned up sessions")

class MFAPermissionDeniedView(PermissionDeniedView):
    """User could not be authenticated"""
