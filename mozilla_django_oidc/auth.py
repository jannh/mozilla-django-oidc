import base64
import hashlib
import jwt
import logging
import requests

try:
    from django.utils.encoding import smart_bytes
except ImportError:
    from django.utils.encoding import smart_str as smart_bytes
from django.contrib.auth import get_user_model
from django.core.urlresolvers import reverse

from mozilla_django_oidc.utils import absolutify, import_from_settings


LOGGER = logging.getLogger(__name__)


def default_username_algo(email):
    # bluntly stolen from django-browserid
    # store the username as a base64 encoded sha224 of the email address
    # this protects against data leakage because usernames are often
    # treated as public identifiers (so we can't use the email address).
    return base64.urlsafe_b64encode(
        hashlib.sha1(smart_bytes(email)).digest()
    ).rstrip(b'=')


class OIDCAuthenticationBackend(object):
    """Override Django's authentication."""

    def __init__(self, *args, **kwargs):
        """Initialize settings."""
        self.OIDC_OP_TOKEN_ENDPOINT = import_from_settings('OIDC_OP_TOKEN_ENDPOINT')
        self.OIDC_OP_USER_ENDPOINT = import_from_settings('OIDC_OP_USER_ENDPOINT')
        self.OIDC_RP_CLIENT_ID = import_from_settings('OIDC_RP_CLIENT_ID')
        self.OIDC_RP_CLIENT_SECRET = import_from_settings('OIDC_RP_CLIENT_SECRET')

        self.UserModel = get_user_model()

    def filter_users_by_claims(self, claims):
        """Return all users matching the specified email."""
        email = claims.get('email')
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email=email)

    def create_user(self, claims):
        """Return object for a newly created user account."""
        # bluntly stolen from django-browserid
        # https://github.com/mozilla/django-browserid/blob/master/django_browserid/auth.py

        username_algo = import_from_settings('OIDC_USERNAME_ALGO', None)
        email = claims.get('email')
        if not email:
            return None

        if username_algo:
            username = username_algo(email)
        else:
            username = default_username_algo(email)

        return self.UserModel.objects.create_user(username, email)

    def verify_token(self, token, **kwargs):
        """Validate the token signature."""

        # Get JWT audience without signature verification
        audience = jwt.decode(token, verify=False)['aud']

        secret = self.OIDC_RP_CLIENT_SECRET
        if import_from_settings('OIDC_RP_CLIENT_SECRET_ENCODED', False):
            secret = base64.urlsafe_b64decode(self.OIDC_RP_CLIENT_SECRET)

        return jwt.decode(token, secret,
                          verify=import_from_settings('OIDC_VERIFY_JWT', True),
                          audience=audience)

    def authenticate(self, code=None, state=None):
        """Authenticates a user based on the OIDC code flow."""

        if not code or not state:
            return None

        token_payload = {
            'client_id': self.OIDC_RP_CLIENT_ID,
            'client_secret': self.OIDC_RP_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': absolutify(reverse('oidc_authentication_callback'))
        }

        # Get the token
        response = requests.post(self.OIDC_OP_TOKEN_ENDPOINT,
                                 data=token_payload,
                                 verify=import_from_settings('OIDC_VERIFY_SSL', True))
        response.raise_for_status()

        # Validate the token
        token_response = response.json()
        payload = self.verify_token(token_response.get('id_token'))

        if payload:
            access_token = token_response.get('access_token')
            user_response = requests.get(self.OIDC_OP_USER_ENDPOINT,
                                         headers={
                                             'Authorization': 'Bearer {0}'.format(access_token)
                                         })
            user_response.raise_for_status()
            user_info = user_response.json()
            email = user_info.get('email')

            # email based filtering
            users = self.filter_users_by_claims(user_info)

            if len(users) == 1:
                return users[0]
            elif len(users) > 1:
                # In the rare case that two user accounts have the same email address,
                # log and bail. Randomly selecting one seems really wrong.
                LOGGER.warn('Multiple users with email address %s.', email)
                return None
            elif import_from_settings('OIDC_CREATE_USER', True):
                user = self.create_user(user_info)
                return user
            else:
                LOGGER.debug('Login failed: No user with email %s found, and '
                             'OIDC_CREATE_USER is False', email)
                return None
        return None

    def get_user(self, user_id):
        """Return a user based on the id."""

        try:
            return self.UserModel.objects.get(pk=user_id)
        except self.UserModel.DoesNotExist:
            return None