""" Cache module that implements the SSM caching wrapper """
from __future__ import absolute_import, print_function

from datetime import datetime, timedelta
from functools import wraps
import six

import boto3
from botocore.exceptions import ClientError

class InvalidParam(Exception):
    """ Raised when something's wrong with the provided param name """

class Refreshable(object):

    ssm_client = boto3.client('ssm')

    def __init__(self, max_age):
        self._last_refresh_time = None
        self._max_age = max_age
        self._max_age_delta = timedelta(seconds=max_age or 0)
    
    def _refresh(self):
        raise NotImplementedError

    def _should_refresh(self):
        # never force refresh if no max_age is configured
        if not self._max_age:
            return False
        # always force refresh if values were never fetched
        if not self._last_refresh_time:
            return True
        # force refresh only if max_age seconds have expired
        return datetime.utcnow() > self._last_refresh_time + self._max_age_delta
    
    def refresh(self):
        self._refresh()
        # keep track of update date for max_age checks
        self._last_refresh_time = datetime.utcnow()


class SSMParameterGroup(Refreshable):
    def __init__(self, max_age=None, with_decryption=True):
        super(SSMParameterGroup, self).__init__(max_age)
        
        self._with_decryption = with_decryption
        self._parameters = {}
    
    def parameter(self, *args, **kwargs):
        kwargs = kwargs.copy()
        if 'max_age' in kwargs:
            raise ValueError("max_age can't be set individually for grouped parameters")
        if 'with_decryption' not in kwargs:
            kwargs['with_decryption'] = self._with_decryption
        parameter = SSMParameter(*args, **kwargs)
        parameter._group = self
        self._parameters[parameter._name] = parameter
        return parameter
    
    def _refresh(self):
        # use batch get
        with_decryption = any(p._with_decryption for p in six.itervalues(self._parameters))
        names = [p._name for p in six.itervalues(self._parameters)]
        invalid_names = []
        for name_batch in _batch(names, 10): # can only get 10 parameters at a time
            response = self.ssm_client.get_parameters(
                Names=name_batch,
                WithDecryption=with_decryption,
            )
            invalid_names.extend(response['InvalidParameters'])
            for item in response['Parameters']:
                self._parameters[item['Name']]._value = item['Value']
        if invalid_names:
            raise InvalidParam(",".join(invalid_names))

class SSMParameter(Refreshable):
    """ The class wraps an SSM Parameter and adds optional caching """

    def __init__(self, param_name, max_age=None, with_decryption=True):
        super(SSMParameter, self).__init__(max_age)
        if not param_name:
            raise ValueError("Must specify name")
        self._name = param_name
        self._value = None
        self._with_decryption = with_decryption
        self._group = None

    def _refresh(self):
        """ Force refresh of the configured param names """
        if self._group:
            return self._group.refresh()
        
        try:
            response = self.ssm_client.get_parameter(
                Name=self._name,
                WithDecryption=self._with_decryption,
            )
            self._value = response['Parameter']['Value']
        except ClientError as e:
            if e.response['Error']['Code'] == 'ParameterNotFound':
                raise InvalidParam(self.name)
            raise
        
    @property
    def name(self):
        return self._name

    @property
    def value(self):
        """
            Retrieve the value of a given param name.
            If only one name is configured, the name can be omitted.
        """
        
        if self._value is None or self._should_refresh():
            self.refresh()
        return self._value

    def refresh_on_error(
            self,
            error_class=Exception,
            error_callback=None,
            retry_argument='is_retry'
        ):
        """ Decorator to handle errors and retries """
        def true_decorator(func):
            """ Actual func wrapper """
            @wraps(func)
            def wrapped(*args, **kwargs):
                """ Actual error/retry handling """
                try:
                    return func(*args, **kwargs)
                except error_class:
                    self.refresh()
                    if callable(error_callback):
                        error_callback()
                    kwargs[retry_argument] = True
                    return func(*args, **kwargs)
            return wrapped
        return true_decorator

def _batch(iterable, n):
    """Turn an iterable into an iterable of batches of size n (or less, for the last one)"""
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]