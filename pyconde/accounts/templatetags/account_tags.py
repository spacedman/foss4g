from django.template import Library
from django.conf import settings

from .. import models


register = Library()


@register.filter
def account_name(user):
    """
    Helper filter for rendering a user object independently of the
    unicode-version of that object.
    """
    if user is None:
        return None
    if user.first_name and user.last_name and user.first_name.lstrip() and user.last_name.lstrip():
        return u'{0} {1}'.format(user.first_name, user.last_name)
    return user.username


@register.inclusion_tag('accounts/tags/avatar.html')
def avatar(user, width=80):
    """
    Handles all avatar renderings in the frontend. If the user doesn't have
    an avatar attached to his profile, gravatar will be used if enabled in
    the settings.
    """
    profile = None
    email = None
    if isinstance(user, models.Profile):
        profile = user
        email = profile.user.email
    else:
        profile = user.profile
        email = user.email
    return {
        'profile': profile,
        'use_gravatar': getattr(settings, 'ACCOUNTS_FALLBACK_TO_GRAVATAR', False),
        'email': email,
        'width': width,
        'avatar_dimensions': '%sx%s' % (width, width),
    }
