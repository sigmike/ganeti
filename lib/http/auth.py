#
#

# Copyright (C) 2007, 2008 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.

"""HTTP authentication module.

"""

import logging
import time
import re
import base64
import binascii

from ganeti import constants
from ganeti import utils
from ganeti import http

from cStringIO import StringIO


# Digest types from RFC2617
HTTP_BASIC_AUTH = "Basic"
HTTP_DIGEST_AUTH = "Digest"

# Not exactly as described in RFC2616, section 2.2, but good enough
_NOQUOTE = re.compile(r"^[-_a-z0-9]$", re.I)


def _FormatAuthHeader(scheme, params):
  """Formats WWW-Authentication header value as per RFC2617, section 1.2

  @type scheme: str
  @param scheme: Authentication scheme
  @type params: dict
  @param params: Additional parameters
  @rtype: str
  @return: Formatted header value

  """
  buf = StringIO()

  buf.write(scheme)

  for name, value in params.iteritems():
    buf.write(" ")
    buf.write(name)
    buf.write("=")
    if _NOQUOTE.match(value):
      buf.write(value)
    else:
      buf.write("\"")
      # TODO: Better quoting
      buf.write(value.replace("\"", "\\\""))
      buf.write("\"")

  return buf.getvalue()


class HttpServerRequestAuthentication(object):
  # Default authentication realm
  AUTH_REALM = None

  def GetAuthRealm(self, req):
    """Returns the authentication realm for a request.

    MAY be overriden by a subclass, which then can return different realms for
    different paths. Returning "None" means no authentication is needed for a
    request.

    @type req: L{http.server._HttpServerRequest}
    @param req: HTTP request context
    @rtype: str or None
    @return: Authentication realm

    """
    return self.AUTH_REALM

  def PreHandleRequest(self, req):
    """Called before a request is handled.

    @type req: L{http.server._HttpServerRequest}
    @param req: HTTP request context

    """
    realm = self.GetAuthRealm(req)

    # Authentication required?
    if realm is None:
      return

    # Check "Authorization" header
    if self._CheckAuthorization(req):
      # User successfully authenticated
      return

    # Send 401 Unauthorized response
    params = {
      "realm": realm,
      }

    # TODO: Support for Digest authentication (RFC2617, section 3).
    # TODO: Support for more than one WWW-Authenticate header with the same
    # response (RFC2617, section 4.6).
    headers = {
      http.HTTP_WWW_AUTHENTICATE: _FormatAuthHeader(HTTP_BASIC_AUTH, params),
      }

    raise http.HttpUnauthorized(headers=headers)

  def _CheckAuthorization(self, req):
    """Checks "Authorization" header sent by client.

    @type req: L{http.server._HttpServerRequest}
    @param req: HTTP request context
    @type credentials: str
    @param credentials: Credentials sent
    @rtype: bool
    @return: Whether user is allowed to execute request

    """
    credentials = req.request_headers.get(http.HTTP_AUTHORIZATION, None)
    if not credentials:
      return False

    # Extract scheme
    parts = credentials.strip().split(None, 2)
    if len(parts) < 1:
      # Missing scheme
      return False

    # RFC2617, section 1.2: "[...] It uses an extensible, case-insensitive
    # token to identify the authentication scheme [...]"
    scheme = parts[0].lower()

    if scheme == HTTP_BASIC_AUTH.lower():
      # Do basic authentication
      if len(parts) < 2:
        raise http.HttpBadRequest(message=("Basic authentication requires"
                                           " credentials"))
      return self._CheckBasicAuthorization(req, parts[1])

    elif scheme == HTTP_DIGEST_AUTH.lower():
      # TODO: Implement digest authentication
      # RFC2617, section 3.3: "Note that the HTTP server does not actually need
      # to know the user's cleartext password. As long as H(A1) is available to
      # the server, the validity of an Authorization header may be verified."
      pass

    # Unsupported authentication scheme
    return False

  def _CheckBasicAuthorization(self, req, input):
    """Checks credentials sent for basic authentication.

    @type req: L{http.server._HttpServerRequest}
    @param req: HTTP request context
    @type input: str
    @param input: Username and password encoded as Base64
    @rtype: bool
    @return: Whether user is allowed to execute request

    """
    try:
      creds = base64.b64decode(input.encode('ascii')).decode('ascii')
    except (TypeError, binascii.Error, UnicodeError):
      logging.exception("Error when decoding Basic authentication credentials")
      return False

    if ":" not in creds:
      return False

    (user, password) = creds.split(":", 1)

    return self.Authenticate(req, user, password)

  def AuthenticateBasic(self, req, user, password):
    """Checks the password for a user.

    This function MUST be overriden by a subclass.

    """
    raise NotImplementedError()


class PasswordFileUser(object):
  """Data structure for users from password file.

  """
  def __init__(self, name, password, options):
    self.name = name
    self.password = password
    self.options = options


def ReadPasswordFile(file_name):
  """Reads a password file.

  Lines in the password file are of the following format:

    <username> <password> [options]

  Fields are separated by whitespace. Username and password are mandatory,
  options are optional and separated by comma (","). Empty lines and comments
  ("#") are ignored.

  @type file_name: str
  @param file_name: Path to password file
  @rtype: dict
  @return: Dictionary containing L{PasswordFileUser} instances

  """
  users = {}

  for line in utils.ReadFile(file_name).splitlines():
    line = line.strip()

    # Ignore empty lines and comments
    if not line or line.startswith("#"):
      continue

    parts = line.split(None, 2)
    if len(parts) < 2:
      # Invalid line
      continue

    name = parts[0]
    password = parts[1]

    # Extract options
    options = []
    if len(parts) >= 3:
      for part in parts[2].split(","):
        options.append(part.strip())

    users[name] = PasswordFileUser(name, password, options)

  return users