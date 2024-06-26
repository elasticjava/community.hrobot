# -*- coding: utf-8 -*-

# Copyright (c), Felix Fontein <felix@fontein.de>, 2019
# Simplified BSD License (see LICENSES/BSD-2-Clause.txt or https://opensource.org/licenses/BSD-2-Clause)
# SPDX-License-Identifier: BSD-2-Clause

from __future__ import absolute_import, division, print_function
__metaclass__ = type


from ansible.module_utils.common.text.converters import to_native
from ansible.module_utils.six import PY3
from ansible.module_utils.six.moves.urllib.error import HTTPError
from ansible.module_utils.urls import fetch_url, open_url

import certifi
import json
import time


ROBOT_DEFAULT_ARGUMENT_SPEC = dict(
    hetzner_user=dict(type='str', required=True),
    hetzner_password=dict(type='str', required=True, no_log=True),
)

# The API endpoint is fixed.
BASE_URL = "https://robot-ws.your-server.de"


def get_x_www_form_urlenconded_dict_from_list(key, values):
    '''Return a dictionary with keys values'''
    if len(values) == 1:
        return {'{key}[]'.format(key=key): values[0]}
    else:
        return dict(('{key}[{index}]'.format(key=key, index=i), x) for i, x in enumerate(values))


def _format_list(obj):
    if not isinstance(obj, (list, tuple)):
        return to_native(obj)
    return [_format_list(e) for e in obj]


def format_error_msg(error):
    # Reference: https://robot.hetzner.com/doc/webservice/en.html#errors
    msg = 'Request failed: {0} {1} ({2})'.format(
        error['status'],
        error['code'],
        error['message'],
    )
    if error.get('missing'):
        msg += '. Missing input parameters: {0}'.format(_format_list(error['missing']))
    if error.get('invalid'):
        msg += '. Invalid input parameters: {0}'.format(_format_list(error['invalid']))
    if error.get('max_request') is not None:
        msg += '. Maximum allowed requests: {0}'.format(error['max_request'])
    if error.get('interval') is not None:
        msg += '. Time interval in seconds: {0}'.format(error['interval'])
    return msg


class PluginException(Exception):
    def __init__(self, message):
        super(PluginException, self).__init__(message)
        self.error_message = message


def plugin_open_url_json(plugin, url, method='GET', timeout=10, data=None, headers=None,
                         accept_errors=None, allow_empty_result=False,
                         allowed_empty_result_status_codes=(200, 204), templar=None):
    '''
    Make general request to Hetzner's JSON robot API.
    '''
    user = plugin.get_option('hetzner_user')
    password = plugin.get_option('hetzner_password')
    if templar is not None:
        if templar.is_template(user):
            user = templar.template(variable=user, disable_lookups=False)
        if templar.is_template(password):
            password = templar.template(variable=password, disable_lookups=False)
    try:
        response = open_url(
            url,
            url_username=user,
            url_password=password,
            force_basic_auth=True,
            data=data,
            headers=headers,
            method=method,
            timeout=timeout,
            ca_path=certifi.where(),
        )
        status = response.code
        content = response.read()
    except HTTPError as e:
        status = e.code
        try:
            content = e.read()
        except AttributeError:
            content = b''
    except Exception as e:
        raise PluginException('Failed request to Hetzner Robot server endpoint {0}: {1}'.format(url, e))

    if not content:
        if allow_empty_result and status in allowed_empty_result_status_codes:
            return None, None
        raise PluginException('Cannot retrieve content from {0}, HTTP status code {1}'.format(url, status))

    try:
        result = json.loads(content.decode('utf-8'))
        if 'error' in result:
            if accept_errors:
                if result['error']['code'] in accept_errors:
                    return result, result['error']['code']
            raise PluginException(format_error_msg(result['error']))
        return result, None
    except ValueError:
        raise PluginException('Cannot decode content retrieved from {0}'.format(url))


def fetch_url_json(module, url, method='GET', timeout=10, data=None, headers=None,
                   accept_errors=None, allow_empty_result=False,
                   allowed_empty_result_status_codes=(200, 204)):
    '''
    Make general request to Hetzner's JSON robot API.
    '''
    module.params['url_username'] = module.params['hetzner_user']
    module.params['url_password'] = module.params['hetzner_password']
    module.params['force_basic_auth'] = True
    resp, info = fetch_url(module, url, method=method, timeout=timeout, data=data, headers=headers, ca_path=certifi.where())
    try:
        # In Python 2, reading from a closed response yields a TypeError.
        # In Python 3, read() simply returns ''
        if PY3 and resp.closed:
            raise TypeError
        content = resp.read()
    except (AttributeError, TypeError):
        content = info.pop('body', None)

    if not content:
        if allow_empty_result and info.get('status') in allowed_empty_result_status_codes:
            return None, None
        module.fail_json(msg='Cannot retrieve content from {0}, HTTP status code {1}'.format(url, info.get('status')))

    try:
        result = module.from_json(content.decode('utf8'))
        if 'error' in result:
            if accept_errors:
                if result['error']['code'] in accept_errors:
                    return result, result['error']['code']
            module.fail_json(msg=format_error_msg(result['error']), error=result['error'])
        return result, None
    except ValueError:
        module.fail_json(msg='Cannot decode content retrieved from {0}'.format(url))


class CheckDoneTimeoutException(Exception):
    def __init__(self, result, error):
        super(CheckDoneTimeoutException, self).__init__()
        self.result = result
        self.error = error


def fetch_url_json_with_retries(module, url, check_done_callback, check_done_delay=10, check_done_timeout=180, skip_first=False, **kwargs):
    '''
    Make general request to Hetzner's JSON robot API, with retries until a condition is satisfied.

    The condition is tested by calling ``check_done_callback(result, error)``. If it is not satisfied,
    it will be retried with delays ``check_done_delay`` (in seconds) until a total timeout of
    ``check_done_timeout`` (in seconds) since the time the first request is started is reached.

    If ``skip_first`` is specified, will assume that a first call has already been made and will
    directly start with waiting.
    '''
    start_time = time.time()
    if not skip_first:
        result, error = fetch_url_json(module, url, **kwargs)
        if check_done_callback(result, error):
            return result, error
    while True:
        elapsed = (time.time() - start_time)
        left_time = check_done_timeout - elapsed
        time.sleep(max(min(check_done_delay, left_time), 0))
        result, error = fetch_url_json(module, url, **kwargs)
        if check_done_callback(result, error):
            return result, error
        if left_time < check_done_delay:
            raise CheckDoneTimeoutException(result, error)
