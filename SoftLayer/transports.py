"""
    SoftLayer.transports
    ~~~~~~~~~~~~~~~~~~~~
    XML-RPC transport layer that uses the requests library.

    :license: MIT, see LICENSE for more details.
"""
import importlib
import json
import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from SoftLayer import consts
from SoftLayer import exceptions
from SoftLayer import utils

LOGGER = logging.getLogger(__name__)
# transports.Request does have a lot of instance attributes. :(
# pylint: disable=too-many-instance-attributes

__all__ = [
    'Request',
    'XmlRpcTransport',
    'RestTransport',
    'TimingTransport',
    'FixtureTransport',
    'SoftLayerListResult',
]

REST_SPECIAL_METHODS = {
    'deleteObject': 'DELETE',
    'createObject': 'POST',
    'createObjects': 'POST',
    'editObject': 'PUT',
    'editObjects': 'PUT',
}


def get_session(user_agent):
    """Sets up urllib sessions"""

    client = requests.Session()
    client.headers.update({
        'Content-Type': 'application/json',
        'User-Agent': user_agent,
    })
    retry = Retry(connect=3, backoff_factor=3)
    adapter = HTTPAdapter(max_retries=retry)
    client.mount('https://', adapter)
    return client


class Request(object):
    """Transport request object."""

    def __init__(self):
        #: API service name. E.G. SoftLayer_Account
        self.service = None

        #: API method name. E.G. getObject
        self.method = None

        #: API Parameters.
        self.args = tuple()

        #: API headers, used for authentication, masks, limits, offsets, etc.
        self.headers = {}

        #: Transport user.
        self.transport_user = None

        #: Transport password.
        self.transport_password = None

        #: Transport headers.
        self.transport_headers = {}

        #: Boolean specifying if the server certificate should be verified.
        self.verify = None

        #: Client certificate file path.
        self.cert = None

        #: InitParameter/identifier of an object.
        self.identifier = None

        #: SoftLayer mask (dict or string).
        self.mask = None

        #: SoftLayer Filter (dict).
        self.filter = None

        #: Integer result limit.
        self.limit = None

        #: Integer result offset.
        self.offset = None


class SoftLayerListResult(list):
    """A SoftLayer API list result."""

    def __init__(self, items, total_count):

        #: total count of items that exist on the server. This is useful when
        #: paginating through a large list of objects.
        self.total_count = total_count

        super(SoftLayerListResult, self).__init__(items)


class XmlRpcTransport(object):
    """XML-RPC transport."""
    def __init__(self, endpoint_url=None, timeout=None, proxy=None, user_agent=None, verify=True):

        self.endpoint_url = (endpoint_url or
                             consts.API_PUBLIC_ENDPOINT).rstrip('/')
        self.timeout = timeout or None
        self.proxy = proxy
        self.user_agent = user_agent or consts.USER_AGENT
        self.verify = verify
        self._client = None

    @property
    def client(self):
        """Returns client session object"""

        if self._client is None:
            self._client = get_session(self.user_agent)
        return self._client

    def __call__(self, request):
        """Makes a SoftLayer API call against the XML-RPC endpoint.

        :param request request: Request object
        """
        largs = list(request.args)

        headers = request.headers

        if request.identifier is not None:
            header_name = request.service + 'InitParameters'
            headers[header_name] = {'id': request.identifier}

        if request.mask is not None:
            headers.update(_format_object_mask_xmlrpc(request.mask,
                                                      request.service))

        if request.filter is not None:
            headers['%sObjectFilter' % request.service] = request.filter

        if request.limit:
            headers['resultLimit'] = {
                'limit': request.limit,
                'offset': request.offset or 0,
            }

        largs.insert(0, {'headers': headers})
        request.transport_headers.setdefault('Content-Type', 'application/xml')
        request.transport_headers.setdefault('User-Agent', self.user_agent)

        url = '/'.join([self.endpoint_url, request.service])
        payload = utils.xmlrpc_client.dumps(tuple(largs),
                                            methodname=request.method,
                                            allow_none=True)

        # Prefer the request setting, if it's not None
        verify = request.verify
        if verify is None:
            verify = self.verify

        LOGGER.debug("=== REQUEST ===")
        LOGGER.debug('POST %s', url)
        LOGGER.debug(request.transport_headers)
        LOGGER.debug(payload)

        try:
            resp = self.client.request('POST', url,
                                       data=payload,
                                       headers=request.transport_headers,
                                       timeout=self.timeout,
                                       verify=verify,
                                       cert=request.cert,
                                       proxies=_proxies_dict(self.proxy))
            LOGGER.debug("=== RESPONSE ===")
            LOGGER.debug(resp.headers)
            LOGGER.debug(resp.content)
            resp.raise_for_status()
            result = utils.xmlrpc_client.loads(resp.content)[0][0]
            if isinstance(result, list):
                return SoftLayerListResult(
                    result, int(resp.headers.get('softlayer-total-items', 0)))
            else:
                return result
        except utils.xmlrpc_client.Fault as ex:
            # These exceptions are formed from the XML-RPC spec
            # http://xmlrpc-epi.sourceforge.net/specs/rfc.fault_codes.php
            error_mapping = {
                '-32700': exceptions.NotWellFormed,
                '-32701': exceptions.UnsupportedEncoding,
                '-32702': exceptions.InvalidCharacter,
                '-32600': exceptions.SpecViolation,
                '-32601': exceptions.MethodNotFound,
                '-32602': exceptions.InvalidMethodParameters,
                '-32603': exceptions.InternalError,
                '-32500': exceptions.ApplicationError,
                '-32400': exceptions.RemoteSystemError,
                '-32300': exceptions.TransportError,
            }
            _ex = error_mapping.get(ex.faultCode, exceptions.SoftLayerAPIError)
            raise _ex(ex.faultCode, ex.faultString)
        except requests.HTTPError as ex:
            raise exceptions.TransportError(ex.response.status_code, str(ex))
        except requests.RequestException as ex:
            raise exceptions.TransportError(0, str(ex))


class RestTransport(object):
    """REST transport.

    REST calls should mostly work, but is not fully tested.
    XML-RPC should be used when in doubt
    """

    def __init__(self, endpoint_url=None, timeout=None, proxy=None, user_agent=None, verify=True):

        self.endpoint_url = (endpoint_url or consts.API_PUBLIC_ENDPOINT_REST).rstrip('/')
        self.timeout = timeout or None
        self.proxy = proxy
        self.user_agent = user_agent or consts.USER_AGENT
        self.verify = verify
        self._client = None

    @property
    def client(self):
        """Returns client session object"""

        if self._client is None:
            self._client = get_session(self.user_agent)
        return self._client

    def __call__(self, request):
        """Makes a SoftLayer API call against the REST endpoint.

        REST calls should mostly work, but is not fully tested.
        XML-RPC should be used when in doubt

        :param request request: Request object
        """
        params = request.headers.copy()
        if request.mask:
            params['objectMask'] = _format_object_mask(request.mask)

        if request.limit:
            params['limit'] = request.limit

        if request.offset:
            params['offset'] = request.offset

        if request.filter:
            params['objectFilter'] = json.dumps(request.filter)

        auth = None
        if request.transport_user:
            auth = requests.auth.HTTPBasicAuth(
                request.transport_user,
                request.transport_password,
            )

        method = REST_SPECIAL_METHODS.get(request.method)

        if method is None:
            method = 'GET'

        body = {}
        if request.args:
            # NOTE(kmcdonald): force POST when there are arguments because
            # the request body is ignored otherwise.
            method = 'POST'
            body['parameters'] = request.args

        raw_body = None
        if body:
            raw_body = json.dumps(body)

        url_parts = [self.endpoint_url, request.service]
        if request.identifier is not None:
            url_parts.append(str(request.identifier))

        if request.method is not None:
            url_parts.append(request.method)

        url = '%s.%s' % ('/'.join(url_parts), 'json')

        # Prefer the request setting, if it's not None
        verify = request.verify
        if verify is None:
            verify = self.verify

        LOGGER.debug("=== REQUEST ===")
        LOGGER.debug(url)
        LOGGER.debug(request.transport_headers)
        LOGGER.debug(raw_body)
        try:
            resp = self.client.request(method, url,
                                       auth=auth,
                                       headers=request.transport_headers,
                                       params=params,
                                       data=raw_body,
                                       timeout=self.timeout,
                                       verify=verify,
                                       cert=request.cert,
                                       proxies=_proxies_dict(self.proxy))
            LOGGER.debug("=== RESPONSE ===")
            LOGGER.debug(resp.headers)
            LOGGER.debug(resp.text)
            resp.raise_for_status()
            result = json.loads(resp.text)

            if isinstance(result, list):
                return SoftLayerListResult(
                    result, int(resp.headers.get('softlayer-total-items', 0)))
            else:
                return result
        except requests.HTTPError as ex:
            message = json.loads(ex.response.text)['error']
            raise exceptions.SoftLayerAPIError(ex.response.status_code,
                                               message)
        except requests.RequestException as ex:
            raise exceptions.TransportError(0, str(ex))


class TimingTransport(object):
    """Transport that records API call timings."""

    def __init__(self, transport):
        self.transport = transport
        self.last_calls = []

    def __call__(self, call):
        """See Client.call for documentation."""
        start_time = time.time()

        result = self.transport(call)

        end_time = time.time()
        self.last_calls.append((call, start_time, end_time - start_time))
        return result

    def get_last_calls(self):
        """Retrieves the last_calls property.

        This property will contain a list of tuples in the form
        (Request, initiated_utc_timestamp, execution_time)
        """
        last_calls = self.last_calls
        self.last_calls = []
        return last_calls


class FixtureTransport(object):
    """Implements a transport which returns fixtures."""
    def __call__(self, call):
        """Load fixture from the default fixture path."""
        try:
            module_path = 'SoftLayer.fixtures.%s' % call.service
            module = importlib.import_module(module_path)
        except ImportError:
            raise NotImplementedError('%s fixture is not implemented' % call.service)
        try:
            return getattr(module, call.method)
        except AttributeError:
            raise NotImplementedError('%s::%s fixture is not implemented' % (call.service, call.method))


def _proxies_dict(proxy):
    """Makes a proxy dict appropriate to pass to requests."""
    if not proxy:
        return None
    return {'http': proxy, 'https': proxy}


def _format_object_mask_xmlrpc(objectmask, service):
    """Format new and old style object masks into proper headers.

    :param objectmask: a string- or dict-based object mask
    :param service: a SoftLayer API service name

    """
    if isinstance(objectmask, dict):
        mheader = '%sObjectMask' % service
    else:
        mheader = 'SoftLayer_ObjectMask'
        objectmask = _format_object_mask(objectmask)

    return {mheader: {'mask': objectmask}}


def _format_object_mask(objectmask):
    """Format the new style object mask.

    This wraps the user mask with mask[USER_MASK] if it does not already
    have one. This makes it slightly easier for users.

    :param objectmask: a string-based object mask

    """
    objectmask = objectmask.strip()
    if (not objectmask.startswith('mask') and
            not objectmask.startswith('[')):
        objectmask = "mask[%s]" % objectmask
    return objectmask
